"""Internal review-state applicability, bounding, and retrieval kernel
(CODING-08A2).

Architectural boundary::

    resolved TargetIdentity
            |
    A1 structured Git observation        (core.git_review)
            |
    A2 content-sensitive applicability + bounded snapshot   (this module)
            |
    opaque runtime snapshot reference
            |
    safe bounded file/hunk retrieval with revalidation
            |
    [A3 model tools / A4 Qt later]

This module owns two things A1 deliberately did not: (1) whether a
structured snapshot's *content* -- not just its Git-status classification
-- still matches the live target, and (2) safe, bounded, on-demand
retrieval of actual diff bytes for one file/layer of a snapshot. It
registers no tool, no ToolRegistry adapter, no Qt code, and grants no
stage/unstage/discard/commit/push/merge authority -- exactly like A1, it
only ever answers "what does this look like," never "what may happen to
it next." Authorization (owner/non-owner, model provenance, Coding
profile policy) stays entirely out of scope; a caller must already hold
a resolved ``core.coding_checkpoint.TargetIdentity`` before any function
here is useful, the same precondition A1 imposes.

Why not just widen A1 with a status-only fingerprint?
------------------------------------------------------
CODING-08A0 proved ``status_digest != content fingerprint``: two Git
status observations can be byte-identical (same XY code, same object
IDs) while the underlying dirty-worktree bytes differ, because
``git status`` never reads worktree content for an already-dirty path --
only its own already-computed index/HEAD object IDs. A1 correctly left
that gap unaddressed (out of A1's scope). This module closes it with a
content-sensitive fingerprint (``_fingerprint_content`` /
``ReviewFingerprint``) layered *on top of* A1's structured facts, not a
replacement for them -- ``BoundedSnapshot`` carries the full A1
``ReviewSnapshot`` verbatim, and applicability checks compare both layers
independently (see ``_live_applicability``).

Why not reuse CODING-06A3's ``_validation_state_fingerprint`` wholesale?
-------------------------------------------------------------------------
``core.coding_checkpoint_observation._validation_state_fingerprint`` is
already a content-sensitive Git fingerprint, but it carries two
properties inappropriate for a generic review target:

1. It is coupled to a durable ``ProjectContext`` binding
   (``verify_project_binding``) -- a review target here is only ever an
   already-resolved ``TargetIdentity``, with no Project concept at all.
2. Its per-path content reader, ``_dirty_path_state``, raises
   ``UnstableCapture`` -- unconditionally, with no retry benefit, since
   it is not actually a race -- for *any* Git-visible dirty path that is
   not a regular file or a symlink (see
   ``core/coding_checkpoint_observation.py:718-724``). A submodule's
   outer-repository entry is a directory on disk; a single dirty
   submodule anywhere among the changed paths therefore aborts the
   *entire* fingerprint capture for the whole checkpoint, unrelated
   files included. That is precisely the failure mode section 3 of the
   CODING-08A2 task spec forbids: "It must not make unrelated repository
   review impossible merely because [a dirty submodule] exists." This
   module's per-path fingerprinting (``_fingerprint_one_change``)
   degrades a submodule (or any other unsupported entry type) to a
   local, truthful, per-path omission instead -- see the "Completeness"
   section below.

This module's fingerprint is also *more* surgical than a naive
port of CODING-06A3's approach would be: a purely-staged path (XY
status's second character is ``.``) needs no filesystem read at all,
because ``worktree bytes == index blob`` by definition whenever
``unstaged`` is False, and the index blob's content is already
Git-content-addressed by A1's own ``index_object_id`` field (folded
verbatim into the fingerprint document). Filesystem bytes are only ever
read for the *worktree* side of an unstaged or untracked path -- see
``_fingerprint_one_change``.

Completeness is part of truth
------------------------------
``ReviewFingerprint.content_complete`` is False, and
``ReviewFingerprint.omissions`` is non-empty, whenever *any* change could
not be safely content-fingerprinted within this module's bounds (a
nested submodule, an unmerged conflict entry, a file over the byte cap,
too many changed paths to fingerprint at all). Applicability then reports
``CURRENT_METADATA_ONLY`` rather than ``CURRENT`` -- never a bare
boolean standing in for five genuinely different truths. See
``resolve_review_applicability`` and the module-level ``CURRENT`` /
``CURRENT_METADATA_ONLY`` / ``STALE`` / ``TARGET_UNAVAILABLE`` / ``ERROR``
constants.

Stable capture
---------------
``capture_snapshot`` follows the same target/state-A -> observation ->
target/state-B capture-and-compare shape A1 and CODING-05A2 already use,
retried once, failing closed (``UnstableSnapshotCapture``) on a second
mismatch -- never returning a snapshot assembled from two different
repository states. Unlike A1, the A/B comparison here spans *three*
independent layers (live identity + instance facts, A1's structured
facts, and this module's content fingerprint), because a status-only
comparison alone cannot see the A0 race (dirty bytes rewritten without
status changing) -- see ``capture_snapshot``'s docstring for the exact
comparison.

Exact target-instance identity
--------------------------------
``core.coding_checkpoint.TargetIdentity.target_key`` is computed from
(kind, canonical root, git_common_dir *path string*, and the *worktree
root directory's* own filesystem identity) -- see
``resolve_target_identity``'s docstring. That means an in-place
``rm -rf .git && git init`` at the same path produces a byte-identical
``target_key``: the worktree root directory's own inode never changed,
and ``git_common_dir`` resolves to the same absolute path string both
times, even though the underlying repository (history, objects,
everything) is completely different. ``InstanceFacts`` adds the one
signal that *does* change in that scenario -- the git_common_dir's own
(device, inode) pair, which is necessarily fresh every time ``git init``
recreates that directory -- alongside the worktree root's own filesystem
identity (which independently catches a linked-worktree directory being
removed and recreated at the same path while the shared main repository
is untouched). Two signals, one for each replacement vector; see
``_resolve_live``.

Snapshot runtime identity
---------------------------
``capture_snapshot`` registers its result under an opaque
``snapshot_ref`` in an in-process registry (``_registry``), mirroring
``core.worktree_manager``'s own opaque-ID registry pattern
(``secrets.token_hex`` under a ``threading.RLock``). A ref is an
ergonomic selector only -- it grants no target authority, is
process-lifetime-only (no durable persistence, no adoption across a
restart), and is bounded both by count (``MAX_SNAPSHOT_COUNT``, oldest
evicted first) and by age (``SNAPSHOT_TTL_SECONDS``, swept opportunistically
on registry access). Once ``resolve_review_applicability`` observes a
ref's live state as STALE, that ref latches: it can never again silently
report CURRENT/CURRENT_METADATA_ONLY, even if live content happens to
transiently match again -- see the ``ever_stale`` handling in
``resolve_review_applicability``. TTL eviction is a distinct, coarser
bound than the stale latch: an evicted ref simply becomes unknown
(``UnknownSnapshot``), not "stale" -- this module never claims a durable
memory of every ref it ever issued, only that a *still-registered* ref
never regains a clean bill of health once proven dirty.

Bounded retrieval
-------------------
``retrieve_review_file`` revalidates applicability *before and after*
capturing bounded content, returning no content at all if either check
fails (see section 13 of the task spec) -- refresh and retrieval stay
two different operations, never conflated. Diff content is bounded at
the hunk level, never mid-line/mid-hunk (``MAX_HUNK_BYTES``,
``MAX_FILE_DIFF_BYTES``); an oversized hunk is omitted as one whole unit,
never sliced-and-marked-complete (see ``_bound_patch``). Every
patch-producing Git invocation this module makes reuses A1's own
``_GLOBAL_SAFE_ARGS`` / ``_DIFF_SAFETY_ARGS`` tuples *by reference*
(imported, never re-declared) so there is exactly one place
``--no-ext-diff``/``--no-textconv`` can ever drift -- see
``_run_diff_patch``. Unlike A1's ``--numstat`` calls (which never invoke
either escape hatch regardless of the flags, per A1's own module
docstring), this module's calls request real patch text, which *can*
invoke both hooks when unguarded -- the safety-flag sentinel tests
against this module's retrieval path are therefore genuinely
load-bearing, not merely structural.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

import core.coding_checkpoint as checkpoint_store
import core.git_review as git_review
from core.git_read import GitReadError, run_bounded_git

# ---------------------------------------------------------------------------
# Named limits. Every cap below is a deliberate bound on a specific kind of
# work, not a value chosen to make a particular test pass. See the module
# docstring section this constant supports for the reasoning.
# ---------------------------------------------------------------------------

# Identity/instance re-probes: a couple of path lines, generous headroom.
IDENTITY_GIT_TIMEOUT_SECONDS = 5
MAX_IDENTITY_STDOUT_BYTES = 64 * 1024
MAX_IDENTITY_STDERR_BYTES = 16 * 1024

# Patch-producing retrieval calls: real file content, so a much larger hard
# transport ceiling than identity probes -- this is a backstop against a
# truly pathological single invocation, distinct from the much smaller
# *soft*, gracefully-degrading per-file/per-hunk bounds below.
RETRIEVAL_GIT_TIMEOUT_SECONDS = 5
MAX_RETRIEVAL_STDOUT_BYTES = 32 * 1024 * 1024
MAX_RETRIEVAL_STDERR_BYTES = 16 * 1024

# Content fingerprinting: local filesystem reads, no subprocess involved.
# A single dirty file's bytes are read in full up to this cap; beyond it,
# the path is omitted from the fingerprint rather than read partially (a
# partial hash would be worse than no hash -- it would silently ignore a
# change past the cutoff).
MAX_FINGERPRINT_FILE_BYTES = 16 * 1024 * 1024
_FINGERPRINT_READ_CHUNK_BYTES = 256 * 1024
# Bounds total fingerprinting work per snapshot regardless of per-file size
# -- a review touching more paths than this degrades the whole fingerprint
# to metadata-only rather than hashing thousands of files inline.
MAX_FINGERPRINT_PATHS = 4000
# Matches the analogous bound in core.coding_checkpoint_observation
# (MAX_SYMLINK_TARGET_CHARS) for consistency; kept as this module's own
# named constant rather than a cross-domain import (see module docstring
# on why CODING-06A3's fingerprint code is mirrored, not imported).
MAX_SYMLINK_TARGET_CHARS = 4096

# Bounded diff retrieval: sized for human/model-consumable review content,
# not raw storage -- generous relative to any ordinary hand-written hunk,
# small relative to the hard transport ceiling above.
MAX_HUNK_BYTES = 64 * 1024
MAX_FILE_DIFF_BYTES = 512 * 1024

# In-memory snapshot registry: a handful of concurrent reviews per process,
# each with a bounded runtime lifetime. Neither bound is chosen to be tight
# -- both exist purely so this module cannot accumulate state without limit
# across a long-running process.
MAX_SNAPSHOT_COUNT = 32
SNAPSHOT_TTL_SECONDS = 1800

REVIEW_FINGERPRINT_ALGO_VERSION = 1

# ---------------------------------------------------------------------------
# Applicability states. Five genuinely different truths, never collapsed
# onto one boolean -- see the module docstring's "Completeness is part of
# truth" section.
# ---------------------------------------------------------------------------
CURRENT = "current"
CURRENT_METADATA_ONLY = "current_metadata_only"
STALE = "stale"
TARGET_UNAVAILABLE = "target_unavailable"
ERROR = "error"

LAYER_STAGED = "staged"
LAYER_UNSTAGED = "unstaged"
_VALID_LAYERS = (LAYER_STAGED, LAYER_UNSTAGED)

# Machine-readable omission reasons. Every place content/fingerprint
# completeness goes False records one of these, never free-form prose --
# see section 4 of the task spec ("a machine-readable omission reason").
REASON_UNMERGED = "unmerged_content_requires_specialized_view"
REASON_SUBMODULE = "nested_submodule_content_not_captured"
REASON_TOO_LARGE = "file_exceeds_fingerprint_byte_limit"
REASON_UNSTABLE = "content_unstable_during_fingerprint_capture"
REASON_UNSUPPORTED_TYPE = "unexpected_filesystem_entry_type"
REASON_TOO_MANY_PATHS = "too_many_changed_paths_for_fingerprint"
REASON_HUNK_TOO_LARGE = "hunk_exceeds_byte_limit"
REASON_FILE_BUDGET = "file_diff_byte_limit_reached"
REASON_MALFORMED_HUNK = "malformed_hunk_header"


class SnapshotError(git_review.GitReviewError):
    """Base class for every new error this module raises.

    Deliberately a specialization of A1's own ``GitReviewError`` -- unlike
    ``core.coding_checkpoint_observation`` (a genuinely separate domain
    from A1's Git review kernel), this module *is* the review kernel's
    next architectural layer, so its errors extend A1's hierarchy rather
    than starting a parallel one. Target-mismatch and Git-transport
    failures reuse A1's own ``ReviewTargetError`` / ``ReviewProbeError``
    directly rather than being redeclared here.
    """


class UnstableSnapshotCapture(SnapshotError):
    """Repository or content changed materially during snapshot capture,
    even after one retry. No snapshot mixing two different repository
    states is ever returned."""


class UnknownSnapshot(SnapshotError):
    """``snapshot_ref`` does not name a live runtime snapshot -- never
    issued, or evicted by the count/TTL bound. Never conflated with
    STALE: this is "we don't have this," not "we checked and it no
    longer matches."""


class UnknownChange(SnapshotError):
    """``change_id`` does not name a change record within this
    snapshot's A1 structured facts."""


class RetrievalLayerError(SnapshotError):
    """The requested layer/relation combination has nothing retrievable
    for this change -- a caller-usage error (wrong layer, unsupported
    relation), never a content-availability outcome. Content that is
    legitimately unavailable (binary, unmerged, submodule, oversized)
    is represented in a successful ``FileDiffResult``, not raised."""


# ---------------------------------------------------------------------------
# Data model.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstanceFacts:
    """Exact worktree-instance facts beyond ``TargetIdentity.target_key``,
    needed to reject a same-path repository replacement. See the module
    docstring's "Exact target-instance identity" section for why both
    signals are necessary."""
    git_common_dir: str
    git_common_dir_filesystem: Optional[tuple]
    worktree_root_filesystem: Optional[tuple]


@dataclass(frozen=True)
class PathFingerprint:
    """One Git-visible change's content-fingerprint contribution.

    ``state`` is one of: "content" (worktree bytes hashed), "symlink"
    (own target text hashed, never followed), "missing" (deleted or
    vanished between measurement and read), "index_content_addressed"
    (purely-staged path; content already fully represented by A1's
    ``index_object_id``, no filesystem read performed), "unmerged",
    "submodule", or "omitted" (unsupported entry type, over the byte
    cap, or unstable across a local retry -- see ``omission_reason``).
    """
    path: str
    state: str
    sha256: Optional[str] = None
    symlink_target: Optional[str] = None
    omission_reason: Optional[str] = None


@dataclass(frozen=True)
class ReviewFingerprint:
    algo_version: int
    fingerprint: Optional[str]
    content_complete: bool
    omissions: tuple
    path_fingerprints: tuple


@dataclass(frozen=True)
class BoundedSnapshot:
    """One immutable, stable capture combining A1's structured Git
    observation with A2's content fingerprint. Registered under an
    opaque ``snapshot_ref`` by ``capture_snapshot``."""
    identity: checkpoint_store.TargetIdentity
    instance: InstanceFacts
    review: "git_review.ReviewSnapshot"
    fingerprint: ReviewFingerprint
    captured_at: str
    captured_at_monotonic: float = field(compare=False)


@dataclass(frozen=True)
class SnapshotHandle:
    snapshot_ref: str
    snapshot: BoundedSnapshot


@dataclass(frozen=True)
class ReviewApplicability:
    state: str
    reasons: tuple


@dataclass(frozen=True)
class DiffHunkHeader:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section_heading: str


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "context" | "add" | "remove" | "no_newline"
    text: str


@dataclass(frozen=True)
class DiffHunk:
    header: Optional[DiffHunkHeader]
    lines: tuple
    byte_size: int
    omitted: bool
    omission_reason: Optional[str]


@dataclass(frozen=True)
class FileDiffResult:
    change_id: str
    path: str
    layer: str
    binary: bool
    hunks: tuple
    complete: bool
    omitted_hunks: int
    omission_reason: Optional[str]
    total_bytes: int
    next_cursor: Optional[int]


@dataclass(frozen=True)
class RetrievalOutcome:
    status: str
    file: Optional[FileDiffResult]
    applicability: ReviewApplicability


@dataclass
class _RegistryEntry:
    """Mutable wrapper around one immutable BoundedSnapshot -- only
    ``ever_stale`` is ever mutated, and only while holding
    ``_registry_lock``. See the module docstring's sticky-stale latch."""
    snapshot: BoundedSnapshot
    ever_stale: bool = False


_registry: "dict[str, _RegistryEntry]" = {}
_registry_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Git transport. Deliberately its own thin wrapper (not git_review's
# private ``_run_git``) with its own byte/time bounds appropriate to this
# module's calls -- but the *safety flags themselves*
# (``_GLOBAL_SAFE_ARGS`` / ``_DIFF_SAFETY_ARGS``) are imported by reference
# from core.git_review, never re-declared, so there is exactly one place
# either can drift. See the module docstring's "Bounded retrieval" section.
# ---------------------------------------------------------------------------

def _run_git(root: str, args: Sequence[str], *, identity_probe: bool = False):
    try:
        if identity_probe:
            return run_bounded_git(
                root, (*git_review._GLOBAL_SAFE_ARGS, *args),
                timeout=IDENTITY_GIT_TIMEOUT_SECONDS,
                max_stdout_bytes=MAX_IDENTITY_STDOUT_BYTES,
                max_stderr_bytes=MAX_IDENTITY_STDERR_BYTES,
            )
        return run_bounded_git(
            root, (*git_review._GLOBAL_SAFE_ARGS, *args),
            timeout=RETRIEVAL_GIT_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_RETRIEVAL_STDOUT_BYTES,
            max_stderr_bytes=MAX_RETRIEVAL_STDERR_BYTES,
        )
    except GitReadError as error:
        raise git_review.ReviewProbeError(str(error)) from error


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


# ---------------------------------------------------------------------------
# Live identity + instance-fact resolution. Mirrors git_review's own
# ``_resolve_live_identity`` rev-parse call rather than importing its
# private helper, but additionally captures the git_common_dir/worktree
# filesystem identity git_review has no reason to need. See the module
# docstring's "Exact target-instance identity" section.
# ---------------------------------------------------------------------------

def _filesystem_instance(path: str) -> Optional[tuple]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    ino = getattr(info, "st_ino", 0)
    dev = getattr(info, "st_dev", 0)
    if not ino and not dev:
        return None
    return (dev, ino)


def _resolve_live(root: str) -> tuple:
    canonical_requested = os.path.realpath(os.path.expanduser(root))
    result = _run_git(
        canonical_requested,
        ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
        identity_probe=True,
    )
    if result.returncode != 0:
        raise git_review.ReviewTargetError("target is not a live Git working tree")
    lines = result.stdout.splitlines()
    if len(lines) != 2 or not lines[0] or not lines[1]:
        raise git_review.ReviewProbeError("Git target discovery returned an invalid shape")
    toplevel = os.path.realpath(_decode(lines[0]))
    common_dir = os.path.realpath(_decode(lines[1]))
    identity = checkpoint_store._build_target_identity("git", toplevel, common_dir)
    instance = InstanceFacts(
        git_common_dir=common_dir,
        git_common_dir_filesystem=_filesystem_instance(common_dir),
        worktree_root_filesystem=_filesystem_instance(toplevel),
    )
    return identity, instance


def _identity_matches(expected: checkpoint_store.TargetIdentity, live: checkpoint_store.TargetIdentity) -> bool:
    return (
        live.kind == expected.kind
        and live.canonical_root == expected.canonical_root
        and live.target_key == expected.target_key
    )


def _review_equal(a: "git_review.ReviewSnapshot", b: "git_review.ReviewSnapshot") -> bool:
    """Structural equality ignoring ``captured_at`` (which always differs
    between two calls even when nothing else did)."""
    return (
        a.identity == b.identity
        and a.head == b.head
        and a.branch == b.branch
        and a.detached == b.detached
        and a.unborn == b.unborn
        and a.operation_state == b.operation_state
        and a.changes == b.changes
    )


# ---------------------------------------------------------------------------
# Content fingerprinting.
# ---------------------------------------------------------------------------

class _ContentRace(Exception):
    """A regular file's bytes were observed to change mid-read. Retried
    once by the caller; a second occurrence degrades to a local omission
    (see ``_fingerprint_worktree_path``) rather than aborting the whole
    fingerprint -- only ``capture_snapshot``'s outer A/B comparison ever
    raises ``UnstableSnapshotCapture``."""


class _TooLarge(Exception):
    """A regular file exceeded MAX_FINGERPRINT_FILE_BYTES. Never a race;
    never retried."""


def _stat_signature(info) -> tuple:
    return (
        getattr(info, "st_dev", 0), getattr(info, "st_ino", 0),
        info.st_mode, info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
    )


def _hash_regular_once(path: str, before) -> str:
    """Mirrors core.coding_checkpoint_observation._hash_regular_file_once's
    O_NOFOLLOW-open / four-way-stat-consistency pattern -- reimplemented
    locally rather than imported, matching this module's own established
    mirror-not-import convention for cross-domain safety primitives."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _ContentRace() from error
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise _ContentRace()
        if opened_before.st_size > MAX_FINGERPRINT_FILE_BYTES:
            raise _TooLarge()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _FINGERPRINT_READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = os.lstat(path)
    except OSError as error:
        raise _ContentRace() from error
    signatures = {
        _stat_signature(before), _stat_signature(opened_before),
        _stat_signature(opened_after), _stat_signature(path_after),
    }
    if len(signatures) != 1 or not stat.S_ISREG(path_after.st_mode):
        raise _ContentRace()
    return digest.hexdigest()


def _fingerprint_worktree_path(root: str, relative_path: str, omissions: set) -> PathFingerprint:
    """Content signal for one worktree-side path. Never follows a
    symlink -- ``os.readlink`` reads the link's own target text, the
    target file is never opened. See the module docstring's "Symlinks"
    coverage and mutation F in the CODING-08A2 handoff report."""
    candidate = os.path.join(root, *relative_path.split("/")) if relative_path else root
    for attempt in range(2):
        try:
            before = os.lstat(candidate)
        except OSError:
            return PathFingerprint(path=relative_path, state="missing")

        if stat.S_ISLNK(before.st_mode):
            try:
                target = os.readlink(candidate)
            except OSError:
                return PathFingerprint(path=relative_path, state="missing")
            target_hash = hashlib.sha256(
                target.encode("utf-8", errors="surrogateescape")
            ).hexdigest()
            display_target = (
                target if len(target) <= MAX_SYMLINK_TARGET_CHARS
                else target[:MAX_SYMLINK_TARGET_CHARS]
            )
            return PathFingerprint(
                path=relative_path, state="symlink",
                sha256=target_hash, symlink_target=display_target,
            )

        if not stat.S_ISREG(before.st_mode):
            omissions.add(REASON_UNSUPPORTED_TYPE)
            return PathFingerprint(
                path=relative_path, state="omitted", omission_reason=REASON_UNSUPPORTED_TYPE,
            )

        try:
            digest = _hash_regular_once(candidate, before)
            return PathFingerprint(path=relative_path, state="content", sha256=digest)
        except _TooLarge:
            omissions.add(REASON_TOO_LARGE)
            return PathFingerprint(
                path=relative_path, state="omitted", omission_reason=REASON_TOO_LARGE,
            )
        except _ContentRace:
            if attempt == 1:
                omissions.add(REASON_UNSTABLE)
                return PathFingerprint(
                    path=relative_path, state="omitted", omission_reason=REASON_UNSTABLE,
                )
    raise AssertionError("unreachable worktree fingerprint retry state")


def _fingerprint_one_change(root: str, change: "git_review.ReviewChange", omissions: set) -> PathFingerprint:
    if change.relation == "unmerged":
        omissions.add(REASON_UNMERGED)
        return PathFingerprint(path=change.path, state="unmerged", omission_reason=REASON_UNMERGED)
    if change.submodule.is_submodule:
        omissions.add(REASON_SUBMODULE)
        return PathFingerprint(path=change.path, state="submodule", omission_reason=REASON_SUBMODULE)
    if change.untracked or change.unstaged:
        return _fingerprint_worktree_path(root, change.path, omissions)
    # Purely staged (not unstaged, not untracked): worktree bytes equal the
    # index blob by definition, and that blob is already content-addressed
    # by Git itself -- change.index_object_id, already folded verbatim into
    # the fingerprint document below. No filesystem read needed or performed.
    return PathFingerprint(path=change.path, state="index_content_addressed")


def _change_as_doc(change: "git_review.ReviewChange") -> dict:
    return {
        "record_type": change.record_type,
        "relation": change.relation,
        "path": change.path,
        "original_path": change.original_path,
        "relation_score": change.relation_score,
        "xy_status": change.xy_status,
        "staged": change.staged,
        "unstaged": change.unstaged,
        "untracked": change.untracked,
        "submodule": {
            "is_submodule": change.submodule.is_submodule,
            "commit_changed": change.submodule.commit_changed,
            "has_tracked_changes": change.submodule.has_tracked_changes,
            "has_untracked_changes": change.submodule.has_untracked_changes,
        },
        "head_mode": change.head_mode,
        "index_mode": change.index_mode,
        "worktree_mode": change.worktree_mode,
        "head_object_id": change.head_object_id,
        "index_object_id": change.index_object_id,
        "unmerged_stages": [
            {"stage": s.stage, "mode": s.mode, "object_id": s.object_id}
            for s in change.unmerged_stages
        ],
    }


def _fingerprint_content(
    identity: checkpoint_store.TargetIdentity,
    instance: InstanceFacts,
    review: "git_review.ReviewSnapshot",
) -> ReviewFingerprint:
    if len(review.changes) > MAX_FINGERPRINT_PATHS:
        return ReviewFingerprint(
            algo_version=REVIEW_FINGERPRINT_ALGO_VERSION, fingerprint=None,
            content_complete=False, omissions=(REASON_TOO_MANY_PATHS,),
            path_fingerprints=(),
        )

    omissions: set = set()
    entries = tuple(sorted(
        (
            _fingerprint_one_change(identity.canonical_root, change, omissions)
            for change in review.changes
        ),
        key=lambda item: item.path,
    ))
    complete = not omissions

    fingerprint_hex = None
    if complete:
        document = {
            "v": REVIEW_FINGERPRINT_ALGO_VERSION,
            "target_key": identity.target_key,
            "canonical_root": identity.canonical_root,
            "git_common_dir": instance.git_common_dir,
            "git_common_dir_fs": (
                list(instance.git_common_dir_filesystem)
                if instance.git_common_dir_filesystem else None
            ),
            "worktree_root_fs": (
                list(instance.worktree_root_filesystem)
                if instance.worktree_root_filesystem else None
            ),
            "head": review.head,
            "branch": review.branch,
            "detached": review.detached,
            "unborn": review.unborn,
            "operation_state": list(review.operation_state),
            "changes": [_change_as_doc(change) for change in review.changes],
            "entries": [
                {
                    "path": e.path, "state": e.state,
                    "sha256": e.sha256, "symlink_target": e.symlink_target,
                }
                for e in entries
            ],
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        fingerprint_hex = hashlib.sha256(encoded).hexdigest()

    return ReviewFingerprint(
        algo_version=REVIEW_FINGERPRINT_ALGO_VERSION,
        fingerprint=fingerprint_hex,
        content_complete=complete,
        omissions=tuple(sorted(omissions)),
        path_fingerprints=entries,
    )


def _fingerprint_equal(a: ReviewFingerprint, b: ReviewFingerprint) -> bool:
    return (
        a.path_fingerprints == b.path_fingerprints
        and a.content_complete == b.content_complete
        and a.omissions == b.omissions
    )


# ---------------------------------------------------------------------------
# Snapshot registry.
# ---------------------------------------------------------------------------

def _evict_expired_locked() -> None:
    now = time.monotonic()
    expired = [
        ref for ref, entry in _registry.items()
        if now - entry.snapshot.captured_at_monotonic > SNAPSHOT_TTL_SECONDS
    ]
    for ref in expired:
        del _registry[ref]


def _register(snapshot: BoundedSnapshot) -> SnapshotHandle:
    with _registry_lock:
        _evict_expired_locked()
        while len(_registry) >= MAX_SNAPSHOT_COUNT:
            oldest_ref = next(iter(_registry))
            del _registry[oldest_ref]
        for _ in range(128):
            candidate = "revsnap-" + secrets.token_hex(12)
            if candidate not in _registry:
                ref = candidate
                break
        else:
            raise SnapshotError("could not allocate a unique snapshot reference")
        _registry[ref] = _RegistryEntry(snapshot=snapshot)
    return SnapshotHandle(snapshot_ref=ref, snapshot=snapshot)


# ---------------------------------------------------------------------------
# Public: capture.
# ---------------------------------------------------------------------------

def capture_snapshot(identity: checkpoint_store.TargetIdentity) -> SnapshotHandle:
    """Capture one stable, content-fingerprinted review snapshot and
    register it under a fresh opaque ``snapshot_ref``.

    The whole target/state-A -> (A1 structured observation + A2 content
    fingerprint) -> target/state-B sequence is retried once on any
    mismatch across identity, instance facts, A1's structured facts, or
    this module's content fingerprint. A second inconsistency raises
    ``UnstableSnapshotCapture`` rather than returning or registering a
    snapshot assembled from two different repository states. Comparing
    the *content fingerprint* (not just A1's structured facts) between A
    and B is what specifically catches CODING-08A0's race: dirty bytes
    rewritten with no Git-status-visible change at all.
    """
    if not isinstance(identity, checkpoint_store.TargetIdentity):
        raise git_review.ReviewTargetError("identity must be an already-resolved TargetIdentity")
    if identity.kind != "git":
        raise git_review.ReviewTargetError("review snapshot kernel only observes Git targets")

    for attempt in range(2):
        live_identity_a, instance_a = _resolve_live(identity.canonical_root)
        if not _identity_matches(identity, live_identity_a):
            raise git_review.ReviewTargetError(
                "supplied target identity no longer matches the live repository"
            )
        review_a = git_review.capture_review_snapshot(identity)
        fingerprint_a = _fingerprint_content(identity, instance_a, review_a)

        live_identity_b, instance_b = _resolve_live(identity.canonical_root)
        if not _identity_matches(identity, live_identity_b):
            raise git_review.ReviewTargetError(
                "supplied target identity no longer matches the live repository"
            )
        review_b = git_review.capture_review_snapshot(identity)
        fingerprint_b = _fingerprint_content(identity, instance_b, review_b)

        if (
            live_identity_a == live_identity_b
            and instance_a == instance_b
            and _review_equal(review_a, review_b)
            and _fingerprint_equal(fingerprint_a, fingerprint_b)
        ):
            snapshot = BoundedSnapshot(
                identity=live_identity_b,
                instance=instance_b,
                review=review_b,
                fingerprint=fingerprint_b,
                captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                captured_at_monotonic=time.monotonic(),
            )
            return _register(snapshot)
        if attempt == 1:
            raise UnstableSnapshotCapture(
                "repository or content changed materially during snapshot capture"
            )
    raise AssertionError("unreachable snapshot capture retry state")


# ---------------------------------------------------------------------------
# Public: applicability.
# ---------------------------------------------------------------------------

def _live_applicability(snapshot: BoundedSnapshot) -> ReviewApplicability:
    try:
        live_identity, live_instance = _resolve_live(snapshot.identity.canonical_root)
    except git_review.ReviewTargetError:
        return ReviewApplicability(TARGET_UNAVAILABLE, ("target_no_longer_resolves",))
    except git_review.GitReviewError:
        return ReviewApplicability(ERROR, ("target_probe_failed",))

    if (
        not _identity_matches(snapshot.identity, live_identity)
        or live_instance != snapshot.instance
    ):
        return ReviewApplicability(TARGET_UNAVAILABLE, ("target_instance_replaced",))

    try:
        live_review = git_review.capture_review_snapshot(snapshot.identity)
    except git_review.ReviewTargetError:
        return ReviewApplicability(TARGET_UNAVAILABLE, ("target_no_longer_resolves",))
    except git_review.UnstableReviewCapture:
        return ReviewApplicability(ERROR, ("live_state_unstable",))
    except git_review.GitReviewError:
        return ReviewApplicability(ERROR, ("live_probe_failed",))

    if not _review_equal(live_review, snapshot.review):
        return ReviewApplicability(STALE, ("structured_state_changed",))

    live_fingerprint = _fingerprint_content(snapshot.identity, live_instance, live_review)
    if live_fingerprint.path_fingerprints != snapshot.fingerprint.path_fingerprints:
        return ReviewApplicability(STALE, ("content_changed",))

    if snapshot.fingerprint.content_complete:
        return ReviewApplicability(CURRENT, ())
    return ReviewApplicability(CURRENT_METADATA_ONLY, snapshot.fingerprint.omissions)


def get_snapshot(snapshot_ref: str) -> BoundedSnapshot:
    """Return the currently-registered ``BoundedSnapshot`` for ``snapshot_ref``
    exactly as captured -- never re-verified against live state here (see
    ``resolve_review_applicability`` for that). Mirrors the identical
    registry-lookup path ``resolve_review_applicability``/
    ``retrieve_review_file`` already use internally; this is the same
    read-only lookup made public so a caller (A3) can render a snapshot's
    own captured facts without duplicating the registry itself."""
    with _registry_lock:
        _evict_expired_locked()
        entry = _registry.get(snapshot_ref)
        if entry is None:
            raise UnknownSnapshot(f"unknown or expired snapshot reference: {snapshot_ref!r}")
        return entry.snapshot


def resolve_review_applicability(snapshot_ref: str) -> ReviewApplicability:
    """Non-mutating: does this already-captured snapshot still match live
    reality right now? Never recaptures, never re-registers. Once this
    reports STALE for a given ref, that ref latches -- see the module
    docstring's "Snapshot runtime identity" section.
    """
    with _registry_lock:
        _evict_expired_locked()
        entry = _registry.get(snapshot_ref)
        if entry is None:
            raise UnknownSnapshot(f"unknown or expired snapshot reference: {snapshot_ref!r}")
        snapshot = entry.snapshot
        latched_stale = entry.ever_stale

    result = _live_applicability(snapshot)
    if latched_stale and result.state in (CURRENT, CURRENT_METADATA_ONLY):
        result = ReviewApplicability(STALE, result.reasons + ("previously_observed_stale",))

    if result.state == STALE:
        with _registry_lock:
            existing = _registry.get(snapshot_ref)
            if existing is not None:
                existing.ever_stale = True
    return result


# ---------------------------------------------------------------------------
# Change identity.
# ---------------------------------------------------------------------------

def review_change_id(change: "git_review.ReviewChange") -> str:
    """Stable, deterministic ID for one change record within a snapshot.
    Public so a caller can compute IDs directly from
    ``BoundedSnapshot.review.changes`` without a separate listing API --
    a snapshot's changes are already fully enumerable via A1's own
    structured facts; A2 only needs to name one of them."""
    basis = f"{change.record_type}\0{change.relation}\0{change.path}\0{change.original_path or ''}"
    return hashlib.sha256(basis.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]


def _find_change(review: "git_review.ReviewSnapshot", change_id: str):
    for change in review.changes:
        if review_change_id(change) == change_id:
            return change
    return None


# ---------------------------------------------------------------------------
# Bounded diff retrieval.
# ---------------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_BINARY_MARKER = b"Binary files "


def _split_hunks(patch_bytes: bytes) -> list:
    """Split raw patch output for one file into raw per-hunk byte blocks.

    Lines before the first ``@@`` header (``diff --git``/``index``/
    ``---``/``+++``/mode lines) are discarded outright -- filename
    identity comes from the caller-supplied ``change.path`` (already
    safely captured by A1 via NUL-delimited porcelain parsing), never
    re-derived from these potentially C-quoted header lines. See the
    module docstring's "Literal paths" reasoning.
    """
    lines = patch_bytes.split(b"\n")
    blocks = []
    current = None
    for line in lines:
        if line.startswith(b"@@ "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return [b"\n".join(block) for block in blocks]


def _parse_header_only(raw: bytes) -> Optional[DiffHunkHeader]:
    first_line = raw.split(b"\n", 1)[0]
    match = _HUNK_HEADER_RE.match(first_line)
    if not match:
        return None
    old_start, old_count, new_start, new_count, heading = match.groups()
    return DiffHunkHeader(
        old_start=int(old_start),
        old_count=int(old_count) if old_count is not None else 1,
        new_start=int(new_start),
        new_count=int(new_count) if new_count is not None else 1,
        section_heading=_decode(heading).strip(),
    )


def _parse_hunk(raw: bytes) -> Optional[DiffHunk]:
    header = _parse_header_only(raw)
    if header is None:
        return None
    lines = raw.split(b"\n")[1:]
    parsed_lines = []
    for line in lines:
        if line == b"":
            continue
        marker = line[:1]
        if marker == b"+":
            kind = "add"
        elif marker == b"-":
            kind = "remove"
        elif marker == b" ":
            kind = "context"
        elif marker == b"\\":
            kind = "no_newline"
        else:
            return None
        parsed_lines.append(DiffLine(kind=kind, text=_decode(line[1:])))
    return DiffHunk(
        header=header, lines=tuple(parsed_lines), byte_size=len(raw),
        omitted=False, omission_reason=None,
    )


def _omitted_hunk(raw: bytes, reason: str) -> DiffHunk:
    return DiffHunk(
        header=_parse_header_only(raw), lines=(), byte_size=len(raw),
        omitted=True, omission_reason=reason,
    )


def _bound_patch(
    patch_bytes: bytes, change_id: str, path: str, layer: str, start_hunk_index: int,
) -> FileDiffResult:
    raw_hunks = _split_hunks(patch_bytes)
    if not raw_hunks and _BINARY_MARKER in patch_bytes:
        return FileDiffResult(
            change_id=change_id, path=path, layer=layer, binary=True,
            hunks=(), complete=True, omitted_hunks=0,
            omission_reason=None, total_bytes=0, next_cursor=None,
        )

    hunks = []
    omitted_hunks = 0
    total_bytes = 0
    next_cursor = None
    budget_exhausted = False
    omission_reason = None

    for index, raw in enumerate(raw_hunks):
        if index < start_hunk_index:
            continue
        size = len(raw)
        if size > MAX_HUNK_BYTES:
            hunks.append(_omitted_hunk(raw, REASON_HUNK_TOO_LARGE))
            omitted_hunks += 1
            omission_reason = omission_reason or REASON_HUNK_TOO_LARGE
            continue
        if budget_exhausted or total_bytes + size > MAX_FILE_DIFF_BYTES:
            budget_exhausted = True
            if next_cursor is None:
                next_cursor = index
            hunks.append(_omitted_hunk(raw, REASON_FILE_BUDGET))
            omitted_hunks += 1
            omission_reason = omission_reason or REASON_FILE_BUDGET
            continue
        parsed = _parse_hunk(raw)
        if parsed is None:
            hunks.append(_omitted_hunk(raw, REASON_MALFORMED_HUNK))
            omitted_hunks += 1
            omission_reason = omission_reason or REASON_MALFORMED_HUNK
            continue
        hunks.append(parsed)
        total_bytes += size

    return FileDiffResult(
        change_id=change_id, path=path, layer=layer, binary=False,
        hunks=tuple(hunks), complete=(omitted_hunks == 0), omitted_hunks=omitted_hunks,
        omission_reason=omission_reason, total_bytes=total_bytes, next_cursor=next_cursor,
    )


def _omitted_file(change_id: str, path: str, layer: str, reason: str) -> FileDiffResult:
    return FileDiffResult(
        change_id=change_id, path=path, layer=layer, binary=False,
        hunks=(), complete=False, omitted_hunks=0,
        omission_reason=reason, total_bytes=0, next_cursor=None,
    )


def _binary_file(change_id: str, path: str, layer: str) -> FileDiffResult:
    return FileDiffResult(
        change_id=change_id, path=path, layer=layer, binary=True,
        hunks=(), complete=True, omitted_hunks=0,
        omission_reason=None, total_bytes=0, next_cursor=None,
    )


def _run_diff_patch(root: str, change: "git_review.ReviewChange", layer: str) -> bytes:
    """CODING-08R.1: the STAGED layer still asks Git directly for the patch
    -- a ``--cached`` diff never touches worktree bytes or invokes a
    ``filter.<name>.clean``/``.process`` driver (confirmed empirically; see
    core.git_review's "Filter-free content comparison" section) -- exactly
    the same reasoning already applied to A1's staged numstat capture.

    UNSTAGED content (and untracked, which is always retrieved through the
    'unstaged' layer -- see ``_capture_file_diff``) DOES need worktree
    bytes, and therefore reuses the exact same filter-free temp-diff
    primitive core.git_review's own unstaged numstat capture uses: a direct,
    unconverting worktree read plus a pure object read for the index side
    (empty for untracked, which has no index side at all), handed to Git's
    own ``diff --no-index`` engine against a private temporary directory
    that can never see the reviewed repository's own ``.gitattributes``.
    This also removes the previous hardcoded ``/dev/null`` untracked
    reference, which was not portable to platforms without that path.
    """
    if layer == LAYER_STAGED:
        args = [
            "diff", "--cached", *git_review._DIFF_SAFETY_ARGS,
            "--no-renames", "--unified=3", "--", change.path,
        ]
        result = _run_git(root, args)
        if result.returncode not in (0, 1):
            raise git_review.ReviewProbeError("Git could not produce a bounded file diff")
        return result.stdout

    try:
        new_bytes = git_review._read_worktree_bytes_raw(root, change.path)
    except git_review._RawContentUnavailable as error:
        raise git_review.ReviewProbeError(
            "worktree content changed or became unavailable during retrieval"
        ) from error

    if change.relation == "untracked":
        old_bytes = b""
    else:
        try:
            old_bytes = git_review._read_index_blob_raw(root, change.index_object_id)
        except git_review._RawContentUnavailable as error:
            raise git_review.ReviewProbeError(
                "index content became unavailable during retrieval"
            ) from error

    return git_review._filter_free_diff(root, old_bytes, new_bytes, ("--no-renames", "--unified=3"))


def _capture_file_diff(
    identity: checkpoint_store.TargetIdentity,
    change: "git_review.ReviewChange",
    layer: str,
    start_hunk_index: int,
) -> FileDiffResult:
    change_id = review_change_id(change)

    if change.relation == "unmerged":
        return _omitted_file(change_id, change.path, layer, REASON_UNMERGED)
    if change.submodule.is_submodule:
        return _omitted_file(change_id, change.path, layer, REASON_SUBMODULE)

    if change.relation == "untracked":
        if layer != LAYER_UNSTAGED:
            raise RetrievalLayerError("untracked changes only support the 'unstaged' layer")
    elif change.relation in git_review._DIFFABLE_RELATIONS:
        if layer == LAYER_STAGED:
            active, diff_meta = change.staged, change.staged_diff
        else:
            active, diff_meta = change.unstaged, change.unstaged_diff
        if not active:
            raise RetrievalLayerError(
                f"change at {change.path!r} has no {layer} content to retrieve"
            )
        if diff_meta is not None and diff_meta.binary:
            return _binary_file(change_id, change.path, layer)
    else:
        raise RetrievalLayerError(f"unsupported relation for retrieval: {change.relation!r}")

    patch_bytes = _run_diff_patch(identity.canonical_root, change, layer)
    return _bound_patch(patch_bytes, change_id, change.path, layer, start_hunk_index)


def retrieve_review_file(
    snapshot_ref: str, change_id: str, layer: str, *, start_hunk_index: int = 0,
) -> RetrievalOutcome:
    """Safe, bounded, revalidated retrieval of one file/layer's diff
    content from a previously captured snapshot.

    Required sequence (section 13 of the task spec): revalidate target +
    fingerprint, capture bounded content, revalidate again, return
    content only if both checks agree the snapshot is still at least
    CURRENT_METADATA_ONLY. If repository state changes anywhere between
    the two checks, this returns no content at all (``file=None``,
    ``status`` reflecting whatever the post-check found) -- it never
    quietly refreshes the caller onto a different state. Refresh
    (``resolve_review_applicability``) and retrieval are separate
    operations; this function composes them, it does not conflate them.
    """
    if layer not in _VALID_LAYERS:
        raise RetrievalLayerError(f"unsupported layer: {layer!r}")
    if (
        not isinstance(start_hunk_index, int)
        or isinstance(start_hunk_index, bool)
        or start_hunk_index < 0
    ):
        raise RetrievalLayerError("start_hunk_index must be a non-negative int")

    before = resolve_review_applicability(snapshot_ref)
    if before.state not in (CURRENT, CURRENT_METADATA_ONLY):
        return RetrievalOutcome(status=before.state, file=None, applicability=before)

    with _registry_lock:
        entry = _registry.get(snapshot_ref)
        if entry is None:
            raise UnknownSnapshot(f"unknown or expired snapshot reference: {snapshot_ref!r}")
        snapshot = entry.snapshot

    change = _find_change(snapshot.review, change_id)
    if change is None:
        raise UnknownChange(f"unknown change_id {change_id!r} for this snapshot")

    file_result = _capture_file_diff(snapshot.identity, change, layer, start_hunk_index)

    after = resolve_review_applicability(snapshot_ref)
    if after.state not in (CURRENT, CURRENT_METADATA_ONLY):
        return RetrievalOutcome(status=after.state, file=None, applicability=after)

    return RetrievalOutcome(status=after.state, file=file_result, applicability=after)
