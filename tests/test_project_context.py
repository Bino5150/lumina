"""core/project_context.py -- CODING-02B-A.

Covers the ProjectContext/ProjectContextState types, project-name
validation, the machine-local binding.json store, and resolve_project_path()'s
lexical (never-realpath) resolution semantics.
"""
import dataclasses
import json
import os

import pytest

import core.project_context as pc
from core.project_context import (
    BindingNotFound,
    BindingRootInvalid,
    MalformedBinding,
    ProjectBindingError,
    ProjectContext,
    ProjectContextState,
    load_project_binding,
    resolve_project_path,
    save_project_binding,
    validate_project_name,
)


@pytest.fixture(autouse=True)
def _isolated_bindings_dir(tmp_path, monkeypatch):
    """Every test in this file gets its own throwaway PROJECT_BINDINGS_DIR --
    never touches the real DATA_DIR/projects/ on this machine. Same
    monkeypatch-the-module-constant convention core/persistence.py's
    PREFS_PATH already uses (see tests/test_file_edit_integration.py's
    isolated_agent_data fixture)."""
    bindings_dir = tmp_path / "bindings"
    monkeypatch.setattr(pc, "PROJECT_BINDINGS_DIR", str(bindings_dir))
    return bindings_dir


# ── ProjectContext: frozen ───────────────────────────────────────────────

def test_project_context_is_frozen():
    ctx = ProjectContext(name="lumina", root="/tmp/lumina")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.root = "/tmp/elsewhere"


def test_project_context_equality_by_value():
    a = ProjectContext(name="lumina", root="/tmp/lumina")
    b = ProjectContext(name="lumina", root="/tmp/lumina")
    assert a == b
    assert a is not b


# ── ProjectContextState: independent instances ──────────────────────────

def test_two_state_holders_are_independent_instances():
    a = ProjectContextState()
    b = ProjectContextState()
    ctx = ProjectContext(name="lumina", root="/tmp/lumina")
    a.set(ctx)
    assert a.get() == ctx
    assert b.get() is None


def test_set_get_clear_roundtrip():
    state = ProjectContextState()
    assert state.get() is None
    ctx = ProjectContext(name="oracle", root="/tmp/oracle")
    state.set(ctx)
    assert state.get() == ctx
    state.clear()
    assert state.get() is None


def test_set_rejects_non_project_context():
    state = ProjectContextState()
    with pytest.raises(TypeError):
        state.set("not a ProjectContext")


def test_snapshot_matches_get_and_is_stable_after_later_clear():
    """Proves snapshot() captures the value at call time -- a later clear()
    on the same holder must not retroactively change what was already
    snapshotted (relevant for CODING-02B-B's capture-at-dispatch design)."""
    state = ProjectContextState()
    ctx = ProjectContext(name="lumina", root="/tmp/lumina")
    state.set(ctx)
    snapshot = state.snapshot()
    assert snapshot == ctx
    state.clear()
    assert snapshot == ctx  # the captured value, untouched
    assert state.get() is None  # the live holder, actually cleared


def test_constructing_with_initial_context_seeds_state():
    ctx = ProjectContext(name="lumina", root="/tmp/lumina")
    state = ProjectContextState(ctx)
    assert state.get() == ctx


# ── Name validation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["lumina", "lumina-dev", "oracle", "motunes", "Foo123", "a", "a1_b-2"])
def test_valid_historical_and_ordinary_names_preserved(name):
    assert validate_project_name(name) == name


@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_empty_or_whitespace_name_rejected(name):
    with pytest.raises(ValueError, match="empty"):
        validate_project_name(name)


def test_leading_trailing_whitespace_rejected():
    with pytest.raises(ValueError, match="whitespace"):
        validate_project_name(" lumina ")


@pytest.mark.parametrize("name", ["/etc/cron.d", "/tmp/evil"])
def test_absolute_name_rejected(name):
    with pytest.raises(ValueError, match="absolute"):
        validate_project_name(name)


@pytest.mark.parametrize("name", ["../../../tmp/evil", "foo/../bar", "a/b", "a\\b"])
def test_separator_or_traversal_name_rejected(name):
    with pytest.raises(ValueError):
        validate_project_name(name)


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_and_dotdot_name_rejected(name):
    with pytest.raises(ValueError):
        validate_project_name(name)


def test_tilde_prefixed_name_rejected():
    with pytest.raises(ValueError, match="~"):
        validate_project_name("~root")


@pytest.mark.parametrize("name", ["foo bar", "foo!", "foo.bar", "-leading-dash"])
def test_other_invalid_characters_rejected(name):
    with pytest.raises(ValueError):
        validate_project_name(name)


# ── Case-collision detection ─────────────────────────────────────────────

def test_case_collision_rejected_on_save(tmp_path):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()

    save_project_binding("Foo", str(root_a))
    with pytest.raises(ValueError, match="collides case-insensitively"):
        save_project_binding("foo", str(root_b))


def test_resaving_same_name_is_not_a_collision(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    save_project_binding("lumina", str(root))
    # Re-saving the identical name must not trip the collision guard.
    ctx = save_project_binding("lumina", str(root))
    assert ctx.name == "lumina"


# ── Binding store: save/load ─────────────────────────────────────────────

def test_save_then_load_roundtrip(tmp_path):
    root = tmp_path / "myroot"
    root.mkdir()
    saved = save_project_binding("lumina", str(root))
    loaded = load_project_binding("lumina")
    assert saved == loaded
    assert loaded.root == str(root)


def test_save_canonicalizes_relative_and_dotted_root(tmp_path):
    root = tmp_path / "a" / "b"
    root.mkdir(parents=True)
    messy = str(root) + "/../b/."
    ctx = save_project_binding("lumina", messy)
    assert ctx.root == str(root)
    assert os.path.isabs(ctx.root)


def test_save_rejects_nonexistent_root(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(BindingRootInvalid):
        save_project_binding("lumina", str(missing))


def test_save_rejects_file_as_root(tmp_path):
    a_file = tmp_path / "im_a_file.txt"
    a_file.write_text("not a directory")
    with pytest.raises(BindingRootInvalid):
        save_project_binding("lumina", str(a_file))


def test_load_missing_binding_raises_binding_not_found():
    with pytest.raises(BindingNotFound):
        load_project_binding("never-created")


def test_load_malformed_json_raises_malformed_binding(tmp_path):
    proj_dir = tmp_path / "bindings" / "lumina"
    proj_dir.mkdir(parents=True)
    (proj_dir / "binding.json").write_text("{not valid json")
    with pytest.raises(MalformedBinding):
        load_project_binding("lumina")


def test_load_binding_missing_root_key_raises_malformed_binding(tmp_path):
    proj_dir = tmp_path / "bindings" / "lumina"
    proj_dir.mkdir(parents=True)
    (proj_dir / "binding.json").write_text(json.dumps({"not_root": "x"}))
    with pytest.raises(MalformedBinding):
        load_project_binding("lumina")


def test_load_stale_root_raises_binding_root_invalid(tmp_path):
    root = tmp_path / "vanishing"
    root.mkdir()
    save_project_binding("lumina", str(root))
    # The bound directory later disappears -- a stale binding must not
    # silently "activate" against a root that no longer exists.
    os.rmdir(str(root))
    with pytest.raises(BindingRootInvalid):
        load_project_binding("lumina")


def test_binding_write_is_atomic_same_directory_pattern(tmp_path):
    root = tmp_path / "myroot"
    root.mkdir()
    save_project_binding("lumina", str(root))
    binding_path = tmp_path / "bindings" / "lumina" / "binding.json"
    assert binding_path.exists()
    # No leftover .tmp file after a successful save.
    assert not (tmp_path / "bindings" / "lumina" / "binding.json.tmp").exists()


def test_atomic_write_failure_preserves_prior_binding(tmp_path, monkeypatch):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()

    save_project_binding("lumina", str(root_a))
    binding_path = tmp_path / "bindings" / "lumina" / "binding.json"
    before = binding_path.read_text()

    def _boom(*a, **kw):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(ProjectBindingError):
        save_project_binding("lumina", str(root_b))

    # Read the on-disk file directly (not through load_project_binding(),
    # which would still work fine here too, but a direct read keeps this
    # test decoupled from that function's own correctness).
    assert binding_path.read_text() == before
    assert json.loads(before)["root"] == str(root_a)


# ── resolve_project_path() ────────────────────────────────────────────────

def _state_with_root(root):
    state = ProjectContextState()
    state.set(ProjectContext(name="p", root=str(root)))
    return state


def test_relative_path_with_active_project_joins_root(tmp_path):
    state = _state_with_root(tmp_path)
    result = resolve_project_path("core/agent.py", state)
    assert result == os.path.abspath(os.path.join(str(tmp_path), "core/agent.py"))


def test_relative_path_with_no_active_project_preserves_legacy_behavior():
    result = resolve_project_path("core/agent.py", None)
    assert result == os.path.expanduser("core/agent.py")
    assert result == "core/agent.py"  # no leading '~', expanduser is a no-op
    assert not os.path.isabs(result)  # legacy tools never forced this absolute


def test_relative_path_with_empty_state_holder_preserves_legacy_behavior():
    empty_state = ProjectContextState()  # constructed, but nothing activated
    result = resolve_project_path("core/agent.py", empty_state)
    assert result == "core/agent.py"


def test_absolute_path_honored_even_with_active_project(tmp_path):
    state = _state_with_root(tmp_path)
    other = "/some/other/absolute/path.py"
    assert resolve_project_path(other, state) == other


def test_tilde_path_never_treated_as_project_relative(tmp_path):
    state = _state_with_root(tmp_path)
    result = resolve_project_path("~/some_file.py", state)
    assert result == os.path.expanduser("~/some_file.py")
    assert str(tmp_path) not in result


def test_dotdot_may_lexically_escape_active_root_mode_a(tmp_path):
    """Mode A is defaulting, not confinement -- a relative '../other/file'
    is allowed to lexically escape the active root. This is deliberate
    (CODING-02A section 8/9), not a bug."""
    project_root = tmp_path / "root"
    project_root.mkdir()
    state = _state_with_root(project_root)
    result = resolve_project_path("../other/file.txt", state)
    assert result == os.path.abspath(os.path.join(str(project_root), "../other/file.txt"))
    assert result == os.path.join(str(tmp_path), "other", "file.txt")
    # Escaped the project root entirely -- confirms no confinement is applied.
    assert not result.startswith(str(project_root))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_resolver_does_not_follow_symlink_components(tmp_path):
    """The critical safety property: resolve_project_path() must be purely
    lexical. If it called realpath() and collapsed a symlink component
    before edit_file's own kernel ever saw the path, CODING-02B-A would
    silently defeat CODING-01's deliberate symlink rejection."""
    project_root = tmp_path / "root"
    project_root.mkdir()
    real_target_dir = tmp_path / "real_target"
    real_target_dir.mkdir()
    (real_target_dir / "secret.txt").write_text("hi")

    link = project_root / "link"
    os.symlink(str(real_target_dir), str(link))

    state = _state_with_root(project_root)
    result = resolve_project_path("link/secret.txt", state)

    # Purely lexical join -- the returned path still names the symlink
    # component itself, it was never resolved through to real_target/.
    expected_lexical = os.path.abspath(os.path.join(str(project_root), "link/secret.txt"))
    assert result == expected_lexical
    assert result != os.path.realpath(expected_lexical)
    assert "real_target" not in result
