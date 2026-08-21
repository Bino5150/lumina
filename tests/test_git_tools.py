"""tools/git_status.py / git_diff.py / git_log.py / git_branches.py -- CODING-02B-B1.

Proves all four Git tools share core.git_repos.resolve_git_repo() behaviorally
(explicit-path compatibility, omitted-path active-Project defaulting,
unauthorized-active-root rejection), and that their registered schemas make
repo_path optional. Uses real, throwaway git repos under tmp_path with
core.git_repos._ALLOWED_REPOS monkeypatched to include them -- never touches
the real ~/lumina / ~/lumina-release repos.
"""
import os
import subprocess

import pytest

import core.git_repos as gr
from core.project_context import ProjectContext, ProjectContextState
from tools.git_status import git_status, register_git_status_tool
from tools.git_diff import git_diff, register_git_diff_tool
from tools.git_log import git_log, register_git_log_tool
from tools.git_branches import git_branches, register_git_branches_tool


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    authorized = tmp_path / "authorized-repo"
    authorized.mkdir()
    _init_repo(authorized)

    unauthorized = tmp_path / "unauthorized-repo"
    unauthorized.mkdir()
    _init_repo(unauthorized)

    monkeypatch.setattr(gr, "_ALLOWED_REPOS", {os.path.realpath(str(authorized))})
    return {"authorized": str(authorized), "unauthorized": str(unauthorized)}


TOOLS = [
    ("git_status", git_status, register_git_status_tool),
    ("git_diff", git_diff, register_git_diff_tool),
    ("git_log", git_log, register_git_log_tool),
    ("git_branches", git_branches, register_git_branches_tool),
]


@pytest.mark.parametrize("name,fn,_register", TOOLS, ids=[t[0] for t in TOOLS])
def test_explicit_path_compatibility(name, fn, _register, repo):
    result = fn(repo["authorized"])
    assert not result.startswith("Error")


@pytest.mark.parametrize("name,fn,_register", TOOLS, ids=[t[0] for t in TOOLS])
def test_explicit_unauthorized_path_still_rejected(name, fn, _register, repo):
    result = fn(repo["unauthorized"])
    assert result.startswith("Error")
    assert "allowlist" in result.lower()


@pytest.mark.parametrize("name,fn,_register", TOOLS, ids=[t[0] for t in TOOLS])
def test_omitted_path_uses_allowlisted_active_project(name, fn, _register, repo):
    state = ProjectContextState(ProjectContext(name="p", root=repo["authorized"]))
    result = fn(None, project_state=state)
    assert not result.startswith("Error")


@pytest.mark.parametrize("name,fn,_register", TOOLS, ids=[t[0] for t in TOOLS])
def test_omitted_path_rejects_unauthorized_active_project(name, fn, _register, repo):
    """The mandatory cross-check: an active Project pointed at a real,
    valid, but non-allowlisted root must still be rejected -- Project
    selection is not Git authorization, for all four tools alike."""
    state = ProjectContextState(ProjectContext(name="oracle", root=repo["unauthorized"]))
    result = fn(None, project_state=state)
    assert result.startswith("Error")
    assert "allowlist" in result.lower()


@pytest.mark.parametrize("name,fn,_register", TOOLS, ids=[t[0] for t in TOOLS])
def test_registered_schema_makes_repo_path_optional(name, fn, _register, repo):
    class _CapturingRegistry:
        def __init__(self):
            self.schema = None
        def register(self, **kw):
            self.schema = kw

    registry = _CapturingRegistry()
    state = ProjectContextState(ProjectContext(name="p", root=repo["authorized"]))
    _register(registry, project_state=state)

    assert registry.schema["name"] == name
    assert "repo_path" not in registry.schema["parameters"]["required"]

    # And the registered tool wrapper itself actually uses the injected
    # project_state when repo_path is omitted -- not just a schema change.
    result = registry.schema["fn"]()
    assert not result.startswith("Error")


def test_all_four_tools_use_the_same_shared_resolver():
    """Source-level guard against re-introducing a fifth divergent copy of
    the allowlist logic -- each module must import resolve_git_repo from
    core.git_repos rather than defining its own."""
    import tools.git_status, tools.git_diff, tools.git_log, tools.git_branches
    for mod in (tools.git_status, tools.git_diff, tools.git_log, tools.git_branches):
        assert mod.resolve_git_repo is gr.resolve_git_repo
        assert not hasattr(mod, "_ALLOWED_REPOS")
        assert not hasattr(mod, "_resolve_allowed_repo")


def test_two_real_agents_resolve_git_defaults_independently(repo):
    """Real two-agent isolation: agent A active on one allowlisted repo,
    agent B active on another -- omitted-path Git resolution for each must
    be independent, and changing A must not affect B. No global state."""
    other_authorized_parent = repo["authorized"]
    # Reuse the fixture's single allowlisted repo as A's root; carve out a
    # second allowlisted repo for B so both are real, distinct, authorized
    # roots.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp2:
        import pathlib
        second = pathlib.Path(tmp2) / "authorized-repo-b"
        second.mkdir()
        _init_repo(second)
        gr._ALLOWED_REPOS = gr._ALLOWED_REPOS | {os.path.realpath(str(second))}

        state_a = ProjectContextState(ProjectContext(name="a", root=repo["authorized"]))
        state_b = ProjectContextState(ProjectContext(name="b", root=str(second)))

        result_a = git_status(None, project_state=state_a)
        result_b = git_status(None, project_state=state_b)

        assert repo["authorized"] in result_a or os.path.realpath(repo["authorized"]) in result_a
        assert str(second) in result_b or os.path.realpath(str(second)) in result_b

        # Changing A's state does not affect B's already-computed result or
        # a fresh call against B.
        state_a.set(None)
        result_b_again = git_status(None, project_state=state_b)
        assert os.path.realpath(str(second)) in result_b_again
