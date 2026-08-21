"""core/git_repos.py -- CODING-02B-B1.

Shared Git-repository authorization boundary for the four read-only Git
tools. Pure resolve_git_repo() unit coverage -- no subprocess/git invoked
here, that's tests/test_git_tools.py's job. _ALLOWED_REPOS is monkeypatched
to a small, hermetic set for every test so none of this depends on
~/lumina or ~/lumina-release actually existing on the machine running the
suite, and never touches the real repos.
"""
import os

import pytest

import core.git_repos as gr
from core.project_context import ProjectContext, ProjectContextState


@pytest.fixture(autouse=True)
def _hermetic_allowlist(tmp_path, monkeypatch):
    authorized = tmp_path / "authorized-repo"
    authorized.mkdir()
    unauthorized = tmp_path / "unauthorized-repo"
    unauthorized.mkdir()
    monkeypatch.setattr(gr, "_ALLOWED_REPOS", {
        os.path.realpath(str(authorized)),
    })
    return {"authorized": str(authorized), "unauthorized": str(unauthorized)}


def test_explicit_allowlisted_path_succeeds(_hermetic_allowlist):
    real, err = gr.resolve_git_repo(_hermetic_allowlist["authorized"], None)
    assert err is None
    assert real == os.path.realpath(_hermetic_allowlist["authorized"])


def test_explicit_unauthorized_path_fails(_hermetic_allowlist):
    real, err = gr.resolve_git_repo(_hermetic_allowlist["unauthorized"], None)
    assert err is not None
    assert "allowlist" in err.lower()


def test_omitted_plus_allowlisted_active_root_succeeds(_hermetic_allowlist):
    state = ProjectContextState(ProjectContext(name="p", root=_hermetic_allowlist["authorized"]))
    real, err = gr.resolve_git_repo(None, state)
    assert err is None
    assert real == os.path.realpath(_hermetic_allowlist["authorized"])


def test_omitted_plus_unauthorized_active_root_fails(_hermetic_allowlist):
    """The core authorization-boundary proof: a fully valid active Project
    (name + real, existing root) must NOT become Git-authorized merely by
    being active. Mirrors the Oracle example from CODING-02B-B0's design."""
    state = ProjectContextState(ProjectContext(name="oracle", root=_hermetic_allowlist["unauthorized"]))
    real, err = gr.resolve_git_repo(None, state)
    assert err is not None
    assert "allowlist" in err.lower()


def test_omitted_plus_no_active_project_fails_clearly(_hermetic_allowlist):
    real, err = gr.resolve_git_repo(None, None)
    assert real is None
    assert err is not None
    assert "no active project" in err.lower()

    # Same result when a real ProjectContextState is given but empty.
    real2, err2 = gr.resolve_git_repo(None, ProjectContextState())
    assert real2 is None
    assert err2 is not None


def test_project_binding_does_not_enlarge_allowed_set(_hermetic_allowlist):
    """An active Project (even an authorized one) must never widen
    _ALLOWED_REPOS for anyone else's explicit-path call -- the set itself is
    static, independent of any ProjectContextState's contents."""
    before = set(gr._ALLOWED_REPOS)
    state = ProjectContextState(ProjectContext(name="p", root=_hermetic_allowlist["authorized"]))
    gr.resolve_git_repo(None, state)
    assert gr._ALLOWED_REPOS == before

    # The unauthorized root is still unauthorized for an explicit call too.
    real, err = gr.resolve_git_repo(_hermetic_allowlist["unauthorized"], state)
    assert err is not None


def test_explicit_path_wins_over_active_project(_hermetic_allowlist):
    """Explicit caller intent always wins -- the active Project is never
    consulted, reinterpreted against, or blended with an explicit repo_path,
    even when doing so would have flipped the outcome either direction."""
    # Active project points at the unauthorized root; explicit path is the
    # authorized one. Explicit path succeeds -- active project is irrelevant.
    state = ProjectContextState(ProjectContext(name="p", root=_hermetic_allowlist["unauthorized"]))
    real, err = gr.resolve_git_repo(_hermetic_allowlist["authorized"], state)
    assert err is None
    assert real == os.path.realpath(_hermetic_allowlist["authorized"])

    # Active project points at the authorized root; explicit path is the
    # unauthorized one. Explicit path fails -- active project's own
    # authorized-ness does not rescue it.
    state2 = ProjectContextState(ProjectContext(name="p", root=_hermetic_allowlist["authorized"]))
    real2, err2 = gr.resolve_git_repo(_hermetic_allowlist["unauthorized"], state2)
    assert err2 is not None


def test_canonicalization_matches_expanduser_then_realpath(_hermetic_allowlist, tmp_path):
    """Preserves the exact canonicalization the four Git tools always used:
    expanduser -> realpath. A symlink to the authorized root must resolve to
    the same real path and be treated as the same repo."""
    link = tmp_path / "link-to-authorized"
    os.symlink(_hermetic_allowlist["authorized"], link)

    real, err = gr.resolve_git_repo(str(link), None)
    assert err is None
    assert real == os.path.realpath(_hermetic_allowlist["authorized"])

    # A trailing slash / redundant segment must canonicalize identically.
    noisy = _hermetic_allowlist["authorized"] + "/./"
    real2, err2 = gr.resolve_git_repo(noisy, None)
    assert err2 is None
    assert real2 == real
