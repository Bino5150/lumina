"""Internal trusted Git review observation kernel (CODING-08A1).

Architectural boundary::

    already-resolved repository/worktree target (TargetIdentity)
                        |
              trusted review kernel  (this module)
                        |
             immutable structured facts (ReviewSnapshot)
                        |
              [future A2/A3/A4 consumers]

This module owns *observation*, not authorization. Callers must already
have resolved a ``core.coding_checkpoint.TargetIdentity`` through
CODING-05/06/07 infrastructure (``coding_checkpoint.resolve_target_identity``,
a ``WorktreeHandle``'s ``target_identity.target``, etc) -- this kernel
never decides whether a caller was entitled to do that, and never
consults owner status, PIN state, tool profile state, or model
provenance. What it *does* decide is narrower: whether the supplied
identity is still, live, the same repository/worktree it claims to be.
That single check is the only place "authorization-shaped" reasoning
belongs here, and it is target validation, not caller authorization.

No tool is registered here. No Qt code, no ToolRegistry, no model-facing
schema, no stage/unstage/discard/commit/push/merge mutation. Every Git
invocation is a bounded, argv-only, no-shell read through
``core.git_read.run_bounded_git`` -- the same hardened subprocess boundary
``core.coding_checkpoint_observation`` uses, so there is exactly one
hardened Git-read implementation in the codebase, not two subtly
different ones.

Ordinary Git diff generation can execute two configured escape hatches:
``diff.external`` (replaces Git's own diff generator entirely) and a
per-attribute ``textconv`` filter (rewrites blob content before Git ever
diffs it). CODING-08A0 confirmed both run under a naive ``git diff``.
Every diff-shaped invocation in this module therefore forces
``--no-ext-diff`` and ``--no-textconv`` explicitly -- never relies on
the *absence* of a diff driver being configured, and never relies on
the specific Git output mode chosen happening to be exempt (see the
next paragraph). See ``tests/test_git_review.py``'s external-helper
sentinel tests, which configure both hooks to leave forensic evidence
if they ever ran and assert that evidence never appears.

Empirically (verified directly against Git 2.43.0, not assumed): the
``--numstat`` metadata mode this module actually uses never invokes
either hook regardless of these flags, because it never renders
human-readable diff text or hands blob content to an external
program -- only full patch generation does. That makes this module's
diff calls doubly safe (immune by output-mode choice, and explicitly
guarded besides) but it also means the executable sentinel tests alone
cannot prove the guard flags are load-bearing for this module's
specific invocations -- removing them changes nothing observable here.
``tests/test_git_review.py`` therefore also asserts, at the
argv-construction level, that every diff-shaped call this module makes
carries both flags; that construction-level test is what actually goes
red if a flag is dropped. See the CODING-08A1 handoff report for the
full empirical record.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from typing import Optional, Sequence

import core.coding_checkpoint as checkpoint_store
from core.git_read import GitCommandResult, GitReadError, run_bounded_git

GIT_TIMEOUT_SECONDS = 5
MAX_GIT_STDOUT_BYTES = 16 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 16 * 1024

# Global argv flags forced onto *every* review Git invocation, positioned
# before the subcommand as Git requires for global options:
#   --no-pager        belt-and-suspenders; PIPE stdout already keeps Git
#                      from invoking a pager, but this never depends on that.
#   -c color.ui=never  porcelain/numstat output is never colorized regardless,
#                      but this is a global override rather than a
#                      per-subcommand flag so it applies uniformly and never
#                      fails with "unknown option" on a subcommand that
#                      doesn't recognize --no-color/--color.
_GLOBAL_SAFE_ARGS = ("--no-pager", "-c", "color.ui=never")

# Forced onto every diff-shaped invocation specifically -- the two escape
# hatches CODING-08A0 identified. Never made conditional on whether a
# repository *appears* to have either configured; both are always passed.
_DIFF_SAFETY_ARGS = ("--no-ext-diff", "--no-textconv")

_OPERATION_PATHS = {
    "merge": ("MERGE_HEAD",),
    "cherry_pick": ("CHERRY_PICK_HEAD",),
    "rebase": ("rebase-merge", "rebase-apply"),
    "revert": ("REVERT_HEAD",),
    "bisect": ("BISECT_LOG",),
}

# The only relations for which a two-sided (HEAD<->index or index<->worktree)
# diff is even meaningful. Unmerged entries have no ordinary two-endpoint
# patch (A1 does not attempt to synthesize one); untracked entries have no
# HEAD/index side to diff against at all.
_DIFFABLE_RELATIONS = ("ordinary", "rename", "copy")


class GitReviewError(Exception):
    """Base class for every error this module raises."""


class ReviewTargetError(GitReviewError):
    """The supplied TargetIdentity is not (or is no longer) a live match.

    Raised for both "this was never a Git target" and "this used to
    resolve to the claimed identity but no longer does" -- callers that
    need to distinguish preflight from concurrent-change cases should
    inspect the message; both are equally "do not trust this snapshot."
    """


class ReviewProbeError(GitReviewError):
    """A bounded Git read failed at the transport level, or exited in a
    way this module was not prepared to interpret."""


class MalformedReviewStatus(ReviewProbeError):
    """Git produced status or diff-metadata output this parser does not
    understand. Fails closed -- never guesses at a partially-parsed
    record's meaning."""


class UnstableReviewCapture(GitReviewError):
    """The repository changed materially between the two stability-check
    observations, even after one conservative retry. No snapshot mixing
    two different repository states is ever returned."""


@dataclass(frozen=True)
class SubmoduleFlags:
    is_submodule: bool
    commit_changed: bool
    has_tracked_changes: bool
    has_untracked_changes: bool


_SUBMODULE_NONE = SubmoduleFlags(False, False, False, False)


@dataclass(frozen=True)
class UnmergedStage:
    """One stage (1=base/common-ancestor, 2=ours, 3=theirs) of a conflict."""
    stage: int
    mode: str
    object_id: str


@dataclass(frozen=True)
class DiffMetadata:
    """Safe, content-free Git diff metadata for one side of one change.

    Derived from ``git diff --numstat`` with ``--no-ext-diff``/
    ``--no-textconv`` always forced, and always with ``--no-renames`` --
    see ``_capture_diff_metadata``'s docstring for why. ``insertions``/
    ``deletions`` are None exactly when ``binary`` is True (Git reports
    "-"/"-" for a binary pair rather than line counts); never a guess.

    This is metadata for *later* content retrieval (a future A-slice), not
    a hunk or any file byte -- deliberately short of "full hunk-content
    retrieval," which stays out of A1's scope.
    """
    binary: bool
    insertions: Optional[int]
    deletions: Optional[int]


@dataclass(frozen=True)
class ReviewChange:
    """One raw porcelain-v2 change record, kept structurally truthful.

    ``record_type`` is the raw porcelain family this came from ("1"
    ordinary, "2" rename/copy, "u" unmerged, "?" untracked) -- never
    inferred, always the literal record marker Git emitted.

    ``relation`` is a semantic classification derived from record_type
    (plus, for "2" records, the X<score> marker's letter): "ordinary",
    "rename", "copy", "unmerged", or "untracked". A "2" record's X letter
    is R or C and is never collapsed to "rename" regardless of which one
    Git actually reported (CODING-08A0's MB-39: the existing checkpoint
    presentation layer does exactly that collapse; this module does not).

    ``staged``/``unstaged`` are independent booleans derived directly from
    the two XY status characters -- never merged into one flag. A path
    that is both staged and further modified in the worktree ("MM") has
    staged=True and unstaged=True simultaneously; that is the whole point
    of keeping HEAD->index and index->worktree as separate layers (see
    the module docstring and CODING-08A0's slice spec, section 6).

    Mode/object-id fields are the literal strings Git reported, including
    the all-zero sentinels Git itself uses for "this side doesn't have
    this path" (mode "000000", the all-zero object id) -- never converted
    to None to look tidier. None is reserved for "this record type
    structurally does not carry this field at all" (e.g. an untracked
    entry has no HEAD/index mode or object id whatsoever).
    """
    record_type: str
    relation: str
    path: str
    original_path: Optional[str]
    relation_score: Optional[int]
    xy_status: str
    staged: bool
    unstaged: bool
    untracked: bool
    submodule: SubmoduleFlags
    head_mode: Optional[str]
    index_mode: Optional[str]
    worktree_mode: Optional[str]
    head_object_id: Optional[str]
    index_object_id: Optional[str]
    unmerged_stages: tuple
    staged_diff: Optional[DiffMetadata]
    unstaged_diff: Optional[DiffMetadata]


@dataclass(frozen=True)
class ReviewSnapshot:
    """One stable, structured observation of a Git target's review state.

    ``identity`` is the caller-supplied, kernel-reverified TargetIdentity
    this snapshot is bound to (see ``capture_review_snapshot``).
    ``changes`` preserves every porcelain-v2 record this module supports
    (section 7 of the CODING-08A1 spec) with no pagination and no count
    truncation -- the only bound is the underlying bounded-read byte cap
    shared with every other Git invocation in this module. Snapshot
    pagination for model-facing display is deliberately a later slice's
    concern, not this one's.
    """
    identity: checkpoint_store.TargetIdentity
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    unborn: bool
    operation_state: tuple
    changes: tuple
    captured_at: str


@dataclass(frozen=True)
class _RepositoryFacts:
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    unborn: bool
    operation_state: tuple


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _serialize_path(value: bytes) -> str:
    path = _decode(value)
    if os.sep != "/":
        path = path.replace(os.sep, "/")
    if os.altsep and os.altsep != "/":
        path = path.replace(os.altsep, "/")
    return path


def _run_git(root: str, args: Sequence[str]) -> GitCommandResult:
    try:
        return run_bounded_git(
            root, (*_GLOBAL_SAFE_ARGS, *args),
            timeout=GIT_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_GIT_STDOUT_BYTES,
            max_stderr_bytes=MAX_GIT_STDERR_BYTES,
        )
    except GitReadError as error:
        raise ReviewProbeError(str(error)) from error


def _one_line(result: GitCommandResult, what: str) -> str:
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 1 or not lines[0]:
        raise ReviewProbeError(f"Git could not provide {what}")
    return _decode(lines[0])


# ---------------------------------------------------------------------------
# Target identity re-verification. This kernel does not resolve an identity
# from scratch on a caller's behalf -- resolve_target_identity() already
# does that, and duplicating it would be exactly the "second repository
# identity system" the CODING-08A1 spec forbids. It only re-probes and
# compares, so a supplied identity that is stale or was never real cannot
# silently pass through to observation.
# ---------------------------------------------------------------------------

def _resolve_live_identity(root: str) -> checkpoint_store.TargetIdentity:
    canonical_requested = os.path.realpath(os.path.expanduser(root))
    result = _run_git(
        canonical_requested,
        ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
    )
    if result.returncode != 0:
        raise ReviewTargetError("target is not a live Git working tree")
    lines = result.stdout.splitlines()
    if len(lines) != 2 or not lines[0] or not lines[1]:
        raise ReviewProbeError("Git target discovery returned an invalid shape")
    toplevel = os.path.realpath(_decode(lines[0]))
    common_dir = os.path.realpath(_decode(lines[1]))
    return checkpoint_store._build_target_identity("git", toplevel, common_dir)


def _verify_identity_match(
    expected: checkpoint_store.TargetIdentity, live: checkpoint_store.TargetIdentity,
):
    if (
        live.kind != expected.kind
        or live.canonical_root != expected.canonical_root
        or live.target_key != expected.target_key
    ):
        raise ReviewTargetError(
            "supplied target identity no longer matches the live repository"
        )


# ---------------------------------------------------------------------------
# Repository facts: HEAD/branch/detached/unborn, hidden operation state.
# Mirrors core.coding_checkpoint_observation's proven _capture_head/
# _capture_operations logic against this module's own _run_git rather than
# importing across domains -- see the CODING-08A1 handoff report for why
# only the low-level subprocess boundary (core.git_read) is shared and this
# small, stable, already-well-understood layer is not.
# ---------------------------------------------------------------------------

def _capture_head(root: str) -> tuple:
    symbolic = _run_git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if symbolic.returncode not in (0, 1):
        raise ReviewProbeError("Git could not resolve HEAD mode")
    branch = _one_line(symbolic, "branch") if symbolic.returncode == 0 else None

    head_result = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head_result.returncode == 0:
        head = _one_line(head_result, "full HEAD object ID")
        if len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head):
            raise ReviewProbeError("Git returned an invalid full HEAD object ID")
    elif head_result.returncode == 128:
        head = None
    else:
        raise ReviewProbeError("Git could not resolve HEAD")

    unborn = branch is not None and head is None
    detached = branch is None and head is not None
    if branch is None and head is None:
        raise ReviewProbeError(
            "Git HEAD is neither a branch, detached commit, nor unborn branch"
        )
    return head, branch, detached, unborn


def _capture_operations(root: str) -> tuple:
    found = []
    for operation, markers in _OPERATION_PATHS.items():
        for marker in markers:
            result = _run_git(
                root, ["rev-parse", "--path-format=absolute", "--git-path", marker]
            )
            path = _one_line(result, f"administrative path for {operation}")
            if os.path.exists(path):
                found.append(operation)
                break
    return tuple(sorted(found))


def _capture_repository_facts(root: str) -> _RepositoryFacts:
    head, branch, detached, unborn = _capture_head(root)
    return _RepositoryFacts(
        head=head, branch=branch, detached=detached, unborn=unborn,
        operation_state=_capture_operations(root),
    )


# ---------------------------------------------------------------------------
# Porcelain-v2 status parsing: the trusted structural inventory.
# ---------------------------------------------------------------------------

def _parse_submodule(sub: str) -> SubmoduleFlags:
    if len(sub) != 4:
        raise MalformedReviewStatus(f"unexpected submodule field: {sub!r}")
    if sub[0] != "S":
        return _SUBMODULE_NONE
    return SubmoduleFlags(
        is_submodule=True,
        commit_changed=sub[1] == "C",
        has_tracked_changes=sub[2] == "M",
        has_untracked_changes=sub[3] == "U",
    )


def _relation_and_score(xscore: str) -> tuple:
    if not xscore or xscore[0] not in ("R", "C"):
        raise MalformedReviewStatus(f"unexpected rename/copy marker: {xscore!r}")
    relation = "rename" if xscore[0] == "R" else "copy"
    score_text = xscore[1:]
    score = int(score_text) if score_text.isdigit() else None
    return relation, score


def _parse_review_status(raw: bytes) -> tuple:
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()

    changes = []
    index = 0
    while index < len(records):
        record = records[index]

        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise MalformedReviewStatus("ordinary status record was malformed")
            xy = _decode(parts[1])
            changes.append(ReviewChange(
                record_type="1", relation="ordinary",
                path=_serialize_path(parts[8]), original_path=None, relation_score=None,
                xy_status=xy,
                staged=xy[0] != ".", unstaged=xy[1] != ".",
                untracked=False,
                submodule=_parse_submodule(_decode(parts[2])),
                head_mode=_decode(parts[3]), index_mode=_decode(parts[4]),
                worktree_mode=_decode(parts[5]),
                head_object_id=_decode(parts[6]), index_object_id=_decode(parts[7]),
                unmerged_stages=(), staged_diff=None, unstaged_diff=None,
            ))

        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index + 1 >= len(records):
                raise MalformedReviewStatus("rename/copy status record was malformed")
            xy = _decode(parts[1])
            relation, score = _relation_and_score(_decode(parts[8]))
            path = _serialize_path(parts[9])
            index += 1
            original_path = _serialize_path(records[index])
            changes.append(ReviewChange(
                record_type="2", relation=relation,
                path=path, original_path=original_path, relation_score=score,
                xy_status=xy,
                staged=xy[0] != ".", unstaged=xy[1] != ".",
                untracked=False,
                submodule=_parse_submodule(_decode(parts[2])),
                head_mode=_decode(parts[3]), index_mode=_decode(parts[4]),
                worktree_mode=_decode(parts[5]),
                head_object_id=_decode(parts[6]), index_object_id=_decode(parts[7]),
                unmerged_stages=(), staged_diff=None, unstaged_diff=None,
            ))

        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise MalformedReviewStatus("unmerged status record was malformed")
            xy = _decode(parts[1])
            stages = tuple(
                UnmergedStage(
                    stage=n, mode=_decode(parts[2 + n]), object_id=_decode(parts[6 + n]),
                )
                for n in (1, 2, 3)
            )
            changes.append(ReviewChange(
                record_type="u", relation="unmerged",
                path=_serialize_path(parts[10]), original_path=None, relation_score=None,
                xy_status=xy,
                staged=xy[0] != ".", unstaged=xy[1] != ".",
                untracked=False,
                submodule=_parse_submodule(_decode(parts[2])),
                head_mode=None, index_mode=None, worktree_mode=_decode(parts[6]),
                head_object_id=None, index_object_id=None,
                unmerged_stages=stages, staged_diff=None, unstaged_diff=None,
            ))

        elif record.startswith(b"? "):
            path_bytes = record[2:]
            if not path_bytes:
                raise MalformedReviewStatus("untracked status record was malformed")
            changes.append(ReviewChange(
                record_type="?", relation="untracked",
                path=_serialize_path(path_bytes), original_path=None, relation_score=None,
                xy_status="??",
                staged=False, unstaged=False, untracked=True,
                submodule=_SUBMODULE_NONE,
                head_mode=None, index_mode=None, worktree_mode=None,
                head_object_id=None, index_object_id=None,
                unmerged_stages=(), staged_diff=None, unstaged_diff=None,
            ))

        else:
            raise MalformedReviewStatus(
                f"unsupported status record type: {record[:2]!r}"
            )
        index += 1

    return tuple(changes)


# ---------------------------------------------------------------------------
# Safe diff metadata: binary detection + line counts, never content.
# ---------------------------------------------------------------------------

def _parse_numstat(raw: bytes) -> dict:
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    result = {}
    for token in tokens:
        if not token:
            continue
        fields = token.split(b"\t", 2)
        if len(fields) != 3:
            raise MalformedReviewStatus("diff numstat record was malformed")
        added_raw, deleted_raw, path_raw = fields
        path = _serialize_path(path_raw)
        if added_raw == b"-" and deleted_raw == b"-":
            result[path] = DiffMetadata(binary=True, insertions=None, deletions=None)
            continue
        try:
            insertions = int(added_raw)
            deletions = int(deleted_raw)
        except ValueError as error:
            raise MalformedReviewStatus(
                "diff numstat counts were not integers"
            ) from error
        if insertions < 0 or deletions < 0:
            raise MalformedReviewStatus("diff numstat counts were negative")
        result[path] = DiffMetadata(binary=False, insertions=insertions, deletions=deletions)
    return result


def _capture_diff_metadata(root: str, *, staged: bool) -> dict:
    """One bulk, content-free ``git diff --numstat`` per layer.

    ``--no-renames`` is always forced. Without it, a rename/copy pair
    collapses into one NUL-triple record (empty-path marker, then old
    path, then new path -- confirmed empirically against real Git 2.43
    output) whose insertions/deletions describe the *pair*, not either
    individual blob. With ``--no-renames``, the same change instead
    produces two ordinary records: the old path as a pure deletion, the
    new path as a pure addition. Binary-ness is a per-blob property (Git
    checks each side's content independently), so the pure-addition
    record at the *current* path is exactly the signal this module needs
    -- "is the blob now at this path binary" -- without taking on
    rename-aware line-attribution semantics A1 does not need and cannot
    get right cheaply. Callers therefore always look this dict up by a
    change's current ``path``, never ``original_path``.
    """
    args = ["diff", *_DIFF_SAFETY_ARGS, "--no-renames", "--numstat", "-z"]
    if staged:
        args = ["diff", "--cached", *_DIFF_SAFETY_ARGS, "--no-renames", "--numstat", "-z"]
    result = _run_git(root, args)
    if result.returncode != 0:
        raise ReviewProbeError("Git review diff metadata observation failed")
    return _parse_numstat(result.stdout)


def _capture_changes(root: str) -> tuple:
    status = _run_git(root, [
        "-c", "status.renames=copies",
        "status", "--porcelain=v2", "-z", "--untracked-files=all",
    ])
    if status.returncode != 0:
        raise ReviewProbeError("Git review status observation failed")
    changes = _parse_review_status(status.stdout)

    needs_staged = any(
        change.staged and change.relation in _DIFFABLE_RELATIONS for change in changes
    )
    needs_unstaged = any(
        change.unstaged and change.relation in _DIFFABLE_RELATIONS for change in changes
    )
    staged_meta = _capture_diff_metadata(root, staged=True) if needs_staged else {}
    unstaged_meta = _capture_diff_metadata(root, staged=False) if needs_unstaged else {}

    return tuple(
        replace(
            change,
            staged_diff=(
                staged_meta.get(change.path)
                if change.staged and change.relation in _DIFFABLE_RELATIONS
                else None
            ),
            unstaged_diff=(
                unstaged_meta.get(change.path)
                if change.unstaged and change.relation in _DIFFABLE_RELATIONS
                else None
            ),
        )
        for change in changes
    )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def capture_review_snapshot(identity: checkpoint_store.TargetIdentity) -> ReviewSnapshot:
    """Capture one stable, structured Git review snapshot.

    ``identity`` must already be a resolved ``TargetIdentity`` -- from
    ``core.coding_checkpoint.resolve_target_identity()``, a
    ``WorktreeHandle.target_identity.target``, or an equivalent
    CODING-05/06/07 seam. This kernel re-verifies, live, that the target
    still resolves to exactly that identity before (and after) observing
    it; it never decides whether the caller was entitled to resolve that
    identity in the first place (see the module docstring).

    The whole observe-A / observe-B sequence is retried once on any
    mismatch (identity, repository facts, or changes). A second
    inconsistency raises UnstableReviewCapture rather than returning or
    silently preferring one of two different repository states.
    """
    if not isinstance(identity, checkpoint_store.TargetIdentity):
        raise ReviewTargetError("identity must be an already-resolved TargetIdentity")
    if identity.kind != "git":
        raise ReviewTargetError("review kernel only observes Git targets")

    for attempt in range(2):
        live_a = _resolve_live_identity(identity.canonical_root)
        _verify_identity_match(identity, live_a)
        facts_a = _capture_repository_facts(identity.canonical_root)
        changes_a = _capture_changes(identity.canonical_root)

        live_b = _resolve_live_identity(identity.canonical_root)
        _verify_identity_match(identity, live_b)
        facts_b = _capture_repository_facts(identity.canonical_root)
        changes_b = _capture_changes(identity.canonical_root)

        if live_a == live_b and facts_a == facts_b and changes_a == changes_b:
            return ReviewSnapshot(
                identity=identity,
                head=facts_b.head,
                branch=facts_b.branch,
                detached=facts_b.detached,
                unborn=facts_b.unborn,
                operation_state=facts_b.operation_state,
                changes=changes_b,
                captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        if attempt == 1:
            raise UnstableReviewCapture(
                "repository changed materially during review capture"
            )
    raise AssertionError("unreachable review capture retry state")
