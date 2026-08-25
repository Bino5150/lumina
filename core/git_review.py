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
diff calls doubly safe against ``diff.external``/``textconv``
specifically (immune by output-mode choice, and explicitly guarded
besides) but it also means the executable sentinel tests alone
cannot prove the guard flags are load-bearing for this module's
specific invocations -- removing them changes nothing observable here.
``tests/test_git_review.py`` therefore also asserts, at the
argv-construction level, that every diff-shaped call this module makes
carries both flags; that construction-level test is what actually goes
red if a flag is dropped. See the CODING-08A1 handoff report for the
full empirical record.

CODING-08R.1: ``diff.external``/``textconv`` are not the only Git-
supported external-process escape hatches a status/diff invocation can
reach, and the "immune by output-mode" property above does not extend
to either of the two CODING-08R found live:

1. ``core.fsmonitor`` -- a repository-local hook *path* Git executes on
   ordinary ``status`` and ``diff``/``--numstat`` calls alike (confirmed:
   fires even on a ``--cached``-only diff). Neutralized globally by
   ``-c core.fsmonitor=`` in ``_GLOBAL_SAFE_ARGS`` below -- see that
   constant's own comment for why the value is the empty string and
   never the string ``"false"``.
2. A tracked ``.gitattributes`` entry selecting a locally-configured
   ``filter.<name>.clean``/``.process`` driver -- confirmed to fire on
   worktree-side ``--numstat`` and patch generation (never on a
   ``--cached`` diff, and never on plain ``status``). Unlike
   ``core.fsmonitor``, Git has no single flag that suppresses a
   repository's own in-tree ``.gitattributes``, so this cannot be closed
   by adding another argv flag here. See ``_filter_free_diff``'s
   docstring below for the structural fix and its documented semantic
   contract change.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
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
#   -c core.fsmonitor= CODING-08R.1: a repository-local core.fsmonitor value
#                      names an external program Git executes on *every*
#                      status/diff/numstat call this module makes -- not just
#                      the diff-shaped ones the two flags below cover.
#                      Empirically confirmed (CODING-08R) against real Git
#                      2.43.0: a hostile .git/config's core.fsmonitor fires on
#                      plain `status`, unstaged `diff --numstat`, and even
#                      `--cached diff --numstat`. Deliberately the EMPTY
#                      STRING, never "false": Git's own documentation states
#                      that Git <=2.35.1 does not understand the boolean form
#                      and treats the literal value "false" as a hook
#                      *pathname* to invoke -- empirically that still lands on
#                      the harmless real `false` utility on a normal PATH
#                      (the command-line override replaces the repository's
#                      own value before any lookup happens, so an attacker's
#                      configured hook path is never consulted), but nothing
#                      about that safety is guaranteed by Git itself. An empty
#                      pathname cannot resolve to any executable under either
#                      the pre- or post-2.36 parser, on any version, so it is
#                      the only form that is safe *by construction* rather
#                      than by favorable resolution. See the CODING-08R.1
#                      handoff report for the full empirical record across
#                      "", "false", and "0".
_GLOBAL_SAFE_ARGS = ("--no-pager", "-c", "color.ui=never", "-c", "core.fsmonitor=")

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


class _RawContentUnavailable(Exception):
    """CODING-08R.1: a worktree/index byte source for filter-free diff
    metadata could not be read as expected (missing, changed type mid-read,
    or over the byte cap). Never raised out of this module -- a caller
    degrades the affected change's diff metadata to None (see
    ``_capture_unstaged_diff_metadata``), exactly like today's bulk numstat
    path already does for a path a live ``git diff --numstat`` happened not
    to report. A genuine race is caught by ``capture_review_snapshot``'s own
    outer A/B stability retry, not by anything in this class."""


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


def _capture_diff_metadata(root: str) -> dict:
    """One bulk, content-free ``git diff --cached --numstat``.

    STAGED only (index<->HEAD). Confirmed empirically (CODING-08R.1) that a
    ``--cached`` diff never invokes a repository-configured content filter
    (``filter.<name>.clean``/``.process``): both sides are already
    content-addressed, already-clean objects, so Git never needs to convert
    worktree bytes for this comparison. The unstaged (index<->worktree)
    counterpart -- which DOES need worktree bytes, and therefore CAN invoke
    a configured filter -- is computed by
    ``_capture_unstaged_diff_metadata`` instead; see that function's
    docstring.

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
    args = ["diff", "--cached", *_DIFF_SAFETY_ARGS, "--no-renames", "--numstat", "-z"]
    result = _run_git(root, args)
    if result.returncode != 0:
        raise ReviewProbeError("Git review diff metadata observation failed")
    return _parse_numstat(result.stdout)


# ---------------------------------------------------------------------------
# Filter-free content comparison (CODING-08R.1).
#
# A worktree-side diff/numstat invocation can trigger a repository-selected
# content filter (``filter.<name>.clean``/``.process``), declared by a
# TRACKED ``.gitattributes`` file (ordinary repository content -- it arrives
# via a plain ``git clone``, no local config tampering required) and defined
# by local Git config -- an external-process escape hatch ``--no-ext-diff``/
# ``--no-textconv`` do not cover; those two flags only ever addressed
# ``diff.external``/``textconv``, a structurally different Git mechanism.
# Empirically confirmed (CODING-08R): unlike ``core.fsmonitor``, there is no
# single Git flag that suppresses a repository's own tracked
# ``.gitattributes`` -- ``core.attributesFile`` only ever supplies an
# *additional* global file, never a substitute for the in-tree one, and
# ``.git/info/attributes`` is a per-path override a caller would have to
# enumerate every hostile path to defeat, not a blanket "off" switch.
#
# The fix is structural, not a flag: never ask Git to diff the real worktree
# path against the real index blob. Instead, read both sides' bytes through
# paths that cannot execute a filter --
#   - worktree side: a direct, unconverting filesystem read (mirrors
#     core.git_review_snapshot's own content-fingerprint read exactly --
#     filters only run at Git's checkin/checkout boundary, never on a raw
#     filesystem read).
#   - index side: ``git cat-file blob <object-id>`` -- reading an
#     already-stored, content-addressed object never applies a clean/
#     smudge/process filter; those only run when content crosses the
#     worktree<->index boundary, not when an existing blob is read back out.
# -- then hand both raw byte-strings to Git's OWN ``diff --no-index`` engine,
# run against a private temporary directory that is not inside any Git
# working tree and carries no ``.gitattributes`` of its own, under generic
# filenames unrelated to the real path, so no attribute pattern -- the
# reviewed repository's or otherwise -- can select a filter for these paths
# even by coincidence. This reuses Git's real, already-tested diff/numstat/
# hunk-generation algorithm verbatim (unchanged downstream parsing in this
# module and in core.git_review_snapshot); only the byte SOURCE changes.
#
# Semantic contract change: displayed diff content and numstat counts now
# reflect the file's actual on-disk/in-object bytes, never Git's own
# content-conversion view -- no EOL (``text``/``eol``) normalization, no
# ``ident`` keyword expansion, no ``working-tree-encoding`` re-encoding, no
# LFS-style or custom clean/smudge filter. A repository that relies on one
# of those for a cosmetic/cross-platform view will see review content differ
# from what a plain terminal ``git diff`` shows in the worktree. This is a
# deliberate, documented choice -- truthful raw content over ever executing
# repository-selected code -- not an oversight. Binary detection is
# unaffected in spirit (still Git's own numstat heuristic) but now runs
# against the same raw bytes rather than filter-converted ones, so a file
# whose *only* filter effect was to change text<->binary classification
# will be classified by its raw on-disk form instead.
#
# Confirmed empirically (CODING-08R.1) NOT needed for: the STAGED layer
# (``_capture_diff_metadata`` above -- a ``--cached`` diff never touches
# worktree bytes or invokes a clean/smudge/process filter) and ``git
# status`` itself (status never invokes a content filter at all, only
# ``core.fsmonitor``, which ``_GLOBAL_SAFE_ARGS`` already neutralizes).
# Also confirmed NOT reachable through review at all: ``smudge`` filters,
# which only run on checkout/populate-worktree-from-index -- an operation
# review never performs (see the CODING-08R.1 handoff report).
# ---------------------------------------------------------------------------

MAX_FILTER_FREE_READ_BYTES = 32 * 1024 * 1024
_FILTER_FREE_READ_CHUNK_BYTES = 256 * 1024


def _read_worktree_bytes_raw(root: str, relative_path: str) -> bytes:
    """Direct, unconverting read of a worktree entry's current bytes.

    A symlink's own target text is returned (never followed) -- Git itself
    tracks a symlink as a blob whose content IS the target string, so this
    mirrors what ``git diff``/``--numstat`` already treats as that path's
    comparable content. Any other non-regular entry, or a read failure,
    raises ``_RawContentUnavailable`` -- the caller degrades that one
    change's diff metadata to ``None`` rather than guessing. Bounded at
    ``MAX_FILTER_FREE_READ_BYTES``: a partial read would silently
    misrepresent the file, so an oversized file is refused outright rather
    than truncated.
    """
    candidate = os.path.join(root, *relative_path.split("/")) if relative_path else root
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        # A legitimate, expected state -- an unstaged deletion (worktree
        # side no longer exists) or a not-yet-created path -- never an
        # error. Empty content is exactly the correct "nothing here" side
        # of the comparison, matching what a real `git diff` full-removal
        # hunk already shows.
        return b""
    except OSError as error:
        raise _RawContentUnavailable() from error

    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(candidate)
        except OSError as error:
            raise _RawContentUnavailable() from error
        return target.encode("utf-8", errors="surrogateescape")

    if not stat.S_ISREG(info.st_mode):
        raise _RawContentUnavailable()

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise _RawContentUnavailable() from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _RawContentUnavailable()
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, _FILTER_FREE_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILTER_FREE_READ_BYTES:
                raise _RawContentUnavailable()
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_index_blob_raw(root: str, object_id: Optional[str]) -> bytes:
    """Read an index/HEAD blob's exact, content-addressed bytes via a pure
    object read. Never applies a clean/smudge/process filter -- those only
    run at the worktree<->index conversion boundary, never when an
    already-stored object is read back out. ``object_id`` is always Git's
    own already-validated hex object id (a ``ReviewChange``'s own
    ``index_object_id`` field), never a caller-supplied string. ``None`` or
    Git's all-zero "no object" sentinel returns empty bytes -- the "this
    side does not exist yet" case (e.g. an unstaged addition)."""
    if not object_id or set(object_id) == {"0"}:
        return b""
    result = _run_git(root, ["cat-file", "blob", object_id])
    if result.returncode != 0:
        raise _RawContentUnavailable(f"could not read blob {object_id!r}")
    return result.stdout


def _filter_free_diff(root: str, old_bytes: bytes, new_bytes: bytes, extra_args: Sequence[str]) -> bytes:
    """Compute a ``git diff --no-index`` result between two in-memory byte
    strings with zero possibility of the reviewed repository's own
    ``.git/config`` or ``.gitattributes`` participating: the compared bytes
    are written to a fresh temporary directory that is not inside any Git
    working tree and carries no ``.gitattributes`` of its own, under
    generic names unrelated to the real path, and the invocation's target
    root is that temporary directory -- never the reviewed repository's
    root. ``--no-ext-diff``/``--no-textconv`` are still forced besides,
    matching every other diff-shaped call in this module. ``tempfile``'s
    own portable default location is used -- never a hardcoded path -- so
    this holds on every supported platform.
    """
    tmp_dir = tempfile.mkdtemp(prefix="lumina-review-diff-")
    try:
        old_path = os.path.join(tmp_dir, "a")
        new_path = os.path.join(tmp_dir, "b")
        with open(old_path, "wb") as handle:
            handle.write(old_bytes)
        with open(new_path, "wb") as handle:
            handle.write(new_bytes)
        result = _run_git(
            tmp_dir,
            ["diff", "--no-index", *_DIFF_SAFETY_ARGS, *extra_args, "--", old_path, new_path],
        )
        # --no-index: 0 (identical) or 1 (differ) are both ordinary outcomes
        # here -- never treated as a Git transport failure.
        if result.returncode not in (0, 1):
            raise ReviewProbeError("filter-free diff generation failed")
        return result.stdout
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _parse_no_index_numstat(raw: bytes) -> DiffMetadata:
    """Parse the exactly-one-record ``--no-index --numstat -z`` shape.

    Confirmed empirically (CODING-08R.1) that this is NOT the ordinary
    ``_parse_numstat`` shape ``_capture_diff_metadata`` parses elsewhere:
    ``--no-index`` always compares two DIFFERENTLY-NAMED paths by
    definition, so Git always renders the record in the rename/copy NUL-
    triple shape (``insertions\\tdeletions\\t\\0old_path\\0new_path\\0``, or
    ``-\\t-\\t\\0...`` for binary) regardless of ``--no-renames`` -- that
    flag only suppresses rename PAIRING/DETECTION across a changeset, which
    has no meaning when the two paths being compared are already given
    explicitly. Byte-identical content produces no record at all (Git exits
    0 with empty output), represented here as an explicit zero-change,
    non-binary result -- the comparison itself succeeded, this is not a
    missing-data case.
    """
    if not raw:
        return DiffMetadata(binary=False, insertions=0, deletions=0)
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    # Shape: "<added>\t<deleted>\t" as tokens[0] (trailing tab = empty third
    # tab-field, Git's rename/copy numstat marker), then old_path/new_path
    # as tokens[1]/tokens[2] -- their content is irrelevant here, the caller
    # already knows which ReviewChange this comparison belongs to.
    if len(tokens) != 3 or not tokens[0].endswith(b"\t"):
        raise MalformedReviewStatus("filter-free numstat record had an unexpected shape")
    added_raw, deleted_raw = tokens[0][:-1].split(b"\t", 1)
    if added_raw == b"-" and deleted_raw == b"-":
        return DiffMetadata(binary=True, insertions=None, deletions=None)
    try:
        insertions = int(added_raw)
        deletions = int(deleted_raw)
    except ValueError as error:
        raise MalformedReviewStatus("filter-free numstat counts were not integers") from error
    if insertions < 0 or deletions < 0:
        raise MalformedReviewStatus("filter-free numstat counts were negative")
    return DiffMetadata(binary=False, insertions=insertions, deletions=deletions)


def _filter_free_diff_metadata(root: str, old_bytes: bytes, new_bytes: bytes) -> DiffMetadata:
    """Filter-free equivalent of one path's ``git diff --numstat`` record."""
    patch = _filter_free_diff(root, old_bytes, new_bytes, ("--no-renames", "--numstat", "-z"))
    return _parse_no_index_numstat(patch)


def _capture_unstaged_diff_metadata(root: str, changes: Sequence["ReviewChange"]) -> dict:
    """Filter-free replacement for a bulk unstaged ``git diff --numstat``.

    One filter-free comparison per diffable-and-unstaged change (see the
    module-level "Filter-free content comparison" section above for why
    this cannot be a single bulk Git invocation the way the staged path
    still is). A read failure for one change (race, unsupported type,
    oversized) degrades that single change's entry to absent from the
    returned dict -- callers already treat a missing dict entry as "no
    metadata this pass" (``dict.get`` returns ``None``), exactly like
    today's bulk-numstat path already does for a path a live Git diff
    happened not to report. A genuine race is caught by
    ``capture_review_snapshot``'s own outer A/B stability retry, not by
    anything here.
    """
    result: dict = {}
    for change in changes:
        if not (change.unstaged and change.relation in _DIFFABLE_RELATIONS):
            continue
        try:
            old_bytes = _read_index_blob_raw(root, change.index_object_id)
            new_bytes = _read_worktree_bytes_raw(root, change.path)
            result[change.path] = _filter_free_diff_metadata(root, old_bytes, new_bytes)
        except _RawContentUnavailable:
            continue
    return result


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
    staged_meta = _capture_diff_metadata(root) if needs_staged else {}
    unstaged_meta = _capture_unstaged_diff_metadata(root, changes)

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
