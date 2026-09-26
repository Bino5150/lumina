"""DOCS-01B -- docs/tools-reference.md must list every registered production
tool, and never list one that doesn't exist.

DOCS-01A's inventory found README.md's tool coverage had drifted badly
behind the live registry (whole subsystems -- Browser Companion, Knowledge
Base, MemPalace, Toolmaker -- were never named). docs/tools-reference.md
was written to be the drift-resistant replacement: a table of every tool
name in backticks, organized by subsystem. This file is what keeps that
promise true going forward -- it reuses the same "maximal registry"
construction as tests/test_tool_tier_completeness_01.py (owner session,
both optional feature flags on, Browser Companion installed+paired,
get_weather registered the way the approved-custom-tool loader produces
it) so the comparison set is the full production tool universe, not
whatever a bare default registry happens to expose.
"""
import re

import pytest

import config
import core.secrets as secrets_module
import tools.chrome_companion as chrome_companion_tools
import tools.toolmaker as toolmaker
from chrome_companion import state as chrome_state
from chrome_companion_testkit import EXT_ID
from core import persistence
from core.agent import LuminaAgent
from tools.get_weather import register_get_weather_tool

DOCS_PATH = "docs/tools-reference.md"
_TOOL_NAME_RE = re.compile(r"`([a-z][a-z0-9_]*)`")

# Prose in the doc's own explanatory text (headers, notes) that happens to
# match the backtick-code-span pattern but never names a real tool.
_NON_TOOL_BACKTICKED_TERMS = {
    "subagents_enabled",
    "background_tasks_enabled",
}


@pytest.fixture
def maximal_registry(tmp_path, monkeypatch):
    """Same construction as test_tool_tier_completeness_01.maximal_registry
    -- see that file's docstring for why each piece is here."""
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "tool_audit.log"))
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chrome_companion_tools, "ensure_hub_started", lambda data_dir=None: object())
    chrome_state.save_install(
        str(tmp_path), extension_id=EXT_ID, socket_path=str(tmp_path / "hub.sock")
    )

    agent = LuminaAgent(owner=True, channel_id="docs-tool-catalog-sync", backend="llamacpp")
    register_get_weather_tool(agent.registry)
    return agent.registry


def _tool_names_in_docs():
    with open(DOCS_PATH, encoding="utf-8") as fh:
        text = fh.read()
    names = {m.group(1) for m in _TOOL_NAME_RE.finditer(text)}
    return names - _NON_TOOL_BACKTICKED_TERMS


def test_every_registered_tool_is_documented(maximal_registry):
    """A tool that ships without a docs/tools-reference.md row is exactly
    the kind of drift DOCS-01A found at README-scale; catch it here
    instead."""
    universe = set(maximal_registry.all_tool_names())
    documented = _tool_names_in_docs()
    missing = universe - documented
    assert not missing, (
        f"Tools registered but missing from {DOCS_PATH}: {sorted(missing)}"
    )


def test_docs_never_name_a_tool_that_no_longer_exists(maximal_registry):
    """The opposite drift: a renamed/removed tool left behind in the doc,
    which would mislead a reader into expecting a tool that isn't there."""
    universe = set(maximal_registry.all_tool_names())
    documented = _tool_names_in_docs()
    stale = documented - universe
    assert not stale, (
        f"{DOCS_PATH} names tools that are not (or no longer) registered: {sorted(stale)}"
    )
