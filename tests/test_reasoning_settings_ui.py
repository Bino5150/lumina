"""
Patch 3A.4 Part 5 -- Settings UI for provider-aware reasoning control
(ui/settings/general_tab.py's Reasoning Effort row).

Real Qt (offscreen platform), real GeneralTab, real LuminaAgent, real
persistence.save()/load() against an isolated tmp prefs.json -- same
"fake only the seam under test" approach tests/test_settings_general_
feedback.py already established. Only requests.get (for OpenRouter's
discovery HTTP) and, where a test needs to force a specific failure,
persistence.save/core.backends.loader.get_llm_backend get faked/spied.

Sections:
  A. OpenAI Sol/Luna -- full dropdown contents + independent pending recall
  B. Anthropic Sonnet 5 (has XHigh) vs Sonnet 4.6 (no XHigh) + stale value
  C. Qwen hybrid -- Provider Default/Disabled/Enabled only
  D. Groq literal "default" vs Provider Default -- label/data distinctness
  E. OmniRoute / LM Studio(local) / unknown-static-model -- all disabled
  F. OpenRouter dynamic lifecycle (undiscovered/refresh/cache/guard rails)
  G. Pending draft-state overlay + _save() integration
  H. Fresh Settings reopen / real runtime handoff
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

import requests
from PySide6.QtWidgets import QApplication

import config
import core.secrets as secrets_module
from core import persistence
from core import reasoning_preferences
from core.reasoning_preferences import resolve_reasoning_effort
from core.backends.reasoning import ReasoningCapabilities
from ui.settings._widgets import ButtonFeedback


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))


def _make_tab(monkeypatch, backend="llamacpp"):
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab

    monkeypatch.setattr(config, "LLM_BACKEND", backend, raising=False)
    agent = LuminaAgent(owner=True, channel_id="reasoning-settings-ui-test", backend=backend)
    return GeneralTab(agent, COLORS)


@pytest.fixture
def tab(qapp, isolated_prefs, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    return _make_tab(monkeypatch)


def _items(combo):
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def _pick(combo, raw_value):
    """Simulate a genuine user pick via the combo's real `activated` signal
    -- not currentIndexChanged/currentTextChanged, which do not fire when
    the effective value doesn't change (see GeneralTab._on_reasoning_
    activated()'s docstring)."""
    idx = combo.findData(raw_value)
    assert idx >= 0, f"{raw_value!r} not present as item data"
    combo.activated.emit(idx)


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return {"data": self._data}


def _fake_get(data, calls=None):
    def _get(*a, **k):
        if calls is not None:
            calls.append(1)
        return _FakeResp(data)
    return _get


# ── A. OpenAI Sol/Luna ───────────────────────────────────────────────────

def test_openai_full_dropdown_contents_and_labels(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")

    assert tab.reasoning_combo.isEnabled()
    assert _items(tab.reasoning_combo) == [
        (None, "Provider Default"),
        ("none", "None"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "XHigh"),
        ("max", "Max"),
    ]


def test_openai_sol_luna_independent_pending_recall_both_directions(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")

    tab.cloud_model.setCurrentText("gpt-5.6-luna")
    assert tab.reasoning_combo.currentData() is None, "Luna must not inherit Sol's pending choice"
    _pick(tab.reasoning_combo, "high")

    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    assert tab.reasoning_combo.currentData() == "max"
    tab.cloud_model.setCurrentText("gpt-5.6-luna")
    assert tab.reasoning_combo.currentData() == "high"

    assert tab._pending_reasoning == {
        ("openai", "gpt-5.6-sol"): "max",
        ("openai", "gpt-5.6-luna"): "high",
    }


# ── B. Anthropic Sonnet 5 vs Sonnet 4.6 ──────────────────────────────────

def test_anthropic_sonnet5_has_xhigh_sonnet46_does_not(tab, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-5", raising=False)
    tab.backend_combo.setCurrentText("anthropic")
    tab.cloud_model.setCurrentText("claude-sonnet-5")
    sonnet5_data = [d for d, _ in _items(tab.reasoning_combo)]
    assert "xhigh" in sonnet5_data
    assert sonnet5_data == [None, "low", "medium", "high", "xhigh", "max"]

    tab.cloud_model.setCurrentText("claude-sonnet-4-6")
    sonnet46_data = [d for d, _ in _items(tab.reasoning_combo)]
    assert "xhigh" not in sonnet46_data
    assert sonnet46_data == [None, "low", "medium", "high", "max"]


def test_stale_sonnet46_xhigh_displays_provider_default_without_mutating_stored_value(tab, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-4-6", raising=False)
    prefs = {"backend_reasoning": {"anthropic": {"claude-sonnet-4-6": "xhigh"}}}
    monkeypatch.setattr(persistence, "load", lambda: prefs)

    tab.backend_combo.setCurrentText("anthropic")
    tab.cloud_model.setCurrentText("claude-sonnet-4-6")

    # Real efforts exist for this model (low/medium/high/max) -- dropdown
    # stays ENABLED, just shows Provider Default because "xhigh" fails
    # validation under Sonnet 4.6's actual capability data.
    assert tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() is None

    # Merely opening/switching to this stale state must NOT queue a
    # pending None -- the real stale "xhigh" must survive untouched.
    assert tab._pending_reasoning == {}
    assert prefs["backend_reasoning"]["anthropic"]["claude-sonnet-4-6"] == "xhigh"

    # Switching away and back must not have mutated anything either.
    tab.cloud_model.setCurrentText("claude-sonnet-5")
    tab.cloud_model.setCurrentText("claude-sonnet-4-6")
    assert tab._pending_reasoning == {}
    assert prefs["backend_reasoning"]["anthropic"]["claude-sonnet-4-6"] == "xhigh"


def test_explicit_user_reset_to_provider_default_records_none(tab, monkeypatch):
    """A user must still be able to deliberately reset a stale value to
    Provider Default even though Provider Default is already what's
    displayed -- this requires `activated` (fires even when the visible
    item doesn't change), never currentIndexChanged/currentTextChanged."""
    monkeypatch.setattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-4-6", raising=False)
    prefs = {"backend_reasoning": {"anthropic": {"claude-sonnet-4-6": "xhigh"}}}
    monkeypatch.setattr(persistence, "load", lambda: prefs)

    tab.backend_combo.setCurrentText("anthropic")
    tab.cloud_model.setCurrentText("claude-sonnet-4-6")
    assert tab.reasoning_combo.currentData() is None  # already showing Provider Default

    index_changed_fired = []
    tab.reasoning_combo.currentIndexChanged.connect(lambda i: index_changed_fired.append(i))

    _pick(tab.reasoning_combo, None)  # re-picking the already-current item

    assert tab._pending_reasoning == {("anthropic", "claude-sonnet-4-6"): None}
    # currentIndexChanged must NOT have fired -- the visible item never
    # changed, proving `activated` (not that signal) is what recorded this.
    assert index_changed_fired == []


# ── C. Qwen hybrid ────────────────────────────────────────────────────────

def test_qwen_hybrid_shows_only_provider_default_disabled_enabled(tab, monkeypatch):
    monkeypatch.setattr(config, "QWEN_DEFAULT_MODEL", "qwen3.5-plus", raising=False)
    tab.backend_combo.setCurrentText("qwen")
    tab.cloud_model.setCurrentText("qwen3.5-plus")

    assert tab.reasoning_combo.isEnabled()
    assert _items(tab.reasoning_combo) == [
        (None, "Provider Default"),
        ("disabled", "Disabled"),
        ("enabled", "Enabled"),
    ]


# ── D. Groq literal "default" vs Provider Default ────────────────────────

def test_groq_literal_default_distinct_label_same_raw_data(tab, monkeypatch):
    monkeypatch.setattr(config, "GROQ_DEFAULT_MODEL", "qwen/qwen3.6-27b", raising=False)
    tab.backend_combo.setCurrentText("groq")
    tab.cloud_model.setCurrentText("qwen/qwen3.6-27b")

    items = _items(tab.reasoning_combo)
    assert items == [
        (None, "Provider Default"),
        ("none", "None"),
        ("default", "Default (Provider Value)"),
    ]
    labels = [label for _, label in items]
    assert labels[0] != labels[2], "Provider Default and Groq's literal 'default' must render distinctly"
    # Raw item data for the literal-"default" entry is exactly the string
    # "default" -- never conflated with the None Provider-Default sentinel.
    label_by_data = dict(items)
    assert label_by_data["default"] == "Default (Provider Value)"


# ── E. OmniRoute / LM Studio(local) / unknown-static-model -- disabled ───

def test_omniroute_disabled_at_provider_default(tab, monkeypatch):
    monkeypatch.setattr(config, "OMNIROUTE_DEFAULT_MODEL", "kr/glm-5", raising=False)
    tab.backend_combo.setCurrentText("omniroute")
    assert not tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() is None
    assert "reasoning control" in tab.reasoning_combo.toolTip()


def test_lmstudio_local_disabled_at_provider_default_no_model_field(tab):
    tab.backend_combo.setCurrentText("lmstudio")
    assert not tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() is None


def test_unknown_static_openai_model_disabled_at_provider_default(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-4o")
    assert not tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() is None


# ── F. OpenRouter dynamic lifecycle ───────────────────────────────────────

def test_openrouter_undiscovered_not_conflated_with_unsupported(tab):
    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("some/model")
    assert not tab.reasoning_combo.isEnabled()
    tooltip = tab.reasoning_combo.toolTip()
    assert "hasn't been loaded yet" in tooltip
    assert "does not expose" not in tooltip
    assert "does not support" not in tooltip


def test_openrouter_provider_default_skips_refresh_entirely_zero_http(tab, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", _fake_get([], calls))

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("some/model")  # no saved/pending value -> Provider Default

    assert calls == [], "no saved/pending value means nothing to validate -- must not touch the network"
    assert not tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() is None


def test_openrouter_explicit_value_triggers_one_guarded_refresh_and_restores_choice(tab, monkeypatch):
    calls = []
    data = [{"id": "some/model", "reasoning": {"supported_efforts": ["low", "high"], "default_effort": "low"}}]
    monkeypatch.setattr(requests, "get", _fake_get(data, calls))

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("some/model")
    tab._pending_reasoning[("openrouter", "some/model")] = "high"
    tab._refresh_reasoning_row()

    assert len(calls) == 1, "exactly one guarded refresh_reasoning_capabilities() HTTP call expected"
    assert tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() == "high"


def test_openrouter_failed_refresh_preserves_saved_value_untouched(tab, monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError("network down")
    monkeypatch.setattr(requests, "get", _boom)

    prefs = {"backend_reasoning": {"openrouter": {"some/model": "high"}}}
    monkeypatch.setattr(persistence, "load", lambda: prefs)

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("some/model")

    assert not tab.reasoning_combo.isEnabled()
    assert tab.reasoning_combo.currentData() is None
    tooltip = tab.reasoning_combo.toolTip()
    assert "could not be loaded" in tooltip
    # The underlying saved value must remain completely untouched.
    assert prefs["backend_reasoning"]["openrouter"]["some/model"] == "high"
    assert tab._pending_reasoning == {}


def test_openrouter_refresh_models_button_retains_usable_cache(tab, monkeypatch):
    calls = []
    data = [{"id": "cached/model", "reasoning": {"supported_efforts": ["low", "high"]}}]
    monkeypatch.setattr(requests, "get", _fake_get(data, calls))

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("cached/model")
    tab._refresh_models()  # simulates clicking the "Refresh Models" button

    assert len(calls) == 1
    probe = tab._reasoning_probes.get("openrouter")
    assert probe is not None
    assert probe.reasoning_capabilities_ready("cached/model") is True
    assert tab.reasoning_combo.isEnabled()
    assert set(d for d, _ in _items(tab.reasoning_combo)) == {None, "low", "high"}


def test_openrouter_switching_between_two_discovered_models_no_second_http_call(tab, monkeypatch):
    calls = []
    data = [
        {"id": "model/a", "reasoning": {"supported_efforts": ["low", "high"]}},
        {"id": "model/b", "reasoning": {"supported_efforts": ["medium"]}},
    ]
    monkeypatch.setattr(requests, "get", _fake_get(data, calls))

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("model/a")
    tab._refresh_models()
    assert len(calls) == 1

    tab.cloud_model.setCurrentText("model/b")
    tab.cloud_model.setCurrentText("model/a")
    tab.cloud_model.setCurrentText("model/b")

    assert len(calls) == 1, "both models came from the same discovered catalog -- no second HTTP call"
    assert tab.reasoning_combo.isEnabled()
    assert set(d for d, _ in _items(tab.reasoning_combo)) == {None, "medium"}


def test_openrouter_discovered_unsupported_distinguishable_from_undiscovered(tab, monkeypatch):
    calls = []
    data = [{"id": "supported/model", "reasoning": {"supported_efforts": ["low"]}}]
    monkeypatch.setattr(requests, "get", _fake_get(data, calls))

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_model.setCurrentText("supported/model")
    tab._refresh_models()
    assert len(calls) == 1

    # Now switch to a DIFFERENT model absent from that same discovery
    # response -- genuinely known-and-unsupported, not unknown.
    tab.cloud_model.setCurrentText("unsupported/model")
    assert len(calls) == 1, "already discovered -- must not re-fetch just because this model has no data"
    assert not tab.reasoning_combo.isEnabled()
    unsupported_tooltip = tab.reasoning_combo.toolTip()
    assert "does not expose a selectable reasoning control" in unsupported_tooltip
    assert "hasn't been loaded yet" not in unsupported_tooltip


# ── G. Pending draft-state overlay + _save() integration ─────────────────

def test_pending_overlay_multiple_edits_across_providers_coexist_before_save(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-5", raising=False)

    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")
    tab.cloud_model.setCurrentText("gpt-5.6-luna")
    _pick(tab.reasoning_combo, "high")

    tab.backend_combo.setCurrentText("anthropic")
    tab.cloud_model.setCurrentText("claude-sonnet-5")
    _pick(tab.reasoning_combo, "xhigh")

    assert tab._pending_reasoning == {
        ("openai", "gpt-5.6-sol"): "max",
        ("openai", "gpt-5.6-luna"): "high",
        ("anthropic", "claude-sonnet-5"): "xhigh",
    }


def test_single_save_persists_all_pending_entries_with_correct_json_shapes(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-5", raising=False)

    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")
    tab.cloud_model.setCurrentText("gpt-5.6-luna")
    _pick(tab.reasoning_combo, "high")

    tab.backend_combo.setCurrentText("anthropic")
    tab.cloud_model.setCurrentText("claude-sonnet-5")
    _pick(tab.reasoning_combo, "xhigh")
    _pick(tab.reasoning_combo, None)  # explicit reset to Provider Default (null)

    tab._save()

    assert tab.save_btn.text() == "✓ Saved"
    saved = persistence.load()
    assert saved["backend_reasoning"] == {
        "openai": {"gpt-5.6-sol": "max", "gpt-5.6-luna": "high"},
        "anthropic": {"claude-sonnet-5": None},
    }


def test_prefs_save_failure_leaves_pending_intact(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")

    monkeypatch.setattr(persistence, "save", lambda prefs: False)
    tab._save()

    assert tab.save_btn.text() == "⚠ Not Saved"
    assert tab._pending_reasoning == {("openai", "gpt-5.6-sol"): "max"}, \
        "a failed disk write must not discard the not-yet-durable pending edit"


def test_successful_save_clears_pending(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")

    tab._save()

    assert tab.save_btn.text() == "✓ Saved"
    assert tab._pending_reasoning == {}


def test_post_save_live_backend_apply_failure_still_durably_writes_reasoning(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")

    from core.backends import loader as loader_module
    monkeypatch.setattr(
        loader_module, "get_llm_backend",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backend on fire")),
    )

    tab._save()

    assert tab.save_btn.text() == "⚠ Partial"
    assert "Settings saved" in tab.status_lbl.text()
    saved = persistence.load()
    assert saved["backend_reasoning"]["openai"]["gpt-5.6-sol"] == "max"
    # Durable once save_prefs() succeeded -- the pending overlay is cleared
    # even though the later live-backend-reconstruction step failed.
    assert tab._pending_reasoning == {}


# ── H. Fresh Settings reopen / real runtime handoff ──────────────────────

def test_fresh_settings_reopen_recalls_saved_value_and_resolves_at_runtime(tab, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-sol", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-5", raising=False)
    tab.cloud_key.setText("sk-fake-openai-key")

    tab.backend_combo.setCurrentText("openai")
    tab.cloud_model.setCurrentText("gpt-5.6-sol")
    _pick(tab.reasoning_combo, "max")

    tab.backend_combo.setCurrentText("anthropic")
    tab.cloud_model.setCurrentText("claude-sonnet-5")
    _pick(tab.reasoning_combo, "xhigh")

    # Save while "anthropic" is the current backend selection -- the
    # pending OpenAI entry must still be included in this one save.
    tab._save()
    assert tab.save_btn.text() == "✓ Saved"

    # A genuinely fresh instance -- new GeneralTab, new LuminaAgent -- not
    # the same object under test above.
    fresh_tab = _make_tab(monkeypatch, backend="openai")

    assert fresh_tab.backend_combo.currentText() == "openai"
    assert fresh_tab.cloud_model.currentText() == "gpt-5.6-sol"
    assert fresh_tab.reasoning_combo.isEnabled()
    assert fresh_tab.reasoning_combo.currentData() == "max"

    # Real runtime handoff -- not just UI-internal state.
    assert resolve_reasoning_effort(fresh_tab.agent.llm, model="gpt-5.6-sol") == "max"
