"""tools/filesystem.py -- CODING-01B-B: read_file() newline-fidelity fix.

Background: CODING-01B-A's kernel review confirmed read_file() opened files
in text mode, which performs universal-newline translation (CRLF -> LF).
That's invisible for a pure read/display tool in isolation, but it sits
directly on edit_file's new read-then-edit workflow: a model that copies
text read_file() showed it into edit_file's old_text would get a byte
mismatch against a real CRLF source file, since edit_file compares against
the file's actual on-disk bytes. Fixed by switching to a binary
seek/read/decode(errors="replace") implementation -- see tools/filesystem.py.

Uses the same _CapturingRegistry pattern as test_guardrails.py /
test_file_edit.py to exercise the real registered read_file/write_file
closures, not a reimplementation.
"""
import os

from tools.filesystem import register_filesystem_tools
from core.project_context import ProjectContext, ProjectContextState


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn


def _tools():
    reg = _CapturingRegistry()
    register_filesystem_tools(reg)
    return reg.fns["read_file"], reg.fns["write_file"]


def _body(result: str) -> str:
    """Strip the '[file: ... ]\\n' header, return just the content body."""
    return result.split("\n", 1)[1]


# ── Newline fidelity ────────────────────────────────────────────────────

def test_lf_file_returned_with_lf_unchanged(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "lf.txt"
    f.write_bytes(b"line1\nline2\nline3\n")
    result = read_file(str(f))
    assert _body(result) == "line1\nline2\nline3\n"


def test_crlf_file_returned_with_crlf_unchanged(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    result = read_file(str(f))
    assert _body(result) == "line1\r\nline2\r\nline3\r\n"


def test_mixed_lf_crlf_not_normalized(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "mixed.txt"
    f.write_bytes(b"a\r\nb\nc\r\nd\n")
    result = read_file(str(f))
    assert _body(result) == "a\r\nb\nc\r\nd\n"


# ── Byte-offset / byte-count contract ──────────────────────────────────

def test_start_is_a_byte_offset(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "digits.txt"
    f.write_bytes(b"0123456789")
    result = read_file(str(f), start=3, max_bytes=100)
    assert _body(result) == "3456789"


def test_max_bytes_is_a_byte_count(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "digits.txt"
    f.write_bytes(b"0123456789")
    result = read_file(str(f), start=3, max_bytes=4)
    assert _body(result) == "3456"


def test_header_reports_correct_total_byte_size(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "f.txt"
    f.write_bytes(b"hello world\r\n")  # 13 bytes
    result = read_file(str(f))
    assert "13 bytes total" in result


def test_truncated_more_remaining_flag_correct(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "f.txt"
    f.write_bytes(b"0123456789")
    result = read_file(str(f), start=0, max_bytes=4)
    assert "more remaining" in result


def test_end_of_file_flag_correct(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "f.txt"
    f.write_bytes(b"0123456789")
    result = read_file(str(f), start=0, max_bytes=100)
    assert "end of file" in result
    assert "more remaining" not in result


# ── Non-UTF-8 chunk handling (read/display path only) ──────────────────

def test_invalid_utf8_chunk_degrades_through_replacement_not_raise(tmp_path):
    read_file, _ = _tools()
    f = tmp_path / "binary.dat"
    f.write_bytes(b"before\xffmid\xfeafter")
    result = read_file(str(f))  # must not raise
    assert "�" in _body(result)
    assert "before" in result and "mid" in result and "after" in result


def test_read_arbitrary_byte_offset_mid_codepoint_does_not_raise(tmp_path):
    """start/max_bytes operate on raw byte boundaries -- a chunk can begin or
    end inside a multibyte UTF-8 code point. errors='replace' must absorb
    that cleanly since this is a display path, not edit_file's strict
    mutation path."""
    read_file, _ = _tools()
    f = tmp_path / "utf8.txt"
    f.write_bytes("héllo".encode("utf-8"))  # 'é' is 2 bytes (0xC3 0xA9)
    result = read_file(str(f), start=0, max_bytes=2)  # splits right after 'h', mid 'é'
    assert result  # no exception


# ── write_file unchanged ────────────────────────────────────────────────

def test_write_file_overwrite_and_append_unchanged(tmp_path):
    read_file, write_file = _tools()
    f = tmp_path / "w.txt"
    r1 = write_file(str(f), "hello\n", mode="overwrite")
    assert "Written" in r1
    assert f.read_bytes() == b"hello\n"

    r2 = write_file(str(f), "world\n", mode="append")
    assert "Written" in r2
    assert f.read_bytes() == b"hello\nworld\n"


# ── CODING-02B-A: project-aware relative-path defaulting ────────────────

def _project_tools(project_state=None):
    reg = _CapturingRegistry()
    register_filesystem_tools(reg, project_state=project_state)
    return reg.fns["read_file"], reg.fns["write_file"], reg.fns["list_dir"], reg.fns["search_files"]


def _active_state(root):
    state = ProjectContextState()
    state.set(ProjectContext(name="p", root=str(root)))
    return state


def test_relative_read_resolves_against_active_project_root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("hello\n")
    read_file, _, _, _ = _project_tools(_active_state(tmp_path))
    result = read_file("sub/f.txt")
    assert _body(result) == "hello\n"


def test_relative_write_resolves_against_active_project_root(tmp_path):
    _, write_file, _, _ = _project_tools(_active_state(tmp_path))
    write_file("new.txt", "content\n")
    assert (tmp_path / "new.txt").read_text() == "content\n"


def test_list_dir_default_dot_lists_active_root(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    _, _, list_dir, _ = _project_tools(_active_state(tmp_path))
    result = list_dir()
    assert "a.txt" in result
    assert "b.txt" in result
    assert str(tmp_path) in result  # display header shows the resolved root


def test_search_files_default_dot_searches_active_root(tmp_path):
    (tmp_path / "target.py").write_text("x = 1\n")
    (tmp_path / "other.md").write_text("# doc\n")
    _, _, _, search_files = _project_tools(_active_state(tmp_path))
    result = search_files(pattern="*.py")
    assert "target.py" in result
    assert "other.md" not in result


def test_absolute_path_unchanged_even_with_active_project(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}.txt"
    outside.write_text("elsewhere\n")
    try:
        read_file, _, _, _ = _project_tools(_active_state(tmp_path))
        result = read_file(str(outside))
        assert _body(result) == "elsewhere\n"
    finally:
        outside.unlink()


def test_tilde_path_unchanged_even_with_active_project(tmp_path):
    read_file, _, _, _ = _project_tools(_active_state(tmp_path))
    result = read_file("~/definitely-does-not-exist-lumina-test.txt")
    assert "not found" in result.lower()
    assert str(tmp_path) not in result


def test_no_active_project_preserves_legacy_relative_behavior(tmp_path, monkeypatch):
    """With no active project, a relative path resolves exactly as it did
    before CODING-02B-A -- against the process cwd, not any project root."""
    (tmp_path / "here.txt").write_text("legacy\n")
    monkeypatch.chdir(tmp_path)
    read_file, _, _, _ = _project_tools(project_state=None)
    result = read_file("here.txt")
    assert _body(result) == "legacy\n"


def test_no_active_project_list_dir_lists_process_cwd(tmp_path, monkeypatch):
    (tmp_path / "only_here.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    _, _, list_dir, _ = _project_tools(project_state=None)
    result = list_dir()
    assert "only_here.txt" in result
