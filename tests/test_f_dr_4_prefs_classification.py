"""F-DR-4: a normal Lumina installation can make a Verified Agent Backup.

Found by the DR-A2 drill on gremlin-pi (2026-11-24): the Verified export
refused every installation that had launched once, because the product's own
first-launch defaults (avatar_path, user_avatar_path, tts_host) and its
Settings saves wrote preference keys the export did not recognize. Owner
direction (2026-11-24): preserve every ordinary preference, keep credentials,
PINs and owner authority out, never let a backed-up setting activate anything,
and never refuse a backup only because a preference is unfamiliar.

Everything here is synthetic: tmp_path data roots, the real exporter, the
real restore engine and the real C1 store.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import core.agent_backup as ab
import core.agent_backup_export as ex
import core.agent_backup_restore as ar
import config
from core import persistence
from core import suspension as S
from core import suspension_runtime as R
from core import wire_seat_gate as G

from test_agent_backup_verified_export_01 import source, read_zip  # noqa: F401
from test_c1_verified_backup_s3a import _fresh_cache, verified, vsource  # noqa: F401
from test_c1_restore_intake_s3b import restore, target  # noqa: F401
from test_c1s2_recover_from_archive import _pin_session, _use  # noqa: F401

REPO = Path(__file__).resolve().parent.parent

KNOWN = (ex._TEXT_PREFS | ex._BOOL_PREFS | ex._NUMBER_PREFS | ex._ENDPOINT_PREFS
         | set(ex._STRUCTURED_PREFS) | ex._DROP_PREFS | ex._DESTINATION_LOCAL_PREFS)
DRILL_KEYS = {"avatar_path", "user_avatar_path", "tts_host", "voicebox_host", "voicebox_profile",
              "multimodal_routes", "multimodal_disabled_providers"}


# ── the consistency check: every preference the product uses is classified ──

def _tracked_python():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / f for f in out.stdout.split() if not f.startswith("tests/")]


_KEY_PATTERNS = (
    re.compile(r'\b_p\.get\(\s*"([A-Za-z0-9_]+)"'),
    re.compile(r'\b_?prefs\.get\(\s*"([A-Za-z0-9_]+)"'),
    re.compile(r'\b_?prefs\[\s*"([A-Za-z0-9_]+)"\s*\]'),
    re.compile(r'persistence\.update\(\s*\{\s*"([A-Za-z0-9_]+)"'),
)


def _product_preference_keys():
    keys = set(persistence._defaults)
    for path in _tracked_python():
        text = path.read_text(encoding="utf-8")
        for pattern in _KEY_PATTERNS:
            keys.update(pattern.findall(text))
    return keys


def test_every_preference_key_the_product_reads_or_writes_is_classified():
    keys = _product_preference_keys()
    assert len(keys) >= 70 and DRILL_KEYS <= keys  # the scan itself works
    secret_named = {k for k in keys - KNOWN if any(p in k.lower() for p in ex._SECRET_NAME_PARTS)}
    unclassified = sorted(keys - KNOWN - secret_named)
    assert unclassified == [], (
        "these preference keys are not classified in core/agent_backup_export.py; "
        f"add each to the right set: {unclassified}")
    # What the name rule catches must really be credentials, not settings.
    assert secret_named <= {"custom_api_key", "elevenlabs_api_key", "omniroute_api_key",
                            "openai_api_key", "anthropic_api_key", "openrouter_api_key"}, secret_named


def test_first_launch_defaults_are_all_kept():
    assert set(persistence._defaults) <= ex._TEXT_PREFS | ex._BOOL_PREFS | ex._NUMBER_PREFS | ex._ENDPOINT_PREFS


def test_numeric_limits_named_tokens_are_settings_not_secrets():
    limits = {"tool_budget_tokens": 13500, "response_reserve_tokens": 4096,
              "context_compaction_batch_tokens": 1400, "dream_min_tokens": 900}
    data, counts = ex._scrub_preferences_counts(json.dumps(limits).encode())
    assert {k: v for k, v in json.loads(data).items() if k in limits} == limits
    assert counts == {"omitted": 0, "unclassified": 0, "destination_local": 0}


# ── hostile and boundary cases ──────────────────────────────────────────────

def _scrub(prefs):
    data, counts = ex._scrub_preferences_counts(json.dumps(prefs).encode())
    return json.loads(data), counts


@pytest.mark.parametrize("key,value,kept,unclassified", [
    ("tts_host", "http://localhost:8880", True, 0),
    ("llm_backend_url", "https://user:pw@example.test/v1", False, 0),
    ("voicebox_host", "http://speech.test/?api_key=abc", False, 0),
    ("chatterbox_host", "http://speech.test/?access_token=abc", False, 0),
    ("backend_endpoints", {"llamacpp": "http://127.0.0.1:8080/v1"}, True, 0),
    ("backend_endpoints", {"custom": "https://u:p@host.test/v1"}, False, 0),
    ("backend_endpoints", {"custom": 8080}, False, 1),
    ("cloud_credentials", {"openrouter": {"default_model": "z-ai/glm"}}, True, 0),
    ("cloud_credentials", {"openrouter": {"api_key": "sk-x", "default_model": "m"}}, False, 0),
    ("backend_context", {"openrouter": {"max_context_tokens": 1000000, "memory_inject_limit": 60}}, True, 0),
    ("backend_models", {"vision": {"session_cookie": "x"}}, False, 0),
    ("multimodal_routes", {"vision_understanding": {"mode": "auto", "fallbacks": ["gemini"]}}, True, 0),
    ("multimodal_routes", ["not", "a", "mapping"], False, 1),
    ("multimodal_disabled_providers", ["qwen"], True, 0),
    ("multimodal_disabled_providers", {"qwen": True}, False, 1),
    ("backend_reasoning", {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}, False, 1),
    ("system_prompt", "x" * (ex._PREF_VALUE_MAX_BYTES + 1), False, 1),
    ("system_prompt", "Be brief. ✓", True, 0),
    ("openai_api_key", "sk-x", False, 0),
    ("some_bot_token", "x", False, 0),
    ("mystery_pref_from_the_future", {"anything": 1}, False, 1),
    # review R0 L1: credential names in other spellings, and credential-shaped values
    ("multimodal_routes", {"vision_understanding": {"mode": "auto", "accessToken": "x"}}, False, 0),
    ("backend_provider_routing", {"headers": {"x-api-key": "x"}}, False, 0),
    ("cloud_credentials", {"custom": {"pass": "x"}}, False, 0),
    ("cloud_credentials", {"custom": {"pin": "1234"}}, False, 0),
    ("backend_reasoning", {"custom": {"clientSecret": "x"}}, False, 0),
    ("backend_reasoning", {"custom": {"sessionId": "x"}}, False, 0),
    ("backend_models", {"custom": "sk-proj-abcdef"}, False, 0),
    ("backend_models", {"custom": "Bearer abc.def"}, False, 0),
    ("backend_models", {"openrouter": "anthropic/claude-sonnet-4"}, True, 0),
    # review R0 L2: an endpoint must be http(s) with a host
    ("llm_backend_url", "file:///etc/passwd", False, 1),
    ("tts_host", "javascript:alert(1)", False, 1),
    ("backend_endpoints", {"custom": "ftp://host.test/v1"}, False, 1),
    ("tts_host", "", True, 0),
    # review R0 L3: query parameter names, not substrings of the whole query
    ("llm_backend_url", "https://host.test/v1?design=1&monkey=2", True, 0),
    ("llm_backend_url", "https://host.test/v1?apiKey=abc", False, 0),
])
def test_each_preference_is_kept_or_left_out_as_classified(key, value, kept, unclassified):
    clean, counts = _scrub({key: value})
    assert (key in clean) is kept
    if kept:
        assert clean[key] == value
    assert counts["unclassified"] == unclassified
    assert counts["omitted"] == (0 if kept else 1)
    assert clean["telegram_remote_access"] is False


@pytest.mark.parametrize("key,value", [
    ("max_turn_minutes", "60"), ("subagents_enabled", 1), ("agent_name", 7), ("tts_host", 8880),
])
def test_a_known_setting_with_the_wrong_type_is_refused_not_guessed(key, value):
    with pytest.raises(ab.AgentBackupError, match="EXPORT_PREFS_TYPE_INVALID"):
        ex._scrub_preferences_counts(json.dumps({key: value}).encode())


def test_scrubbing_is_idempotent_so_the_verifier_accepts_its_own_output():
    prefs = {**_settings_shaped_prefs(), "pin_hash": "a" * 64, "mystery": 1,
             "llm_backend_url": "https://u:p@h.test", "wire_autonomy": True}
    once, _ = ex._scrub_preferences_counts(json.dumps(prefs).encode())
    twice, counts = ex._scrub_preferences_counts(once)
    assert once == twice and counts["unclassified"] == 0


# ── the realistic regression: first launch + Settings saves → Verified ──────

def _settings_shaped_prefs():
    """What the General, Multimodal, Tools and User profile Settings saves
    write (ui/settings/*_tab.py), with the values those saves take from
    config. The Qt test below drives the real General and Multimodal saves."""
    return {
        "llm_backend": config.LLM_BACKEND, "llm_backend_url": config.LLM_BACKEND_URL,
        "backend_endpoints": {"llamacpp": "http://127.0.0.1:8080/v1"}, "backend_endpoints_migrated": True,
        "max_tool_iterations": config.MAX_TOOL_ITERATIONS,
        "max_tool_calls_per_response": config.MAX_TOOL_CALLS_PER_RESPONSE,
        "max_tool_calls_per_turn": config.MAX_TOOL_CALLS_PER_TURN, "max_turn_minutes": config.MAX_TURN_MINUTES,
        "response_reserve_tokens": config.RESPONSE_RESERVE_TOKENS,
        "context_compaction_enabled": config.CONTEXT_COMPACTION_ENABLED,
        "context_compaction_batch_tokens": config.CONTEXT_COMPACTION_BATCH_TOKENS,
        "dream_sweep_enabled": config.DREAM_SWEEP_ENABLED, "dream_idle_minutes": config.DREAM_IDLE_MINUTES,
        "show_think_blocks": config.SHOW_THINK_BLOCKS, "system_prompt": config.SYSTEM_PROMPT,
        "backend_context": {config.LLM_BACKEND: {"max_context_tokens": 32000, "memory_inject_limit": 12,
                                                 "tool_result_max_chars": 9000}},
        "cloud_credentials": {"openrouter": {"default_model": "z-ai/glm-5.3-flash"}},
        "custom_default_model": "", "omniroute_default_model": "",
        "tts_enabled": True, "tts_host": config.TTS_HOST, "tts_backend": "kokoro",
        "voicebox_host": config.VOICEBOX_HOST, "voicebox_profile": "Rick Sanchez",
        "stt_enabled": False, "stt_backend": "faster-whisper", "stt_model": "base", "stt_device": "cpu",
        "multimodal_routes": {"vision_understanding": {"mode": "specialist", "specialist": "gemini",
                                                       "fallbacks": ["openai"], "model": "gemini-2.5-flash"}},
        "multimodal_disabled_providers": ["qwen"],
        "subagents_enabled": True, "background_tasks_enabled": True, "max_subagent_depth": 3,
        "human_profile_curation_enabled": False, "agent_name": "Lumina",
        "avatar_path": "/old/machine/avatars/rick.png", "user_avatar_path": "/old/machine/me.png",
    }


SECRETS = {"pin_hash": "PIN-SENTINEL", "pin_salt": "SALT-SENTINEL", "openai_api_key": "KEY-SENTINEL",
           "telegram_owner_chat_id": "OWNER-SENTINEL", "telegram_remote_access": True}


def _write_like_a_real_install(data, monkeypatch):
    prefs_path = data / "memory" / "prefs.json"
    monkeypatch.setattr(persistence, "PREFS_PATH", str(prefs_path))
    prefs_path.unlink()
    assert persistence.save(persistence.load())          # first launch: the product's own defaults
    assert persistence.update(_settings_shaped_prefs())   # Settings saves
    assert persistence.update(SECRETS)
    return json.loads(prefs_path.read_text())


def _archived_prefs(path):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("state/preferences/prefs.json"))


def test_a_launched_and_configured_install_makes_a_verified_backup_keeping_its_settings(vsource, monkeypatch):
    on_disk = _write_like_a_real_install(vsource[0], monkeypatch)
    assert DRILL_KEYS <= set(on_disk)
    manifest, receipt = verified(vsource)
    assert receipt["export_kind"] == "verified_agent_backup" and receipt["coverage_blockers"] == []
    assert receipt["full_agent_coverage"] is True
    assert manifest["export_observations"]["prefs_unclassified_count"] == 0
    archived = _archived_prefs(vsource[2])
    for key, value in on_disk.items():
        if key in SECRETS:
            continue
        assert archived[key] == value, key
    assert archived["telegram_remote_access"] is False
    blob = b"".join(read_zip(vsource[2]).values())
    for sentinel in ("PIN-SENTINEL", "SALT-SENTINEL", "KEY-SENTINEL", "OWNER-SENTINEL"):
        assert sentinel.encode() not in blob
    assert receipt["prefs_omitted_count"] == len(SECRETS)  # PIN pair, API key, owner chat id, Telegram switch


def test_an_unfamiliar_preference_is_left_out_counted_and_never_blocks(vsource, monkeypatch):
    _write_like_a_real_install(vsource[0], monkeypatch)
    assert persistence.update({"mystery_pref_from_the_future": "UNKNOWN-SENTINEL"})
    manifest, receipt = verified(vsource)
    assert receipt["coverage_blockers"] == [] and receipt["export_kind"] == "verified_agent_backup"
    assert manifest["export_observations"]["prefs_unclassified_count"] == 1
    assert receipt["semantic_coverage"]["unclassified_preferences_omitted"] == 1
    assert b"UNKNOWN-SENTINEL" not in b"".join(read_zip(vsource[2]).values())


def test_restored_settings_survive_but_nothing_is_active_until_the_owner_recovers(vsource, target, monkeypatch):
    on_disk = _write_like_a_real_install(vsource[0], monkeypatch)
    verified(vsource)
    data_dir, base = target
    _plan, receipt = restore(str(vsource[2]), data_dir, base)
    root = Path(data_dir)
    restored = json.loads((root / "memory" / "prefs.json").read_text())
    for key in ("system_prompt", "voicebox_profile", "multimodal_routes", "backend_context",
                "avatar_path", "tts_host", "subagents_enabled", "max_turn_minutes", "cloud_credentials"):
        assert restored[key] == on_disk[key], key
    assert restored["telegram_remote_access"] is False
    assert not {"pin_hash", "pin_salt", "openai_api_key", "telegram_owner_chat_id"} & set(restored)
    # Backing a setting up never activates it: the restored copy waits for
    # the owner's recovery, with no live safety store and the Wire locked.
    assert S.probe(root).status is S.StoreStatus.ABSENT_WITH_TRACE
    assert G.seat_verdict(root).reason == G.RECOVERED_UNRELEASED
    _use(monkeypatch, root)
    assert R.status().state is not R.State.ACTIVE


def test_restore_transform_keeps_values_and_forces_communications_off():
    raw = json.dumps({**_settings_shaped_prefs(), **SECRETS}).encode()
    data, note = ar._prefs_bytes(raw, "/new/base", set())
    restored = json.loads(data)
    assert restored["multimodal_routes"] == _settings_shaped_prefs()["multimodal_routes"]
    assert restored["telegram_remote_access"] is False and "pin_hash" not in restored
    assert note["unclassified_keys_omitted"] == 0 and note["security_fields_dropped"] == len(SECRETS)


# ── F-DR-1: a missing voice engine never stops startup ──────────────────────

def test_startup_without_the_named_voice_engine_starts_without_voice(capsys):
    import main

    class Missing:
        def get_tts_backend(self):
            raise ModuleNotFoundError("No module named 'torch'")

    assert main._startup_tts_backend(Missing(), True) is None
    assert "Voice backend unavailable, starting without voice" in capsys.readouterr().err

    class Present:
        def get_tts_backend(self):
            return "BACKEND"

    assert main._startup_tts_backend(Present(), True) == "BACKEND"
    assert main._startup_tts_backend(Missing(), False) is None


def test_run_gui_builds_voice_only_through_the_fail_soft_helper():
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    run_gui = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_gui")
    calls = [ast.unparse(n) for n in ast.walk(run_gui) if isinstance(n, ast.Call)]
    assert any(c.startswith("_startup_tts_backend(") for c in calls)
    assert not any(c.startswith("tts_loader.get_tts_backend(") for c in calls)


# ── the real Settings saves (Qt) ────────────────────────────────────────────

def test_real_general_and_multimodal_settings_saves_leave_nothing_unclassified(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    # The hermetic setup of tests/test_prefs_stale_write_01.py: real widgets,
    # real persistence, throwaway prefs/db/credentials.
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    for name in ("TTS_HOST", "VOICEBOX_HOST", "VOICEBOX_PROFILE", "MULTIMODAL_ROUTES",
                 "MULTIMODAL_DISABLED_PROVIDERS", "TTS_ENABLED", "TTS_BACKEND", "STT_ENABLED",
                 "STT_BACKEND", "STT_MODEL", "STT_DEVICE", "SYSTEM_PROMPT"):
        monkeypatch.setattr(config, name, getattr(config, name))
    from types import SimpleNamespace
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab
    from ui.settings.tts_tab import MultimodalTab

    assert persistence.save(persistence.load())  # first launch
    agent = LuminaAgent(owner=True, channel_id="f-dr-4-general", backend="llamacpp")
    GeneralTab(agent, COLORS)._save()
    MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)._save()
    saved = Path(persistence.PREFS_PATH).read_bytes()
    keys = set(json.loads(saved))
    assert {"llm_backend", "max_tool_iterations", "system_prompt", "multimodal_routes", "voicebox_profile"} <= keys
    clean, counts = ex._scrub_preferences_counts(saved)
    assert counts["unclassified"] == 0, sorted(keys - KNOWN)
    assert set(json.loads(clean)) >= keys - ex._DROP_PREFS - ex._DESTINATION_LOCAL_PREFS - {"cloud_credentials"}
