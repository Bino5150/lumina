"""CODING-01B-A kernel tests — tools/file_edit.py.

Calls edit_file() directly (no LuminaAgent, no registry dispatch, no
network/model calls) except for the small Registration section, which
exercises register_file_edit_tools() against a minimal capturing registry
identical in spirit to the one in test_guardrails.py.

Concurrency/failure-injection tests use deterministic monkeypatch seams —
_read_target_snapshot (the module's single read choke point, used both for
the initial snapshot and the pre-replace revalidation), os.fdopen, os.fsync,
and os.replace — never sleep()/timing races.
"""
import hashlib
import os
import stat

import pytest

from tools import file_edit
from tools.file_edit import edit_file, register_file_edit_tools


# ── Registration helpers ───────────────────────────────────────────────────

class _CapturingRegistry:
    """Same pattern as test_guardrails.py's _CapturingRegistry, extended to
    also retain description/parameters so schema-shape tests can inspect
    them directly."""

    def __init__(self):
        self.fns = {}
        self.descriptions = {}
        self.parameters = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn
        self.descriptions[name] = description
        self.parameters[name] = parameters


# ── Basic success (1-7) ──────────────────────────────────────────────────

def test_single_replacement(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello world\n")
    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}])
    assert result.startswith("[Edited:")
    assert f.read_text() == "hello there\n"


def test_surrounding_content_unchanged(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha\nbeta\ngamma\ndelta\n")
    edit_file(str(f), [{"old_text": "beta", "new_text": "BETA"}])
    assert f.read_text() == "alpha\nBETA\ngamma\ndelta\n"


def test_deletion_via_empty_new_text(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("keep this, drop_me, keep that\n")
    edit_file(str(f), [{"old_text": "drop_me, ", "new_text": ""}])
    assert f.read_text() == "keep this, keep that\n"


def test_multiple_disjoint_replacements_atomic(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two three four\n")
    result = edit_file(str(f), [
        {"old_text": "one", "new_text": "ONE"},
        {"old_text": "three", "new_text": "THREE"},
    ])
    assert result.startswith("[Edited: ")
    assert "2 hunk(s)" in result
    assert f.read_text() == "ONE two THREE four\n"


def test_result_sha_before_correct(tmp_path):
    f = tmp_path / "f.txt"
    original = "hello world\n"
    f.write_bytes(original.encode("utf-8"))
    expected_before = hashlib.sha256(original.encode("utf-8")).hexdigest()
    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}])
    assert f"sha256 before: {expected_before}" in result


def test_result_sha_after_correct(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello world\n")
    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}])
    expected_after = hashlib.sha256(b"hello there\n").hexdigest()
    assert f"sha256 after: {expected_after}" in result


def test_unified_diff_matches_actual_resulting_content(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("line1\nline2\nline3\n")
    result = edit_file(str(f), [{"old_text": "line2", "new_text": "CHANGED"}])
    diff_block = result.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
    assert "-line2" in diff_block
    assert "+CHANGED" in diff_block
    assert " line1" in diff_block  # unchanged context line still shown
    assert f.read_text() == "line1\nCHANGED\nline3\n"


# ── Transactionality (8-12) ──────────────────────────────────────────────

def test_second_hunk_invalid_blocks_first_hunk_too(tmp_path):
    f = tmp_path / "f.txt"
    original = "one two three\n"
    f.write_text(original)
    result = edit_file(str(f), [
        {"old_text": "one", "new_text": "ONE"},
        {"old_text": "NOT_PRESENT", "new_text": "X"},
    ])
    assert result.startswith("[Error:")
    assert "was not found" in result
    assert f.read_text() == original  # first hunk never reached disk


def test_ambiguous_hunk_no_mutation(tmp_path):
    f = tmp_path / "f.txt"
    original = "dup dup dup\n"
    f.write_text(original)
    result = edit_file(str(f), [{"old_text": "dup", "new_text": "X"}])
    assert result.startswith("[Error:")
    assert "matches 3 times" in result
    assert f.read_text() == original


def test_overlapping_hunks_no_mutation(tmp_path):
    f = tmp_path / "f.txt"
    original = "abcdef\n"
    f.write_text(original)
    result = edit_file(str(f), [
        {"old_text": "abcd", "new_text": "X"},
        {"old_text": "cdef", "new_text": "Y"},
    ])
    assert result.startswith("[Error:")
    assert "overlap" in result
    assert f.read_text() == original


def test_stale_sha_no_mutation(tmp_path):
    f = tmp_path / "f.txt"
    original = "hello world\n"
    f.write_text(original)
    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}],
                        expected_sha256="0" * 64)
    assert result.startswith("[Error:")
    assert "stale file" in result
    assert f.read_text() == original


def test_concurrent_change_at_revalidation_survives(tmp_path, monkeypatch):
    """Deterministic seam, not a timing race: _read_target_snapshot is
    monkeypatched to return a tampered fingerprint on its second call (the
    pre-replace revalidation), simulating a concurrent actor changing the
    file between our initial read and our final write. Also covers #30:
    the temp file must not survive this abort."""
    f = tmp_path / "f.txt"
    original = "hello world\n"
    f.write_text(original)

    real = file_edit._read_target_snapshot
    calls = {"n": 0}

    def fake(path):
        calls["n"] += 1
        raw, fst, fingerprint = real(path)
        if calls["n"] == 1:
            return raw, fst, fingerprint
        tampered = ("tampered-dev",) + fingerprint[1:]
        return raw, fst, tampered

    monkeypatch.setattr(file_edit, "_read_target_snapshot", fake)

    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}])

    assert result.startswith("[Error:")
    assert "changed during the edit" in result
    assert calls["n"] == 2  # proves revalidation actually ran a second read
    assert f.read_text() == original  # the "external" content survives untouched
    assert not any(p.name.startswith(".edit_file_") for p in tmp_path.iterdir())


def test_hard_link_gained_during_transaction_detected_at_revalidation(tmp_path, monkeypatch):
    """CODING-01B-D regression: st_nlink is part of the revalidation
    fingerprint. A target that starts with nlink==1 (admitted normally --
    the nlink>1 check only fires at initial snapshot) but gains a real hard
    link between the initial snapshot and the pre-replace revalidation must
    be caught there and refused, not silently missed.

    Reproduces the exact CODING-01B-C review finding via a genuine
    os.link() injected deterministically at the revalidation seam's second
    invocation -- not a fabricated fingerprint tuple, not sleep/timing."""
    f = tmp_path / "f.txt"
    original = "hello world\n"
    f.write_text(original)
    other_link = tmp_path / "other_link.txt"

    real = file_edit._read_target_snapshot
    calls = {"n": 0}

    def fake(path):
        calls["n"] += 1
        if calls["n"] == 2:
            os.link(path, other_link)  # real hard link, created deterministically
        return real(path)

    monkeypatch.setattr(file_edit, "_read_target_snapshot", fake)

    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}])

    assert result.startswith("[Error:")
    assert "changed during the edit" in result
    assert calls["n"] == 2  # proves the initial nlink==1 snapshot was admitted
    assert f.read_text() == original  # never overwritten by Lumina
    assert other_link.read_text() == original  # the gained link's content untouched too
    assert not any(p.name.startswith(".edit_file_") for p in tmp_path.iterdir())


# ── Input validation (13-22) ──────────────────────────────────────────────

def test_empty_edits_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    result = edit_file(str(f), [])
    assert result.startswith("[Error:")
    assert "non-empty list" in result


def test_malformed_edit_item_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    result = edit_file(str(f), ["not a dict"])
    assert result.startswith("[Error:")
    assert "not a valid" in result


def test_missing_old_text_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    result = edit_file(str(f), [{"new_text": "x"}])
    assert result.startswith("[Error:")
    assert "missing 'old_text'" in result


def test_missing_new_text_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    result = edit_file(str(f), [{"old_text": "hello"}])
    assert result.startswith("[Error:")
    assert "missing 'new_text'" in result


def test_empty_old_text_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    result = edit_file(str(f), [{"old_text": "", "new_text": "x"}])
    assert result.startswith("[Error:")
    assert "empty old_text" in result


def test_non_string_values_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    result = edit_file(str(f), [{"old_text": 123, "new_text": "x"}])
    assert result.startswith("[Error:")
    assert "must be strings" in result


def test_noop_hunk_rejected(tmp_path):
    f = tmp_path / "f.txt"
    original = "hello world\n"
    f.write_text(original)
    result = edit_file(str(f), [{"old_text": "hello", "new_text": "hello"}])
    assert result.startswith("[Error:")
    assert "no-op" in result
    assert f.read_text() == original


def test_malformed_sha_rejected(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello world\n")
    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}],
                        expected_sha256="not-a-valid-digest")
    assert result.startswith("[Error:")
    assert "invalid expected_sha256" in result


def test_nonexistent_file_rejected(tmp_path):
    missing = tmp_path / "does_not_exist.txt"
    result = edit_file(str(missing), [{"old_text": "a", "new_text": "b"}])
    assert result.startswith("[Error:")
    assert "file not found" in result


def test_directory_path_rejected(tmp_path):
    result = edit_file(str(tmp_path), [{"old_text": "a", "new_text": "b"}])
    assert result.startswith("[Error:")
    assert "directory" in result


# ── Filesystem safety (23-31) ──────────────────────────────────────────────

def test_symlink_rejected(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hello\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    result = edit_file(str(link), [{"old_text": "hello", "new_text": "hi"}])
    assert result.startswith("[Error:")
    assert "symlink" in result
    assert real.read_text() == "hello\n"


def test_broken_symlink_rejected_not_reported_as_missing(tmp_path):
    link = tmp_path / "broken_link.txt"
    link.symlink_to(tmp_path / "nonexistent_target.txt")
    result = edit_file(str(link), [{"old_text": "a", "new_text": "b"}])
    assert result.startswith("[Error:")
    assert "symlink" in result
    assert "file not found" not in result


def test_non_utf8_rejected_no_temp_residue(tmp_path):
    f = tmp_path / "binary.txt"
    f.write_bytes(b"\xff\xfe\x00invalid utf-8\x80\x81")
    result = edit_file(str(f), [{"old_text": "a", "new_text": "b"}])
    assert result.startswith("[Error:")
    assert "not valid UTF-8" in result
    assert not any(p.name.startswith(".edit_file_") for p in tmp_path.iterdir())


def test_crlf_and_lf_preserved_outside_target(tmp_path):
    f = tmp_path / "mixed.txt"
    f.write_bytes(b"line1\r\nline2\r\nline3\nline4\r\n")
    edit_file(str(f), [{"old_text": "line2", "new_text": "LINE2_CHANGED"}])
    assert f.read_bytes() == b"line1\r\nLINE2_CHANGED\r\nline3\nline4\r\n"


def test_unix_permission_bits_preserved(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello world\n")
    os.chmod(f, 0o640)
    edit_file(str(f), [{"old_text": "world", "new_text": "there"}])
    mode = stat.S_IMODE(os.stat(f).st_mode)
    assert mode == 0o640


def test_temp_file_removed_when_write_fails(tmp_path, monkeypatch):
    """Covers #29 and #40: a raw write() exception during the temp-file
    write step must leave the original bytes intact and leave no temp
    residue. Injected via a targeted os.fdopen wrapper scoped to 'wb' mode
    only, so the read path (mode 'rb') is untouched."""
    f = tmp_path / "f.txt"
    original = "original content\n"
    f.write_text(original)

    real_fdopen = os.fdopen

    class _WriteFailFile:
        def __init__(self, real_file):
            self._real = real_file

        def write(self, data):
            raise OSError("simulated write failure (ENOSPC)")

        def flush(self):
            pass

        def fileno(self):
            return self._real.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._real.close()
            return False

    def fake_fdopen(fd, mode="r", *a, **kw):
        real_file = real_fdopen(fd, mode, *a, **kw)
        if mode == "wb":
            return _WriteFailFile(real_file)
        return real_file

    monkeypatch.setattr(file_edit.os, "fdopen", fake_fdopen)

    result = edit_file(str(f), [{"old_text": "original", "new_text": "CHANGED"}])

    assert result.startswith("[Error:")
    assert "failed to write temporary file" in result
    assert f.read_text() == original
    assert not any(p.name.startswith(".edit_file_") for p in tmp_path.iterdir())


def test_hard_linked_target_rejected(tmp_path):
    f = tmp_path / "f.txt"
    original = "hello world\n"
    f.write_text(original)
    other_link = tmp_path / "other_link.txt"
    os.link(f, other_link)

    result = edit_file(str(f), [{"old_text": "world", "new_text": "there"}])

    assert result.startswith("[Error:")
    assert "hard-linked" in result
    assert "nlink=2" in result
    assert f.read_text() == original
    assert other_link.read_text() == original


# ── Matching correctness (32-35) ───────────────────────────────────────────

def test_replacement_text_containing_another_hunks_search_text(tmp_path):
    """new_text of one hunk contains the literal old_text of another hunk.
    Matching must resolve against the ORIGINAL snapshot only — a buggy
    sequential implementation would find the decoy and either misfire or
    report a false ambiguity."""
    f = tmp_path / "f.txt"
    f.write_text("AAA middle CCC end\n")
    edits = [
        {"old_text": "AAA", "new_text": "ZZZ CCC ZZZ"},
        {"old_text": "CCC", "new_text": "REPLACED"},
    ]
    result = edit_file(str(f), edits)
    assert result.startswith("[Edited:")
    assert f.read_text() == "ZZZ CCC ZZZ middle REPLACED end\n"


def test_edits_in_reverse_file_order_still_correct(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("AAA middle CCC end\n")
    edits = [
        {"old_text": "CCC", "new_text": "REPLACED"},
        {"old_text": "AAA", "new_text": "ZZZ CCC ZZZ"},
    ]
    result = edit_file(str(f), edits)
    assert result.startswith("[Edited:")
    assert f.read_text() == "ZZZ CCC ZZZ middle REPLACED end\n"


def test_repeated_same_target_across_two_hunks_fails_safely(tmp_path):
    f = tmp_path / "f.txt"
    original = "only one X here\n"
    f.write_text(original)
    result = edit_file(str(f), [
        {"old_text": "X", "new_text": "A"},
        {"old_text": "X", "new_text": "B"},
    ])
    assert result.startswith("[Error:")
    assert "overlap" in result
    assert f.read_text() == original


def test_old_text_spanning_multiple_lines(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("line1\nline2\nline3\nline4\n")
    result = edit_file(str(f), [{"old_text": "line2\nline3", "new_text": "MERGED"}])
    assert result.startswith("[Edited:")
    assert f.read_text() == "line1\nMERGED\nline4\n"


# ── Registration (36-39) ───────────────────────────────────────────────────

def test_exactly_one_edit_file_registration():
    reg = _CapturingRegistry()
    register_file_edit_tools(reg)
    assert list(reg.fns.keys()) == ["edit_file"]


def test_nested_array_object_schema_has_intended_required_fields():
    reg = _CapturingRegistry()
    register_file_edit_tools(reg)
    params = reg.parameters["edit_file"]
    assert params["required"] == ["path", "edits"]
    edits_schema = params["properties"]["edits"]
    assert edits_schema["type"] == "array"
    item_schema = edits_schema["items"]
    assert item_schema["type"] == "object"
    assert set(item_schema["required"]) == {"old_text", "new_text"}
    assert item_schema["properties"]["old_text"]["type"] == "string"
    assert item_schema["properties"]["new_text"]["type"] == "string"


def test_expected_sha256_is_optional():
    reg = _CapturingRegistry()
    register_file_edit_tools(reg)
    params = reg.parameters["edit_file"]
    assert "expected_sha256" in params["properties"]
    assert "expected_sha256" not in params["required"]


def test_registration_points_to_real_production_function():
    reg = _CapturingRegistry()
    register_file_edit_tools(reg)
    assert reg.fns["edit_file"] is edit_file


# ── Failure injection (40-43) ──────────────────────────────────────────────

def test_fsync_before_replace_failure_leaves_original_intact(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    original = "original content\n"
    f.write_text(original)

    def fake_fsync(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(file_edit.os, "fsync", fake_fsync)

    result = edit_file(str(f), [{"old_text": "original", "new_text": "CHANGED"}])

    assert result.startswith("[Error:")
    assert "failed to write temporary file" in result
    assert f.read_text() == original
    assert not any(p.name.startswith(".edit_file_") for p in tmp_path.iterdir())


def test_os_replace_failure_leaves_original_intact_and_cleans_temp(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    original = "original content\n"
    f.write_text(original)

    def fake_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(file_edit.os, "replace", fake_replace)

    result = edit_file(str(f), [{"old_text": "original", "new_text": "CHANGED"}])

    assert result.startswith("[Error:")
    assert "failed to replace" in result
    assert f.read_text() == original
    assert not any(p.name.startswith(".edit_file_") for p in tmp_path.iterdir())


def test_post_replace_dirfsync_failure_reported_truthfully(tmp_path, monkeypatch, capsys):
    """The edit has already durably landed by the time directory-fsync
    hardening runs. A failure there must not be reported as if the edit
    never happened -- it should still report success, with the durability
    hardening failure surfaced honestly (console print, matching the
    persistence.py convention), not silently swallowed either."""
    f = tmp_path / "f.txt"
    f.write_text("original content\n")

    real_fsync = os.fsync
    calls = {"n": 0}

    def fake_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_fsync(fd)  # the temp-file fsync must still succeed
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(file_edit.os, "fsync", fake_fsync)

    result = edit_file(str(f), [{"old_text": "original", "new_text": "CHANGED"}])

    assert result.startswith("[Edited:")
    assert f.read_text() == "CHANGED content\n"
    captured = capsys.readouterr()
    assert "FILE_EDIT" in captured.out
    assert "parent directory fsync failed" in captured.out
