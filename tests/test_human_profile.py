"""MB-08 Part 1: dynamic human profile block.

Bio (owner-authored) and the curated profile (Lumina-authored, resynthesized
by core/dreaming.py's idle sweep) used to be baked once into system_prompt --
at Agent construction and again on every persona swap -- with no precedence
rule against the fresh-every-turn palace block if they disagreed. This is
now reconstructed per-turn in ContextManager._build_system_prompt(), same
pattern as palace_block/projects_block, and the old bake sites are gone.
"""
import os

import pytest

import core.context as context
import core.persistence as persistence
import tools.palace as palace


@pytest.fixture(autouse=True)
def no_real_palace(monkeypatch):
    """Keep palace_block out of these tests entirely -- only human_block is
    under test here."""
    monkeypatch.setattr(palace, "build_context_block", lambda **kw: "")


def _fake_prefs(**overrides):
    base = {"human_bio": "", "human_bio_public": "", "human_profile_curated": ""}
    base.update(overrides)
    return base


def test_owner_true_with_bio_produces_human_block(monkeypatch):
    prefs = _fake_prefs(human_bio="Bino is a software engineer building Lumina.")
    monkeypatch.setattr(persistence, "load", lambda: prefs)

    ctx = context.ContextManager(owner=True)
    prompt = ctx._build_system_prompt()

    assert "## About" in prompt
    assert "Bino is a software engineer building Lumina." in prompt


def test_owner_false_with_public_bio_only(monkeypatch):
    prefs = _fake_prefs(
        human_bio="PRIVATE: Bino's home address is secret.",
        human_bio_public="Bino builds AI agents.",
    )
    monkeypatch.setattr(persistence, "load", lambda: prefs)

    ctx = context.ContextManager(owner=False)
    prompt = ctx._build_system_prompt()

    assert "Bino builds AI agents." in prompt
    assert "PRIVATE: Bino's home address is secret." not in prompt


def test_owner_true_with_no_bio_or_curated_omits_human_block(monkeypatch):
    prefs = _fake_prefs()  # bio, public bio, and curated notes all empty
    monkeypatch.setattr(persistence, "load", lambda: prefs)

    ctx = context.ContextManager(owner=True)
    prompt = ctx._build_system_prompt()

    assert "## About" not in prompt


def test_agent_py_no_longer_references_human_bio():
    """Regression guard for the two removed static bakes -- Agent.__init__
    used to append human_bio/human_bio_public onto self.ctx.system_prompt
    directly, and apply_persona() used to redo the same thing on every
    persona swap. Both are gone now that ContextManager._build_system_prompt()
    injects the (fresher) dynamic human_block instead. Pure text check, no
    live Agent instance needed."""
    agent_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "agent.py"
    )
    with open(agent_path) as f:
        src = f.read()
    assert "human_bio" not in src
