"""CODING-01B-B integration tests -- proves the real wiring, not just the
kernel in isolation (already proven in tests/test_file_edit.py).

Specifically: that core/agent.py's real register_file_edit_tools(self.registry)
call actually took effect; that the non-owner PIN gate treats edit_file like
the project's other write_local tools instead of falling back to the
execute-tier fail-closed default a missing TOOL_TIERS entry would produce;
and that the universal emergency-stop blast door still covers it with zero
special-cased plumbing.

Real LuminaAgent construction + isolated prefs/secrets, following the exact
convention test_settings_general_feedback.py's _make_tab() already
established -- not a lighter-weight reimplementation of the constructor.
"""
import pytest

import core.secrets as secrets_module
from core import emergency_stop, persistence
from core.agent import LuminaAgent
from core.tool_profiles import apply_tool_profile
from tools.file_edit import register_file_edit_tools
from tools.registry import ToolRegistry


@pytest.fixture
def isolated_agent_data(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))


# ── Agent-registration integration ─────────────────────────────────────

def test_owner_agent_registry_contains_edit_file(isolated_agent_data):
    agent = LuminaAgent(owner=True, channel_id="coding-01b-b-owner-test", backend="llamacpp")
    assert "edit_file" in agent.registry.list_tools()


def test_nonowner_agent_registry_contains_edit_file_structurally(isolated_agent_data):
    """edit_file must be structurally present for non-owner sessions too
    (unlike toolmaker's tools, which core/agent.py only registers when
    owner=True) -- it's default-DISABLED for non-owner, never absent."""
    agent = LuminaAgent(owner=False, channel_id="coding-01b-b-guest-test", backend="llamacpp")
    assert "edit_file" in agent.registry.list_tools()


# ── Non-owner default-deny / explicit-grant ────────────────────────────

def test_nonowner_without_grant_edit_file_disabled(isolated_agent_data):
    agent = LuminaAgent(owner=False, channel_id="coding-01b-b-deny-test", backend="llamacpp")
    assert "edit_file" not in agent.registry.list_enabled()
    result = agent.registry.call("edit_file", {"path": "/tmp/does-not-matter", "edits": []})
    assert "disabled" in result.lower()


def test_nonowner_with_coding_profile_grant_not_pin_blocked(isolated_agent_data, tmp_path):
    """Once a non-owner session is explicitly granted the Coding profile
    through the real apply_tool_profile() path (the one headless.py/main.py
    actually use), edit_file must behave like the project's other
    write_local tools -- reachable without PIN verification -- rather than
    the execute-tier fail-closed default an unclassified tool would get."""
    agent = LuminaAgent(owner=False, channel_id="coding-01b-b-grant-test", backend="llamacpp")
    apply_tool_profile(agent.registry, profile_name="Coding", owner=False)

    assert "edit_file" in agent.registry.list_enabled()

    target = tmp_path / "f.txt"
    target.write_text("hello world\n")
    result = agent.registry.call("edit_file", {
        "path": str(target),
        "edits": [{"old_text": "world", "new_text": "there"}],
    })

    assert "PIN verification required" not in result
    assert result.startswith("[Edited:")
    assert target.read_text() == "hello there\n"


def test_nonowner_default_deny_unaffected_by_grant_to_other_session(isolated_agent_data):
    """Cross-contamination guard, mirroring test_owner_isolation.py's own
    check: granting one guest session the Coding profile must not leak
    edit_file access into a separate, ungranted guest session."""
    granted = LuminaAgent(owner=False, channel_id="coding-01b-b-grant-a", backend="llamacpp")
    apply_tool_profile(granted.registry, profile_name="Coding", owner=False)
    assert "edit_file" in granted.registry.list_enabled()

    ungranted = LuminaAgent(owner=False, channel_id="coding-01b-b-grant-b", backend="llamacpp")
    assert "edit_file" not in ungranted.registry.list_enabled()


# ── Emergency-stop interaction ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_emergency_state():
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


def test_edit_file_blocked_while_emergency_stop_latched(tmp_path):
    """One cheap regression on top of the existing generic blast-door proof
    in test_emergency_stop.py -- confirms edit_file specifically dispatches
    through the same universal ToolRegistry.call() lease, with no special
    E-stop plumbing needed or added."""
    registry = ToolRegistry()
    register_file_edit_tools(registry)

    target = tmp_path / "f.txt"
    target.write_text("hello world\n")

    emergency_stop.latch(source="test", reason="block-edit-file")

    result = registry.call("edit_file", {
        "path": str(target),
        "edits": [{"old_text": "world", "new_text": "there"}],
    })

    assert "blocked" in result.lower()
    assert target.read_text() == "hello world\n"  # never touched

    emergency_stop.rearm_local()
    result2 = registry.call("edit_file", {
        "path": str(target),
        "edits": [{"old_text": "world", "new_text": "there"}],
    })
    assert result2.startswith("[Edited:")
    assert target.read_text() == "hello there\n"


# ── Static schema verification (before any live model gets access) ─────
#
# LM Studio/Ollama pass registry.get_schemas() through unchanged (no
# _translate_tools() equivalent exists for them -- payload["tools"] = tools
# directly), so there's no transformation logic to verify there. Anthropic
# and Gemini each rewrap the schema; both are checked directly against
# their real _translate_tools() static methods below, no new backend
# abstraction needed for it.

def _real_schema():
    registry = ToolRegistry()
    register_file_edit_tools(registry)
    return registry.get_schemas()


def test_edit_file_present_exactly_once_in_real_schema():
    schemas = _real_schema()
    names = [s["function"]["name"] for s in schemas]
    assert names.count("edit_file") == 1


def test_real_schema_nested_shape_matches_contract():
    schemas = _real_schema()
    params = next(s["function"]["parameters"] for s in schemas if s["function"]["name"] == "edit_file")
    edits_schema = params["properties"]["edits"]
    assert edits_schema["type"] == "array"
    assert edits_schema["items"]["type"] == "object"
    assert set(edits_schema["items"]["required"]) == {"old_text", "new_text"}
    assert "expected_sha256" not in params["required"]


def test_anthropic_translator_preserves_nested_edits_shape():
    from core.backends.anthropic_backend import AnthropicBackend

    translated = AnthropicBackend._translate_tools(_real_schema())
    tool = next(t for t in translated if t["name"] == "edit_file")
    edits_schema = tool["input_schema"]["properties"]["edits"]
    assert edits_schema["type"] == "array"
    assert edits_schema["items"]["type"] == "object"
    assert set(edits_schema["items"]["required"]) == {"old_text", "new_text"}


def test_gemini_translator_preserves_nested_edits_shape():
    from core.backends.gemini_backend import GeminiBackend

    translated = GeminiBackend._translate_tools(_real_schema())
    declarations = translated[0]["functionDeclarations"]
    tool = next(t for t in declarations if t["name"] == "edit_file")
    edits_schema = tool["parameters"]["properties"]["edits"]
    assert edits_schema["type"] == "array"
    assert edits_schema["items"]["type"] == "object"
    assert set(edits_schema["items"]["required"]) == {"old_text", "new_text"}
