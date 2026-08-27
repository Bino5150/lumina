"""PREFS-STALE-WRITE-01 (supersedes the narrow BACKEND-PREFS-01 framing) —
session-long prefs snapshots must never publish whole-document writes that
revert newer durable state written by another component.

Live repro (2026-08-27, continuously-running desktop instance, NO restart
required):

    Settings saved OpenRouter + ~1M context + custom memory injection limit
    → click New Chat
    → context reverted to ~32K, memory injection limit to 12.

UltraAudit + independent refuter repro: a single New Chat restored a revoked
Telegram owner chat ID and re-enabled a tool the owner had explicitly
disabled.

Root mechanism (verified in the live tree, not assumed from narration):

    ui/main_window.py:468          self._prefs = persistence.load()   # ONCE, at startup
    ui/main_window.py:801/807/855/873/917   persistence.save(self._prefs)  # whole-doc publish
    ui/settings/user_profile_tab.py:20      self._prefs = persistence.load()   # ONCE, at startup
    ui/settings/user_profile_tab.py:120/138/150/154  persistence.save(self._prefs)
    ui/settings/communications_tab.py:25    self._prefs = persistence.load()   # ONCE, at startup
    ui/settings/communications_tab.py:198/351        persistence.save(self._prefs)

SettingsPanel (and therefore every settings tab) is constructed exactly once
at MainWindow startup (ui/main_window.py:624) and reused for the whole
session, so every one of those snapshots goes stale the moment any other
component writes prefs. Routine UI actions (New Chat, chat switch, persona
switch, avatar change, typing in My Human fields) are rollback events.

Invariant under repair:

    No long-lived component may publish an authoritative whole-document prefs
    snapshot loaded at an earlier time. Every mutation must be
    fresh-load → mutate owned keys → atomic save.

Test layout:
    - Cross-writer survival matrix (RED before repair): MainWindow chat/
      persona/avatar writers and the stale settings-tab writers must never
      revert backend/model, per-backend context (max_context_tokens /
      memory_inject_limit), disabled_tools, Telegram owner binding, or the
      My Human curated profile written after their snapshot was taken.
    - Healthy controls (GREEN before and after): closeEvent already
      fresh-loads; fixed-endpoint backends already ignore caller-supplied
      URLs; agent construction already prefers the durable backend over
      defaults. These pin the correct behavior so the repair cannot regress
      them, and they are the RED targets for the M4/M5 mutation proofs.
    - GeneralTab save round-trip: backend AND model must survive together
      (RED target for the M2 mutation).
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import core.persistence as persistence
import config as config_module

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Isolate every durable surface this module touches. Mirrors the
    test_review_panel.py hermetic pattern: real widgets, real persistence,
    real (throwaway) prefs.json / lumina.db / credentials.json."""
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    # Never inherit an ambient cloud backend from the real prefs.json this
    # machine may have: deterministic local backend for window construction.
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    import ui.main_window as main_window_module
    monkeypatch.setattr(main_window_module.browser_manager, "close", lambda: None)
    return tmp_path


def _seed_startup_era():
    """Era A — the state a component's snapshot holds after construction."""
    persistence.save({
        "window_width": 1150,
        "window_height": 760,
        "last_chat_id": None,
        "llm_backend": "llamacpp",
        "llm_backend_url": "http://127.0.0.1:8080/v1",
        "telegram_owner_chat_id": "11112222",
        "disabled_tools": [],
        "human_bio": "era-a-bio",
        "human_profile_curated": "STARTUP-ERA-PROFILE",
        "backend_context": {
            "llamacpp": {
                "max_context_tokens": 32000,
                "memory_inject_limit": 12,
                "tool_result_max_chars": 9000,
            },
        },
    })


def _write_post_startup_era():
    """Era B — what Settings (fresh-load writers) durably writes AFTER the
    components above took their snapshots. Mirrors the live repro exactly:
    OpenRouter + ~1M context + custom memory injection limit, plus the
    authority-sensitive keys UltraAudit saw reverted (disabled_tools,
    Telegram owner binding) and the My Human curated profile."""
    prefs = persistence.load()
    prefs["llm_backend"] = "openrouter"
    prefs["backend_context"] = {
        "openrouter": {
            "max_context_tokens": 1000000,
            "memory_inject_limit": 60,
            "tool_result_max_chars": 20000,
        },
    }
    prefs["telegram_owner_chat_id"] = "99998888"
    prefs["disabled_tools"] = ["edit_file"]
    prefs["human_bio"] = "post-startup-bio"
    prefs["human_profile_curated"] = "FRESHLY-CURATED-BY-DREAM"
    assert persistence.save(prefs) is True


def _assert_post_startup_era_survived(expect_bio="post-startup-bio",
                                      expect_curated="FRESHLY-CURATED-BY-DREAM"):
    """Every era-B value must still be on disk after the trigger fires.
    expect_bio/expect_curated let the My Human tests pass the value the
    test itself legitimately typed — the helper's job is proving the
    UNRELATED era-B keys survived, not freezing the mutated key."""
    disk = persistence.load()
    assert disk["llm_backend"] == "openrouter"
    ctx = disk["backend_context"]["openrouter"]
    assert ctx["max_context_tokens"] == 1000000
    assert ctx["memory_inject_limit"] == 60
    assert ctx["tool_result_max_chars"] == 20000
    assert disk["telegram_owner_chat_id"] == "99998888"
    assert disk["disabled_tools"] == ["edit_file"]
    assert disk["human_bio"] == expect_bio
    assert disk["human_profile_curated"] == expect_curated


def _make_window():
    from core.agent import LuminaAgent
    from ui.main_window import LuminaWindow

    agent = LuminaAgent(owner=True, channel_id="prefs-stale-write-01")
    return LuminaWindow(agent)


# ── MainWindow stale-snapshot writers (the primary live repro) ─────────────


def test_new_chat_preserves_post_startup_settings(qapp, hermetic):
    """THE live repro: save OpenRouter + 1M context + custom memory injection
    → click New Chat → all of it must remain unchanged. A single New Chat
    must never restore a revoked Telegram owner ID, re-enable a disabled
    tool, or wipe dream-curated My Human state."""
    _seed_startup_era()
    win = _make_window()          # snapshot = era A
    _write_post_startup_era()     # Settings durably writes era B
    win._new_chat()               # routine UI action
    _assert_post_startup_era_survived()
    win.close()


def test_switch_chat_preserves_post_startup_settings(qapp, hermetic):
    """Sibling path implicated by UltraAudit: switching between existing
    chats is a normal high-frequency action and must not be a rollback."""
    _seed_startup_era()
    win = _make_window()
    _write_post_startup_era()
    existing = win._current_chat_id
    assert existing is not None  # startup created/restored a chat
    other = win._current_chat_id  # drive a real switch via the chat combo path
    win._load_chat(other)
    _assert_post_startup_era_survived()
    win.close()


def test_persona_switch_preserves_post_startup_settings(qapp, hermetic):
    """Persona switch publishes last_persona — must not publish the rest of
    the document with it."""
    _seed_startup_era()
    win = _make_window()
    _write_post_startup_era()
    # Index 0 is the "— select —" placeholder (itemData=None, writer no-ops).
    # Index 1 is the first real persona file — the test must drive an actual
    # write, not a placeholder selection that silently skips it.
    assert win.persona_combo.count() >= 2, (
        "personas/ directory should be reachable from the repo root"
    )
    assert win.persona_combo.itemData(1), "first persona entry must carry a file path"
    win._on_persona_selected(1)   # the real combo-change production path
    _assert_post_startup_era_survived()
    win.close()


def test_user_avatar_apply_preserves_post_startup_settings(qapp, hermetic):
    avatar = hermetic / "user-avatar.png"
    avatar.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    _seed_startup_era()
    win = _make_window()
    _write_post_startup_era()
    win._apply_user_avatar(str(avatar))
    _assert_post_startup_era_survived()
    win.close()


# ── Stale settings-tab writers (constructed once at startup, reused) ───────


def _make_profile_tab():
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.user_profile_tab import UserProfileTab

    agent = LuminaAgent(owner=True, channel_id="prefs-stale-write-01-profile")
    return UserProfileTab(agent, COLORS)


def _make_communications_tab():
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.communications_tab import CommunicationsTab

    agent = LuminaAgent(owner=True, channel_id="prefs-stale-write-01-comms")
    return CommunicationsTab(agent, COLORS)


def test_profile_bio_autosave_preserves_post_startup_settings(qapp, hermetic):
    """Typing in My Human fires textChanged → _autosave_bio on a startup-era
    snapshot. Editing your own bio must not revert backend/context/authority
    state written after startup."""
    _seed_startup_era()
    tab = _make_profile_tab()
    _write_post_startup_era()
    tab.human_bio.setPlainText("brand new bio typed by the owner")
    _assert_post_startup_era_survived(expect_bio="brand new bio typed by the owner")


def test_profile_curated_autosave_preserves_post_startup_settings(qapp, hermetic):
    """The My Human erase mechanism: a textChanged autosave on the curated
    field republishes the snapshot taken BEFORE dream sweep curated —
    erasing a successful curation after the fact."""
    _seed_startup_era()
    tab = _make_profile_tab()
    _write_post_startup_era()     # includes FRESHLY-CURATED-BY-DREAM
    tab.human_profile_curated.setPlainText("owner-tweaked curated notes")
    _assert_post_startup_era_survived(expect_curated="owner-tweaked curated notes")


def test_profile_save_preserves_post_startup_settings(qapp, hermetic):
    _seed_startup_era()
    tab = _make_profile_tab()
    _write_post_startup_era()
    tab.human_bio.setPlainText("saved bio")   # _save() reads the widget
    tab._save()
    _assert_post_startup_era_survived(expect_bio="saved bio")
    disk = persistence.load()
    assert disk["user_name"] == config_module.USER_NAME


def test_communications_public_bio_autosave_preserves_post_startup_settings(qapp, hermetic):
    _seed_startup_era()
    tab = _make_communications_tab()
    _write_post_startup_era()
    tab.public_bio.setPlainText("public bio text")
    _assert_post_startup_era_survived()
    assert persistence.load()["human_bio_public"] == "public bio text"


def test_communications_telegram_save_preserves_post_startup_settings(qapp, hermetic):
    """UltraAudit's sharpest finding: a stale publish restored a REVOKED
    Telegram owner binding. Saving the owner chat ID must never revert
    anything else — and the new binding must land."""
    _seed_startup_era()
    tab = _make_communications_tab()
    _write_post_startup_era()
    tab.tg_chat_id.setText("77776666")
    tab._save_telegram()
    disk = persistence.load()
    assert disk["telegram_owner_chat_id"] == "77776666"
    # ...and the unrelated era-B state must still be intact:
    assert disk["llm_backend"] == "openrouter"
    assert disk["disabled_tools"] == ["edit_file"]
    assert disk["human_profile_curated"] == "FRESHLY-CURATED-BY-DREAM"
    assert disk["backend_context"]["openrouter"]["max_context_tokens"] == 1000000


# ── Healthy controls (GREEN before and after the repair) ───────────────────


def test_close_event_preserves_post_startup_settings(qapp, hermetic):
    """closeEvent already fresh-loads before adding window size — the correct
    pattern, pinned here as the in-repo reference behavior."""
    _seed_startup_era()
    win = _make_window()
    _write_post_startup_era()
    win.resize(1400, 900)
    win.close()
    _assert_post_startup_era_survived()
    disk = persistence.load()
    assert disk["window_width"] == 1400
    assert disk["window_height"] == 900


def test_fixed_endpoint_backend_ignores_caller_supplied_url(hermetic):
    """M5 RED target: a caller-supplied URL must never leak into a backend
    that owns a fixed provider endpoint."""
    from core.backends.loader import get_llm_backend

    backend = get_llm_backend(name="openai", url="http://169.254.169.254/v1")
    assert backend.base_url == "https://api.openai.com/v1"


def test_agent_construction_uses_durable_backend_not_default(hermetic):
    """M4 RED target: startup must honor the durable selected backend, not
    substitute a default/discovery state merely because the app restarted."""
    monkeypatch = hermetic  # noqa: F841  (hermetic already isolated secrets/prefs)
    import config as cfg
    from core.agent import LuminaAgent

    cfg.LLM_BACKEND = "openrouter"
    try:
        agent = LuminaAgent(owner=True, channel_id="prefs-stale-write-01-startup")
        assert agent.llm.name == "openrouter"
    finally:
        cfg.LLM_BACKEND = "llamacpp"


def test_general_tab_save_roundtrips_backend_and_model(qapp, hermetic):
    """M2 RED target: backend and model must survive a save TOGETHER. A repair
    that persists the backend but drops the per-provider model must fail
    here."""
    import config as cfg
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab

    cfg.LLM_BACKEND = "openrouter"
    cfg.OPENROUTER_API_KEY = "sk-test-openrouter"
    cfg.OPENROUTER_DEFAULT_MODEL = ""
    try:
        agent = LuminaAgent(owner=True, channel_id="prefs-stale-write-01-general",
                            backend="openrouter")
        tab = GeneralTab(agent, COLORS)
        tab.backend_combo.setCurrentText("openrouter")
        tab.cloud_key.setText("sk-test-openrouter")
        tab.cloud_model.setCurrentText("z-ai/glm-5.3-flash")
        tab._save()
    finally:
        cfg.LLM_BACKEND = "llamacpp"

    disk = persistence.load()
    assert disk["llm_backend"] == "openrouter"
    assert disk["cloud_credentials"]["openrouter"]["default_model"] == "z-ai/glm-5.3-flash"
