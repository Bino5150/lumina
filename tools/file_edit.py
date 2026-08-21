"""
Surgical File Editor — exact-match, multi-hunk, all-or-nothing text edits
to a single existing UTF-8 file.

CODING-01B-A kernel checkpoint: register_file_edit_tools() exists but is
not called by core/agent.py yet, edit_file is not in TOOL_TIERS, and it is
not in any tool_profiles/*.json. Nothing here changes what LuminaAgent can
actually do until a later checkpoint wires it in.

Concurrency note: the pre-replace revalidation in _apply_replacement() is
strong optimistic revalidation, not a mathematically atomic filesystem
compare-and-swap. Nothing else in this codebase takes a real flock()/
fcntl() lock on arbitrary source files, so acquiring one here wouldn't be
honored by any other writer anyway. There remains an unavoidable POSIX
TOCTOU interval between the final revalidation read and os.replace()
unless every writer participates in a stronger locking/CAS protocol.
"""

import os
import stat
import errno
import hashlib
import tempfile

from tools.diff import _unified_diff
from core.project_context import resolve_project_path


class _KernelError(Exception):
    """Internal control-flow only — carries a ready-to-return '[Error: ...]'
    string so every rejection path (deep inside the read seam or shallow in
    edit_file's own validation) produces exactly one deterministic message."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _err(message: str) -> str:
    return f"[Error: {message}]"


def _read_target_snapshot(path: str):
    """The dedicated read seam — lstat-based symlink/directory checks, an
    O_NOFOLLOW open (closes the check-then-open race for the common case:
    if a symlink was swapped in between our lstat and this open, the kernel
    itself refuses to follow it), fstat of the fd actually opened, and a
    full read.

    Deliberately the single choke point both the initial snapshot and the
    pre-replace revalidation call through, so tests can monkeypatch this one
    function to make "the second read" observe changed content
    deterministically, instead of racing real timing.

    Returns (raw_bytes, fstat_result, fingerprint) on success. Raises
    _KernelError with a specific message on any expected rejection
    (missing / symlink / directory / not-regular).
    """
    try:
        lst = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        raise _KernelError(_err(f"file not found: {path}"))

    # Checked on the lstat (no-follow) result first, so a broken symlink is
    # recognized as a symlink rather than reported as merely missing.
    if stat.S_ISLNK(lst.st_mode):
        raise _KernelError(_err(f"refusing to edit a symlink: {path}"))
    if stat.S_ISDIR(lst.st_mode):
        raise _KernelError(_err(f"path is a directory, not a file: {path}"))
    if not stat.S_ISREG(lst.st_mode):
        raise _KernelError(_err(f"not a regular file: {path}"))

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise _KernelError(_err(f"refusing to edit a symlink: {path}"))
        if e.errno == errno.ENOENT:
            raise _KernelError(_err(f"file not found: {path}"))
        raise _KernelError(_err(f"failed to open {path}: {e}"))

    try:
        fst = os.fstat(fd)
        if stat.S_ISDIR(fst.st_mode):
            raise _KernelError(_err(f"path is a directory, not a file: {path}"))
        if not stat.S_ISREG(fst.st_mode):
            raise _KernelError(_err(f"not a regular file: {path}"))
    except BaseException:
        os.close(fd)
        raise

    with os.fdopen(fd, "rb") as f:
        raw = f.read()

    # Fingerprint deliberately excludes atime (our own read would perturb
    # it) and prefers structural identity (dev/ino) over content alone: a
    # second actor replacing the path with byte-identical content is still
    # a concurrent replacement and shouldn't have its inode/mode state
    # casually overwritten by our stale transaction. st_nlink is included
    # so a target that gains a hard link during the edit transaction (still
    # nlink==1 at admission, where it's allowed to proceed) is caught here
    # at revalidation rather than silently missed -- nlink is concurrent
    # filesystem state like any other field above, not a separate check.
    fingerprint = (
        fst.st_dev, fst.st_ino, fst.st_size,
        stat.S_IMODE(fst.st_mode), fst.st_nlink, fst.st_mtime_ns,
        hashlib.sha256(raw).hexdigest(),
    )
    return raw, fst, fingerprint


def _apply_replacement(path: str, new_bytes: bytes, original_fst, original_fingerprint):
    """Writes new_bytes to a same-directory temp file, revalidates the
    source is unchanged since the initial snapshot, then atomically
    replaces it. Returns None on success, or an '[Error: ...]' string on
    failure. The source file is left byte-for-byte untouched on every
    failure path that runs before os.replace() itself executes."""
    parent = os.path.dirname(path) or "."
    mode_bits = stat.S_IMODE(original_fst.st_mode)

    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".edit_file_", suffix=".tmp")
    except OSError as e:
        return _err(f"failed to create temporary file for {path}: {e}")

    try:
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(new_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, mode_bits)
        except OSError as e:
            return _err(f"failed to write temporary file for {path}: {e}")

        # Pre-replace revalidation: any failure to re-confirm the source is
        # exactly what we started from is treated as a concurrent change,
        # fail-closed — including exceptions this seam wasn't specifically
        # written to anticipate. If we can't prove nothing changed, we
        # don't replace.
        try:
            _, _, revalidate_fingerprint = _read_target_snapshot(path)
        except Exception:
            return _err(f"{path} changed during the edit — nothing was written")

        if revalidate_fingerprint != original_fingerprint:
            return _err(f"{path} changed during the edit — nothing was written")

        try:
            os.replace(tmp_path, path)
        except OSError as e:
            return _err(f"failed to replace {path}: {e}")

        tmp_path = None  # replaced — no longer ours to clean up

        # Best-effort durability hardening only. The replace already
        # happened and is real; a failure here must never be reported as
        # if the edit didn't occur.
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as e:
            print(f"[FILE_EDIT] parent directory fsync failed (edit already applied): {e}", flush=True)

        return None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def edit_file(path: str, edits: list, expected_sha256: str = "", project_state=None) -> str:
    """project_state (CODING-02B-A): optional core.project_context.
    ProjectContextState. None (default) preserves this function's exact
    pre-CODING-02B-A behavior — every direct call in tests/test_file_edit.py
    and tests/test_file_edit_integration.py omits it and is unaffected.
    Path resolution happens here, strictly before the kernel admission
    logic below (_read_target_snapshot's lstat/O_NOFOLLOW/fstat sequence) —
    resolve_project_path() never calls realpath(), so it cannot resolve away
    a symlink component before the kernel gets a chance to reject it."""
    path = os.path.abspath(resolve_project_path(path, project_state))

    try:
        raw, fst, fingerprint = _read_target_snapshot(path)
    except _KernelError as e:
        return e.message

    if fst.st_nlink > 1:
        return _err(
            f"refusing to edit a hard-linked file (nlink={fst.st_nlink}): {path} "
            "— editing would silently detach it from its other links"
        )

    try:
        original_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _err(f"{path} is not valid UTF-8 — edit_file only supports UTF-8 text files")

    sha_before = fingerprint[6]  # sha256 hex is the last fingerprint element

    if expected_sha256:
        normalized = expected_sha256.strip().lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            return _err("invalid expected_sha256 — must be a 64-character hex SHA-256 digest")
        if normalized != sha_before:
            return _err(f"stale file — expected_sha256 does not match the current contents of {path}")

    if not isinstance(edits, list) or len(edits) == 0:
        return _err("edits must be a non-empty list of {old_text, new_text} objects")

    parsed = []
    for i, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return _err(f"edit #{i} is not a valid {{old_text, new_text}} object")
        if "old_text" not in edit:
            return _err(f"edit #{i} is missing 'old_text'")
        if "new_text" not in edit:
            return _err(f"edit #{i} is missing 'new_text'")
        old_text = edit["old_text"]
        new_text = edit["new_text"]
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return _err(f"edit #{i}'s old_text/new_text must be strings")
        if old_text == "":
            return _err(f"edit #{i} has an empty old_text — old_text must be non-empty")
        if old_text == new_text:
            return _err(f"edit #{i} is a no-op — old_text and new_text are identical")
        parsed.append((i, old_text, new_text))

    # Every hunk resolves against this one immutable original snapshot —
    # never against a working buffer already touched by an earlier hunk.
    # That's what keeps this order-independent: an earlier replacement can
    # never manufacture or destroy a later match.
    spans = []  # (start, end, new_text, index)
    for i, old_text, new_text in parsed:
        count = original_text.count(old_text)
        if count == 0:
            return _err(f"edit #{i}'s old_text was not found in {path}")
        if count > 1:
            return _err(f"edit #{i}'s old_text matches {count} times in {path} — must match exactly once")
        start = original_text.index(old_text)
        end = start + len(old_text)
        spans.append((start, end, new_text, i))

    spans_by_start = sorted(spans, key=lambda s: s[0])
    for a, b in zip(spans_by_start, spans_by_start[1:]):
        if a[1] > b[0]:
            return _err(
                f"edits overlap in {path} — hunk spans [{a[0]},{a[1]}) and "
                f"[{b[0]},{b[1]}) intersect"
            )

    # Apply highest offset to lowest so earlier (lower-offset) spans' text
    # coordinates never shift out from under a later replacement.
    new_full_text = original_text
    for start, end, new_text, i in sorted(spans, key=lambda s: s[0], reverse=True):
        new_full_text = new_full_text[:start] + new_text + new_full_text[end:]

    if new_full_text == original_text:
        # Provably unreachable given the per-hunk no-op guard above (every
        # hunk changes a distinct, non-overlapping span) — kept as an
        # explicit invariant rather than trusted purely by construction.
        return _err("edit produces no change — nothing written")

    diff_text = _unified_diff(original_text, new_full_text, old_label=path, new_label=path)
    new_bytes = new_full_text.encode("utf-8")
    sha_after = hashlib.sha256(new_bytes).hexdigest()

    failure = _apply_replacement(path, new_bytes, fst, fingerprint)
    if failure is not None:
        return failure

    return (
        f"[Edited: {path} | {len(spans)} hunk(s) | "
        f"sha256 before: {sha_before} | sha256 after: {sha_after}]\n"
        f"```diff\n{diff_text}\n```"
    )


def register_file_edit_tools(registry, project_state=None):
    """project_state: see edit_file()'s own docstring. Threaded through a
    thin wrapper closure (not registered directly) so the registered tool's
    dispatched kwargs still match its JSON schema exactly — project_state
    is per-agent plumbing, never a model-facing parameter. Same pattern
    tools/tasks.py already uses to thread the owning agent through its
    registered tool wrappers."""

    def _tool_edit_file(path, edits, expected_sha256=""):
        return edit_file(path, edits, expected_sha256=expected_sha256, project_state=project_state)

    registry.register(
        name="edit_file",
        fn=_tool_edit_file,
        description=(
            "Surgically edit an existing UTF-8 text file by replacing exact "
            "text spans. Each old_text must match the file's current contents "
            "exactly once; all edits in one call apply atomically — either "
            "every hunk lands or none do. Optionally pass expected_sha256 to "
            "require the file's current contents match before editing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to an existing regular file to edit. Supports ~ expansion. Must not be a symlink."
                },
                "edits": {
                    "type": "array",
                    "description": "Ordered list of surgical edits, applied atomically. Each old_text must match exactly once in the file.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string", "description": "Exact text to replace. Must be non-empty and match exactly once in the current file."},
                            "new_text": {"type": "string", "description": "Replacement text. Use an empty string to delete old_text."}
                        },
                        "required": ["old_text", "new_text"]
                    }
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional SHA-256 hex digest of the file's exact current bytes. If given and it doesn't match, nothing is written."
                }
            },
            "required": ["path", "edits"]
        }
    )
