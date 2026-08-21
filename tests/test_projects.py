"""tools/projects.py -- CODING-02B-A.

Exercises the real registered closures (register_projects_tools), same
_CapturingRegistry convention as test_filesystem.py/test_terminal.py, not a
reimplementation. Covers: create_project's validated name + local-only
binding (no execution root in tracked Markdown), set_project_root,
activate_project/get_active_project/clear_active_project, and confirms the
old tracked-root-as-fallback failure mode CODING-02A found is now closed.
"""
import json
import os

import pytest

import core.project_context as pc
import core.secrets as secrets_module
import tools.projects as tp
from core import persistence
from core.agent import LuminaAgent
from core.project_context import ProjectContext, ProjectContextState
from core.tool_profiles import OWNER_ONLY_TOOLS, apply_tool_profile
from tools.projects import register_projects_tools
from tools.registry import ToolRegistry


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn


@pytest.fixture(autouse=True)
def _isolated_projects_dirs(tmp_path, monkeypatch):
    """Every test gets fully isolated tracked-projects/, DATA_DIR chats/,
    and DATA_DIR bindings/ directories -- never touches the real repo's
    projects/ tree or the real DATA_DIR on this machine."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    projectlist = projects_dir / "projectlist.md"
    projectlist.write_text("# Projects\n\n")

    monkeypatch.setattr(tp, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(tp, "PROJECTLIST", str(projectlist))
    monkeypatch.setattr(tp, "PROJECT_CHATS_DIR", str(tmp_path / "data_projects"))
    monkeypatch.setattr(pc, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings"))


def _tools(project_state=None):
    reg = _CapturingRegistry()
    register_projects_tools(reg, project_state=project_state)
    return reg.fns


def _make_source_root(tmp_path, subdir="src"):
    root = tmp_path / subdir
    root.mkdir()
    return root


# ── create_project: validation + local-only binding ──────────────────────

def test_create_project_validates_name(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    result = tools["create_project"]("bad name!", "desc", str(root))
    assert result.startswith("[Error:")
    assert not os.path.exists(os.path.join(tp.PROJECTS_DIR, "bad name!"))


@pytest.mark.parametrize("bad_name", ["../evil", "/etc/cron.d", "..", "."])
def test_create_project_blocks_traversal_and_absolute_names(tmp_path, bad_name):
    tools = _tools()
    root = _make_source_root(tmp_path)
    result = tools["create_project"](bad_name, "desc", str(root))
    assert result.startswith("[Error:")


def test_create_project_writes_binding_locally(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    result = tools["create_project"]("demo", "desc", str(root))
    assert "created" in result.lower()

    binding_path = os.path.join(str(tmp_path / "bindings"), "demo", "binding.json")
    assert os.path.exists(binding_path)
    with open(binding_path) as f:
        data = json.load(f)
    assert data["root"] == str(root)


def test_new_project_md_contains_no_execution_root(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    project_md = os.path.join(tp.PROJECTS_DIR, "demo", "project.md")
    content = open(project_md).read()
    assert "Root" not in content
    assert str(root) not in content


def test_new_projectlist_md_contains_no_execution_root(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    content = open(tp.PROJECTLIST).read()
    assert "demo" in content
    assert str(root) not in content
    assert "`" not in content  # old format backtick-quoted the root path


def test_refresh_index_contains_no_machine_root(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    (root / "a.py").write_text("x = 1\n")

    result = tools["refresh_codebase_index"]("demo", str(root))
    assert "indexed" in result.lower()

    codebase_md = os.path.join(tp.PROJECTS_DIR, "demo", "codebase.md")
    content = open(codebase_md).read()
    assert "Root" not in content
    assert str(root) not in content
    assert "a.py" in content  # the walk itself still worked


def test_create_project_reports_partial_state_on_bad_root(tmp_path):
    tools = _tools()
    missing = tmp_path / "does-not-exist"
    result = tools["create_project"]("demo", "desc", str(missing))
    assert result.startswith("[Error:")
    # Nothing should have been written at all -- root validation happens
    # before any tracked file write.
    assert not os.path.exists(os.path.join(tp.PROJECTS_DIR, "demo"))


# ── set_project_root ──────────────────────────────────────────────────────

def test_set_project_root_requires_existing_project(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    result = tools["set_project_root"]("nonexistent", str(root))
    assert result.startswith("[Error:")
    assert "create it first" in result.lower()


def test_set_project_root_is_data_dir_local_not_tracked(tmp_path):
    tools = _tools()
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))

    other_root = _make_source_root(tmp_path, "other_src")
    result = tools["set_project_root"]("demo", str(other_root))
    assert "configured" in result.lower()

    # Tracked project.md is untouched by this call.
    project_md = os.path.join(tp.PROJECTS_DIR, "demo", "project.md")
    assert str(other_root) not in open(project_md).read()

    # The DATA_DIR-local binding reflects the new root.
    binding_path = os.path.join(str(tmp_path / "bindings"), "demo", "binding.json")
    assert json.load(open(binding_path))["root"] == str(other_root)


def test_set_project_root_does_not_activate(tmp_path):
    state = ProjectContextState()
    tools = _tools(project_state=state)
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    assert state.get() is None  # create_project never activates either

    tools["set_project_root"]("demo", str(root))
    assert state.get() is None  # still not activated


# ── activate_project / get_active_project / clear_active_project ────────

def test_activation_success(tmp_path):
    state = ProjectContextState()
    tools = _tools(project_state=state)
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))

    result = tools["activate_project"]("demo")
    assert "Active project: demo" in result
    assert str(root) in result
    ctx = state.get()
    assert ctx == ProjectContext(name="demo", root=str(root))


def test_activation_missing_binding_fails_closed(tmp_path):
    """Project tracked (project.md exists) but never bound to a local root
    -- must fail truthfully, never guess."""
    state = ProjectContextState()
    tools = _tools(project_state=state)
    os.makedirs(os.path.join(tp.PROJECTS_DIR, "unbound"))
    with open(os.path.join(tp.PROJECTS_DIR, "unbound", "project.md"), "w") as f:
        f.write("# unbound\n")

    result = tools["activate_project"]("unbound")
    assert result.startswith("[Error:")
    assert "no local execution root configured" in result.lower()
    assert state.get() is None


def test_activation_stale_binding_fails_closed(tmp_path):
    state = ProjectContextState()
    tools = _tools(project_state=state)
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))

    # The bound root vanishes after the binding was created.
    os.rmdir(str(root))

    result = tools["activate_project"]("demo")
    assert result.startswith("[Error:")
    assert state.get() is None


def test_activation_unknown_project_fails_closed():
    state = ProjectContextState()
    tools = _tools(project_state=state)
    result = tools["activate_project"]("never-heard-of-it")
    assert result.startswith("[Error:")
    assert state.get() is None


def test_activation_ignores_stale_tracked_root_line(tmp_path):
    """CODING-02A's central finding: a tracked project.md's **Root:** line
    can be (and, on the real release tree, was) wrong for the tree it lives
    in. activate_project must never parse or trust it -- only a real local
    binding.json counts."""
    project_dir = os.path.join(tp.PROJECTS_DIR, "legacy")
    os.makedirs(project_dir)
    with open(os.path.join(project_dir, "project.md"), "w") as f:
        f.write("# legacy\n\n**Description:** old-style\n**Root:** /totally/wrong/path\n")

    state = ProjectContextState()
    tools = _tools(project_state=state)
    result = tools["activate_project"]("legacy")
    assert result.startswith("[Error:")
    assert "no local execution root configured" in result.lower()
    assert state.get() is None
    assert "/totally/wrong/path" not in result


def test_get_active_project_reports_none_and_active(tmp_path):
    state = ProjectContextState()
    tools = _tools(project_state=state)
    assert "no active project" in tools["get_active_project"]().lower()

    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    tools["activate_project"]("demo")
    result = tools["get_active_project"]()
    assert "demo" in result
    assert str(root) in result


def test_clear_active_project(tmp_path):
    state = ProjectContextState()
    tools = _tools(project_state=state)
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    tools["activate_project"]("demo")
    assert state.get() is not None

    result = tools["clear_active_project"]()
    assert "cleared" in result.lower()
    assert state.get() is None
    assert "no active project" in tools["get_active_project"]().lower()


def test_two_state_holders_do_not_cross_contaminate(tmp_path):
    state_a = ProjectContextState()
    state_b = ProjectContextState()
    tools_a = _tools(project_state=state_a)
    tools_b = _tools(project_state=state_b)

    root = _make_source_root(tmp_path)
    tools_a["create_project"]("demo", "desc", str(root))
    tools_a["activate_project"]("demo")

    assert state_a.get() is not None
    assert state_b.get() is None
    assert "no active project" in tools_b["get_active_project"]().lower()


def test_activate_project_without_state_holder_reports_error(tmp_path):
    """register_projects_tools(registry) with no project_state (the
    pre-CODING-02B-A call shape) must fail cleanly rather than crash."""
    tools = _tools(project_state=None)
    root = _make_source_root(tmp_path)
    tools["create_project"]("demo", "desc", str(root))
    result = tools["activate_project"]("demo")
    assert result.startswith("[Error:")


# ── CODING-02B-A1: create_project owner-only binding-authority fix ──────
#
# create_project(name, description, root_path) calls the exact same
# save_project_binding() set_project_root uses to write
# DATA_DIR/projects/<name>/binding.json -- CODING-02B-A made set_project_root
# owner-only but left create_project unclassified, leaving a second,
# un-gated path to the identical privileged write. These prove the real
# non-owner enforcement path (core/tool_profiles.py's apply_tool_profile(),
# the same function apply_persona()/Settings/headless.py all use) actually
# closes it -- not just the static OWNER_ONLY_TOOLS set membership already
# covered in tests/test_tool_profiles.py.

@pytest.fixture
def isolated_agent_data(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))


def test_nonowner_explicit_grant_cannot_regain_create_project():
    """A profile that explicitly lists create_project in tools_enabled must
    still lose it for a non-owner session -- proves the hard
    OWNER_ONLY_TOOLS strip in apply_tool_profile(), not merely a PIN block.
    Uses the real ToolRegistry + register_projects_tools + apply_tool_profile
    path, not a reimplementation."""
    registry = ToolRegistry()
    register_projects_tools(registry)

    apply_tool_profile(registry, tools_enabled=["create_project"], owner=False)

    assert "create_project" not in registry.list_enabled()
    result = registry.call("create_project", {
        "name": "bypass-attempt", "description": "x", "root_path": "/tmp",
    })
    assert "disabled" in result.lower()


def test_nonowner_agent_cannot_create_project_binding_via_registry_call(isolated_agent_data, tmp_path):
    """The actual security consequence: a real non-owner LuminaAgent,
    explicitly (mis)granted create_project through the real
    apply_tool_profile() path, cannot create a tracked project directory or
    a machine-local binding.json by dispatching through
    ToolRegistry.call() -- the same call path a live model uses."""
    agent = LuminaAgent(owner=False, channel_id="coding-02b-a1-bypass-test", backend="llamacpp")
    apply_tool_profile(agent.registry, tools_enabled=["create_project"], owner=False)

    assert "create_project" not in agent.registry.list_enabled()

    source_root = _make_source_root(tmp_path)
    result = agent.registry.call("create_project", {
        "name": "bypass-attempt",
        "description": "attempted non-owner project creation",
        "root_path": str(source_root),
    })

    assert "disabled" in result.lower()
    assert not os.path.exists(os.path.join(tp.PROJECTS_DIR, "bypass-attempt"))
    assert not os.path.exists(
        os.path.join(pc.PROJECT_BINDINGS_DIR, "bypass-attempt", "binding.json")
    )


def test_set_project_root_and_create_project_both_owner_only():
    assert "set_project_root" in OWNER_ONLY_TOOLS
    assert "create_project" in OWNER_ONLY_TOOLS


def test_other_project_tools_still_not_owner_only():
    for name in ("load_project", "update_project", "refresh_codebase_index",
                 "load_codebase", "link_chat", "get_project_chats",
                 "activate_project", "clear_active_project", "get_active_project"):
        assert name not in OWNER_ONLY_TOOLS


def test_owner_create_project_still_works_end_to_end(tmp_path):
    """No product-behavior change for the owner -- create_project must
    still validate, create tracked identity, and bind the root exactly as
    CODING-02B-A left it."""
    tools = _tools()
    root = _make_source_root(tmp_path)
    result = tools["create_project"]("still-works", "desc", str(root))
    assert "created" in result.lower()
    assert "bound" in result.lower()
    assert os.path.isdir(os.path.join(tp.PROJECTS_DIR, "still-works"))
    binding = pc.load_project_binding("still-works")
    assert binding.root == str(root)


# ── CODING-02B-D: set_project_root owner-only behavioral regression ─────
#
# CODING-02B-A/A1 already hard-excluded set_project_root via OWNER_ONLY_TOOLS
# and covered it with a static membership assertion
# (tests/test_tool_profiles.py::test_set_project_root_is_owner_only). What
# was missing was a real-dispatch behavioral proof, mirroring the
# create_project regressions above. This exercises the stronger consequence
# CODING-02B-D section 3 prefers over a fresh-binding check: an existing
# tracked project with a real binding already pointing at one root, a
# non-owner session explicitly granted set_project_root attempting to
# *repoint* it at a different valid root, and confirmation the binding is
# completely untouched afterward.

def test_nonowner_agent_cannot_modify_existing_binding_via_set_project_root(isolated_agent_data, tmp_path):
    """Real non-owner LuminaAgent, real apply_tool_profile(owner=False),
    real registry.call() -- the same dispatch path a live model uses.
    Proves set_project_root cannot be regained by explicit grant, and that
    an existing machine-local binding cannot be silently repointed through
    it."""
    tools = _tools()
    root_a = _make_source_root(tmp_path, "root-a")
    root_b = _make_source_root(tmp_path, "root-b")
    setup = tools["create_project"]("existing-proj", "desc", str(root_a))
    assert "bound" in setup.lower()
    assert pc.load_project_binding("existing-proj").root == str(root_a)

    agent = LuminaAgent(owner=False, channel_id="coding-02b-d-bypass-test", backend="llamacpp")
    apply_tool_profile(agent.registry, tools_enabled=["set_project_root"], owner=False)

    assert "set_project_root" not in agent.registry.list_enabled()
    result = agent.registry.call("set_project_root", {
        "name": "existing-proj", "root_path": str(root_b),
    })
    assert "disabled" in result.lower()

    # The real consequence: the pre-existing binding must be completely
    # untouched -- still root_a, never repointed to root_b.
    assert pc.load_project_binding("existing-proj").root == str(root_a)
