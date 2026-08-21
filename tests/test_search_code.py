"""tools/search.py -- CODING-03A1: search_code().

Additive to tools/filesystem.py's search_files (untouched, still exists).
Uses the same _CapturingRegistry / real-ToolRegistry patterns as
test_filesystem.py and test_tool_profiles.py to exercise the real
registered closure and the real tier/profile machinery, not
reimplementations.
"""
import os

import config
from tools.registry import ToolRegistry
from tools.search import register_search_tools, MAX_MATCHES
from core.project_context import ProjectContext, ProjectContextState
from core.tool_profiles import TOOL_TIERS, apply_tool_profile, list_profiles


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn


def _search(project_state=None):
    reg = _CapturingRegistry()
    register_search_tools(reg, project_state=project_state)
    return reg.fns["search_code"]


def _active_state(root):
    state = ProjectContextState()
    state.set(ProjectContext(name="p", root=str(root)))
    return state


def _write(path, content):
    path.write_text(content)
    return path


def _assert_real_match(result, needle):
    """Bare `needle in result` is a vacuous check: the no-match message
    itself is f"[No matches for {query!r} in ...]", which contains the
    query text -- so a broken implementation that finds nothing can still
    make that assertion pass. Require an actual '>' match marker too."""
    assert not result.startswith("[No matches"), result
    assert needle in result
    assert "> " in result


# ── Literal search ───────────────────────────────────────────────────────

def test_literal_single_match(tmp_path):
    _write(tmp_path / "a.py", "x = 1\nNEEDLE_ONE = 2\ny = 3\n")
    search_code = _search()
    result = search_code("NEEDLE_ONE", path=str(tmp_path))
    assert "a.py:2" in result
    assert "> 2: NEEDLE_ONE = 2" in result


def test_literal_multiple_files(tmp_path):
    _write(tmp_path / "a.py", "NEEDLE_TWO\n")
    _write(tmp_path / "b.py", "NEEDLE_TWO\n")
    search_code = _search()
    result = search_code("NEEDLE_TWO", path=str(tmp_path))
    assert "a.py:1" in result
    assert "b.py:1" in result


def test_multiple_matches_in_one_file(tmp_path):
    lines = ["NEEDLE_THREE" if i in (0, 20) else f"filler{i}" for i in range(25)]
    _write(tmp_path / "a.py", "\n".join(lines) + "\n")
    search_code = _search()
    result = search_code("NEEDLE_THREE", path=str(tmp_path), context_lines=0)
    assert "a.py:1" in result
    assert "a.py:21" in result


# ── Location / context ───────────────────────────────────────────────────

def test_exact_line_number(tmp_path):
    _write(tmp_path / "a.py", "one\ntwo\nNEEDLE_FOUR\nfour\n")
    search_code = _search()
    result = search_code("NEEDLE_FOUR", path=str(tmp_path))
    assert result.startswith("a.py:3")


def test_context_lines_zero_returns_only_match_line(tmp_path):
    _write(tmp_path / "a.py", "before\nNEEDLE_FIVE\nafter\n")
    search_code = _search()
    result = search_code("NEEDLE_FIVE", path=str(tmp_path), context_lines=0)
    assert "before" not in result
    assert "after" not in result
    assert "> 2: NEEDLE_FIVE" in result


def test_normal_n_line_context(tmp_path):
    _write(tmp_path / "a.py", "l1\nl2\nNEEDLE_SIX\nl4\nl5\n")
    search_code = _search()
    result = search_code("NEEDLE_SIX", path=str(tmp_path), context_lines=2)
    assert "l1" in result and "l2" in result and "l4" in result and "l5" in result


def test_context_clips_at_file_start(tmp_path):
    _write(tmp_path / "a.py", "NEEDLE_SEVEN\nl2\nl3\n")
    search_code = _search()
    result = search_code("NEEDLE_SEVEN", path=str(tmp_path), context_lines=5)
    assert "1: NEEDLE_SEVEN" in result
    assert "0:" not in result  # no line 0, no negative clamp leak


def test_context_clips_at_file_end(tmp_path):
    _write(tmp_path / "a.py", "l1\nl2\nNEEDLE_EIGHT\n")
    search_code = _search()
    result = search_code("NEEDLE_EIGHT", path=str(tmp_path), context_lines=5)
    assert "3: NEEDLE_EIGHT" in result
    assert "4:" not in result


def test_overlapping_context_windows_merge(tmp_path):
    # matches on line 5 and line 8, context_lines=2 -> windows [3,7] and
    # [6,10] overlap (6 <= 7+1) -> must merge into one window/header.
    lines = [f"l{i}" for i in range(1, 11)]
    lines[4] = "NEEDLE_NINE"
    lines[7] = "NEEDLE_NINE"
    _write(tmp_path / "a.py", "\n".join(lines) + "\n")
    search_code = _search()
    result = search_code("NEEDLE_NINE", path=str(tmp_path), context_lines=2)
    assert result.count("a.py:") == 1  # one merged header, not two
    assert result.count("> ") == 2      # both match lines still marked


# ── Regex ─────────────────────────────────────────────────────────────────

def test_valid_regex(tmp_path):
    _write(tmp_path / "a.py", "foo123\nfoobar\n")
    search_code = _search()
    result = search_code(r"foo\d+", path=str(tmp_path), regex=True, context_lines=0)
    assert "foo123" in result
    assert "foobar" not in result


def test_invalid_regex_clean_error(tmp_path):
    search_code = _search()
    result = search_code("(unclosed", path=str(tmp_path), regex=True)
    assert result.startswith("[Error: invalid regex:")


def test_regex_metacharacters_literal_when_regex_false(tmp_path):
    _write(tmp_path / "a.py", "axb\na.b\n")
    search_code = _search()
    result = search_code("a.b", path=str(tmp_path), context_lines=0)
    _assert_real_match(result, "a.b")
    assert "axb" not in result


# ── Case sensitivity ─────────────────────────────────────────────────────

def test_case_sensitive_literal(tmp_path):
    _write(tmp_path / "a.py", "foo_needle\nFoo_needle\n")
    search_code = _search()
    result = search_code("Foo_needle", path=str(tmp_path), context_lines=0)
    _assert_real_match(result, "Foo_needle")
    assert "> 1: foo_needle" not in result


def test_case_sensitive_regex(tmp_path):
    _write(tmp_path / "a.py", "foo_needle\nFoo_needle\n")
    search_code = _search()
    result = search_code("Foo_needle", path=str(tmp_path), regex=True, context_lines=0)
    assert result.count("> ") == 1


# ── Glob ─────────────────────────────────────────────────────────────────

def test_glob_includes_and_excludes(tmp_path):
    _write(tmp_path / "a.py", "NEEDLE_TEN\n")
    _write(tmp_path / "b.md", "NEEDLE_TEN\n")
    search_code = _search()
    result = search_code("NEEDLE_TEN", path=str(tmp_path), glob="*.py")
    assert "a.py" in result
    assert "b.md" not in result


def test_unmatched_glob_is_no_result_not_error(tmp_path):
    _write(tmp_path / "a.py", "NEEDLE_ELEVEN\n")
    search_code = _search()
    result = search_code("NEEDLE_ELEVEN", path=str(tmp_path), glob="*.rs")
    assert not result.startswith("[Error")
    assert "No matches" in result


# ── Single-file glob semantics (CODING-03A1C) ────────────────────────────
#
# A1 special-cased a single-file target to always ignore glob -- rejected
# by review: every supplied constraint must stay meaningful, including
# against an explicit single-file path.

def test_single_file_matching_glob_is_searched(tmp_path):
    f = _write(tmp_path / "foo.py", "NEEDLE_GLOB_MATCH\n")
    search_code = _search()
    result = search_code("NEEDLE_GLOB_MATCH", path=str(f), glob="*.py")
    _assert_real_match(result, "NEEDLE_GLOB_MATCH")


def test_single_file_nonmatching_glob_is_no_result_not_error(tmp_path):
    f = _write(tmp_path / "foo.py", "NEEDLE_GLOB_MISS\n")
    search_code = _search()
    result = search_code("NEEDLE_GLOB_MISS", path=str(f), glob="*.md")
    assert not result.startswith("[Error")
    assert "No matches" in result


def test_single_file_no_glob_still_searched(tmp_path):
    f = _write(tmp_path / "foo.py", "NEEDLE_GLOB_NONE\n")
    search_code = _search()
    result = search_code("NEEDLE_GLOB_NONE", path=str(f))
    _assert_real_match(result, "NEEDLE_GLOB_NONE")


# ── Targets ──────────────────────────────────────────────────────────────

def test_directory_target(tmp_path):
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "a.py", "NEEDLE_TWELVE\n")
    search_code = _search()
    result = search_code("NEEDLE_TWELVE", path=str(tmp_path))
    _assert_real_match(result, "NEEDLE_TWELVE")
    assert os.path.join("sub", "a.py") in result


def test_single_file_target(tmp_path):
    f = _write(tmp_path / "a.py", "NEEDLE_THIRTEEN\n")
    search_code = _search()
    result = search_code("NEEDLE_THIRTEEN", path=str(f))
    assert result.startswith("a.py:1")


def test_missing_target(tmp_path):
    search_code = _search()
    result = search_code("x", path=str(tmp_path / "does_not_exist.py"))
    assert result.startswith("[Error: path not found:")


# ── Project/path semantics ──────────────────────────────────────────────

def test_relative_path_with_active_project(tmp_path):
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "a.py", "NEEDLE_FOURTEEN\n")
    search_code = _search(_active_state(tmp_path))
    result = search_code("NEEDLE_FOURTEEN", path="sub")
    _assert_real_match(result, "NEEDLE_FOURTEEN")


def test_absolute_path_overrides_active_project(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}.py"
    outside.write_text("NEEDLE_FIFTEEN\n")
    try:
        search_code = _search(_active_state(tmp_path))
        result = search_code("NEEDLE_FIFTEEN", path=str(outside))
        _assert_real_match(result, "NEEDLE_FIFTEEN")
    finally:
        outside.unlink()


def test_no_active_project_preserves_legacy_cwd_relative(tmp_path, monkeypatch):
    _write(tmp_path / "a.py", "NEEDLE_SIXTEEN\n")
    monkeypatch.chdir(tmp_path)
    search_code = _search(project_state=None)
    result = search_code("NEEDLE_SIXTEEN", path="a.py")
    _assert_real_match(result, "NEEDLE_SIXTEEN")


def test_dotdot_escapes_active_project_lexically(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    _write(tmp_path / "outside.py", "NEEDLE_SEVENTEEN\n")
    search_code = _search(_active_state(project_root))
    result = search_code("NEEDLE_SEVENTEEN", path="../outside.py")
    _assert_real_match(result, "NEEDLE_SEVENTEEN")  # Mode A: not confined, lexical escape allowed


# ── Query validation ──────────────────────────────────────────────────────

def test_empty_query_rejected(tmp_path):
    search_code = _search()
    result = search_code("", path=str(tmp_path))
    assert result == "[Error: query must not be empty or whitespace-only]"


def test_whitespace_only_query_rejected(tmp_path):
    search_code = _search()
    result = search_code("   ", path=str(tmp_path))
    assert result == "[Error: query must not be empty or whitespace-only]"


def test_context_lines_out_of_range_rejected(tmp_path):
    search_code = _search()
    result = search_code("x", path=str(tmp_path), context_lines=11)
    assert result.startswith("[Error: context_lines")


# ── Bounding / truncation ─────────────────────────────────────────────────

def test_deterministic_match_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 10_000_000)  # isolate from char budget
    n = MAX_MATCHES + 50
    for i in range(n):
        _write(tmp_path / f"f{i}.py", "NEEDLE_CAP\n")
    search_code = _search()
    result = search_code("NEEDLE_CAP", path=str(tmp_path), context_lines=0)
    assert result.count("> ") == MAX_MATCHES
    assert f"showing {MAX_MATCHES} of {n} matches" in result


def test_character_budget_truncation(tmp_path, monkeypatch):
    for i in range(50):
        _write(tmp_path / f"f{i}.py", "NEEDLE_BUDGET\n")
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 400)  # forces truncation well before MAX_MATCHES
    search_code = _search()
    result = search_code("NEEDLE_BUDGET", path=str(tmp_path), context_lines=0)
    assert "[truncated: showing" in result
    shown = int(result.split("showing ")[1].split(" of ")[0])
    total = int(result.split(" of ")[1].split(" matches")[0])
    assert 0 < shown < total == 50


def test_truncation_footer_counts_are_true(tmp_path, monkeypatch):
    for i in range(30):
        _write(tmp_path / f"f{i}.py", "NEEDLE_TRUE\n")
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 500)
    search_code = _search()
    result = search_code("NEEDLE_TRUE", path=str(tmp_path), context_lines=0)
    shown = int(result.split("showing ")[1].split(" of ")[0])
    total = int(result.split(" of ")[1].split(" matches")[0])
    assert total == 30
    assert shown == result.count("> ")


def test_no_mid_window_cutoff_on_truncation(tmp_path, monkeypatch):
    for i in range(30):
        _write(tmp_path / f"f{i}.py", "NEEDLE_WHOLE\n")
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 500)
    search_code = _search()
    result = search_code("NEEDLE_WHOLE", path=str(tmp_path), context_lines=0)
    assert "[truncated:" in result  # precondition -- this test only proves something if truncation actually fired
    body = result.split("\n\n[truncated:")[0]
    blocks = body.split("\n\n")
    for b in blocks:
        header, line = b.split("\n")
        assert header.endswith(":1")
        assert line == "> 1: NEEDLE_WHOLE"  # every rendered block is complete, none clipped


# ── Truncation footer ceiling-fitting (CODING-03A1C) ─────────────────────
#
# A1 reserved scan-phase headroom for the footer but appended it after the
# fact with no proof it actually fit inside the real, live
# config.TOOL_RESULT_MAX_CHARS -- a small-enough ceiling could produce a
# result longer than the configured limit, silently relying on
# core/context.py's own downstream slice to save it. These prove the real
# invariant directly: len(search_code(...)) <= max(0, ceiling) always, for
# every truncated path, with no help from core/context.py.

def _make_matches(tmp_path, n, needle="NEEDLE_CEILING"):
    for i in range(n):
        _write(tmp_path / f"f{i}.py", f"{needle}\n")


def test_full_footer_fits_normal_production_ceiling(tmp_path, monkeypatch):
    _make_matches(tmp_path, 30)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 500)
    search_code = _search()
    result = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert len(result) <= 500
    assert "[truncated: showing" in result
    shown = int(result.split("showing ")[1].split(" of ")[0])
    total = int(result.split(" of ")[1].split(" matches")[0])
    assert total == 30
    assert shown == result.count("> ")


def test_ceiling_exactly_fits_full_footer(tmp_path, monkeypatch):
    _make_matches(tmp_path, 5)
    # A ceiling well under the scan-phase reserve forces zero rendered
    # blocks, so the footer alone (N=0) is the whole result -- measure its
    # real length instead of hardcoding the literal footer text.
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 199)
    search_code = _search()
    probe = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert probe.startswith("[truncated: showing 0 of 5")
    exact_ceiling = len(probe)

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", exact_ceiling)
    result = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert result == probe
    assert len(result) <= exact_ceiling


def test_ceiling_just_below_full_footer_degrades(tmp_path, monkeypatch):
    _make_matches(tmp_path, 5)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 199)
    search_code = _search()
    probe = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    full_footer_len = len(probe)

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", full_footer_len - 1)
    result = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert result == "[truncated]"
    assert len(result) <= full_footer_len - 1


def test_ceiling_fits_compact_marker_not_full_footer(tmp_path, monkeypatch):
    _make_matches(tmp_path, 5)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 20)
    search_code = _search()
    result = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert result == "[truncated]"
    assert len(result) <= 20


def test_ceiling_of_one_returns_ellipsis(tmp_path, monkeypatch):
    _make_matches(tmp_path, 5)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 1)
    search_code = _search()
    result = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert result == "…"
    assert len(result) <= 1


def test_ceiling_of_zero_returns_empty_string(tmp_path, monkeypatch):
    _make_matches(tmp_path, 5)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 0)
    search_code = _search()
    result = search_code("NEEDLE_CEILING", path=str(tmp_path), context_lines=0)
    assert result == ""
    assert len(result) <= 0


def test_no_matches_is_informative_not_error(tmp_path):
    _write(tmp_path / "a.py", "nothing here\n")
    search_code = _search()
    result = search_code("NEEDLE_ABSENT", path=str(tmp_path))
    assert not result.startswith("[Error")
    assert "No matches" in result


# ── Binary / encoding ───────────────────────────────────────────────────

def test_binary_file_skipped_in_directory_search(tmp_path):
    (tmp_path / "bin.dat").write_bytes(bytes(range(256)))
    _write(tmp_path / "a.py", "NEEDLE_BINARY\n")
    search_code = _search()
    result = search_code("NEEDLE_BINARY", path=str(tmp_path))
    assert "bin.dat" not in result
    _assert_real_match(result, "NEEDLE_BINARY")


def test_direct_binary_file_target_is_truthful(tmp_path):
    f = tmp_path / "bin.dat"
    f.write_bytes(bytes(range(256)))
    search_code = _search()
    result = search_code("x", path=str(f))
    assert result.startswith("[Skipped: binary file, not searched:")


def test_invalid_utf8_degrades_without_crash(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"before \xff\xfe NEEDLE_UTF8 after\n")
    search_code = _search()
    result = search_code("NEEDLE_UTF8", path=str(tmp_path))
    _assert_real_match(result, "NEEDLE_UTF8")


# ── Symlinks ──────────────────────────────────────────────────────────────

def test_symlinked_directory_not_recursed(tmp_path):
    real = tmp_path / "real_target"
    real.mkdir()
    _write(real / "deep.py", "NEEDLE_SYMLINK_DIR\n")
    os.symlink(real, tmp_path / "linked_dir", target_is_directory=True)
    search_code = _search()
    result = search_code("NEEDLE_SYMLINK_DIR", path=str(tmp_path), context_lines=0)
    assert result.count("> ") == 1  # found once via the real path, not duplicated via the symlink


def test_symlinked_file_is_searchable(tmp_path):
    real = _write(tmp_path / "real.py", "NEEDLE_SYMLINK_FILE\n")
    os.symlink(real, tmp_path / "linked.py")
    search_code = _search()
    result = search_code("NEEDLE_SYMLINK_FILE", path=str(tmp_path), context_lines=0)
    assert result.count("> ") == 2  # both the real file and the symlink are ordinary files here


# ── Directory exclusion ────────────────────────────────────────────────────

def test_git_directory_excluded(tmp_path):
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".git" / "config", "NEEDLE_GIT\n")
    search_code = _search()
    result = search_code("NEEDLE_GIT", path=str(tmp_path))
    assert "No matches" in result


def test_pycache_directory_excluded(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    _write(tmp_path / "__pycache__" / "a.pyc", "NEEDLE_PYCACHE\n")
    search_code = _search()
    result = search_code("NEEDLE_PYCACHE", path=str(tmp_path))
    assert "No matches" in result


def test_github_directory_is_searchable(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    _write(tmp_path / ".github" / "workflows" / "tests.yml", "NEEDLE_GITHUB\n")
    search_code = _search()
    result = search_code("NEEDLE_GITHUB", path=str(tmp_path))
    # A0's proven gap vs legacy search_files -- must not regress
    _assert_real_match(result, "NEEDLE_GITHUB")


# ── Deterministic traversal order (CODING-03A1C) ─────────────────────────
#
# A1 used ambient os.walk ordering (filesystem-dependent, not
# content-dependent) -- review reproduced non-lexical result order.
# Directories and files below are deliberately created out of order so a
# passing result actually proves the explicit sort is doing the work,
# rather than the fixture accidentally landing in sorted order already.

def test_traversal_order_is_lexical_and_deterministic(tmp_path):
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "mu").mkdir()
    _write(tmp_path / "zeta" / "z2.py", "NEEDLE_ORDER\n")
    _write(tmp_path / "zeta" / "z1.py", "NEEDLE_ORDER\n")
    _write(tmp_path / "alpha" / "a2.py", "NEEDLE_ORDER\n")
    _write(tmp_path / "alpha" / "a1.py", "NEEDLE_ORDER\n")
    _write(tmp_path / "mu" / "m1.py", "NEEDLE_ORDER\n")
    _write(tmp_path / "top2.py", "NEEDLE_ORDER\n")
    _write(tmp_path / "top1.py", "NEEDLE_ORDER\n")

    search_code = _search()
    result = search_code("NEEDLE_ORDER", path=str(tmp_path), context_lines=0)

    # Header lines only -- exclude match-line ("> ") and blank separator lines.
    found_order = [
        line.split(":", 1)[0]
        for line in result.split("\n")
        if line and not line.startswith((">", " ", "["))
    ]
    # os.walk is topdown: a directory's own files are yielded before its
    # subdirectories are recursed into, and dirnames/filenames are each
    # sorted independently at every level -- not a single flat alphabetical
    # merge of full relative paths.
    expected_order = [
        "top1.py", "top2.py",
        os.path.join("alpha", "a1.py"), os.path.join("alpha", "a2.py"),
        os.path.join("mu", "m1.py"),
        os.path.join("zeta", "z1.py"), os.path.join("zeta", "z2.py"),
    ]
    assert found_order == expected_order

    result2 = search_code("NEEDLE_ORDER", path=str(tmp_path), context_lines=0)
    assert result == result2  # byte-identical across independent calls


# ── Schema ──────────────────────────────────────────────────────────────

def test_schema_contains_only_model_facing_arguments():
    registry = ToolRegistry()
    register_search_tools(registry, project_state=_active_state("/tmp"))
    schema = registry.get_schemas(["search_code"])[0]
    props = set(schema["function"]["parameters"]["properties"].keys())
    assert props == {"query", "path", "glob", "regex", "context_lines"}
    assert "project_state" not in props


# ── Security / profile ────────────────────────────────────────────────────

def test_search_code_classified_read_only():
    assert TOOL_TIERS["search_code"] == "read_only"


def test_coding_profile_includes_search_code_and_keeps_search_files():
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    enabled = set(coding.get("enabled", []))
    assert "search_code" in enabled
    assert "search_files" in enabled


def test_active_project_alone_does_not_grant_child_search_code():
    registry = ToolRegistry()
    register_search_tools(registry, project_state=_active_state("/tmp"))
    apply_tool_profile(registry, profile_name=None, tools_enabled=[], owner=False)
    assert not registry.is_enabled("search_code")


def test_explicitly_granted_child_can_use_search_code():
    registry = ToolRegistry()
    register_search_tools(registry, project_state=_active_state("/tmp"))
    apply_tool_profile(registry, profile_name=None, tools_enabled=["search_code"], owner=False)
    assert registry.is_enabled("search_code")
    assert TOOL_TIERS["search_code"] not in {"execute", "self_modifying", "outbound_action"}
