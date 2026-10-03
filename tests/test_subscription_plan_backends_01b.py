"""
SUBSCRIPTION-PLAN-BACKENDS-01B -- backend lane identity, fuel isolation and
dispatch provenance foundation.

Covers (scroll section 18): A legacy identity stability, B lane/model
independence, C preference separation, D dispatch immutability, F router
quota-class denial, G telemetry identity, H backup/persistence boundary,
plus the registry/loader/utility/subagent policy seams those rest on. The
cross-path "fuel isolation" proof (E, with a network tripwire and populated
paid credentials) lives in test_subscription_plan_backends_01b_fuel_isolation.py.

Nothing here touches the network, a real credential, OAuth, or a plan
backend: there is none. tests/conftest.py isolates LUMINA_DATA_DIR and
LUMINA_SECRETS_PATH before any project import, as everywhere in this suite.
"""
import ast
import dataclasses
import json
import os
import sqlite3
import types
from types import MappingProxyType

import pytest

import config
import core.backend_identity as bi
import core.capability_router as cr
import core.persistence as persistence
from core import lane_preferences, reasoning_preferences
from core.backend_identity import (
    BackendIdentity,
    OperationKind,
    QuotaClass,
    ReservedBackendLaneError,
    identity_for_lane,
    identity_of_backend,
)
from core.backends import loader
from core.backends.base import BaseLLMBackend
from core.flight_recorder import FlightRecorder
from core.redaction import SECRET_VALUE_RE, is_secret_key

PLAN = bi.OPENAI_CHATGPT_PLAN_LANE
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pre-01B hardcoded cloud set core/vision_lane.py used to carry. Pinned
# verbatim so the registry-derived placement is provably identical for every
# lane that existed before 01B.
_PRE_01B_CLOUD_SET = frozenset(
    {"openrouter", "deepseek", "groq", "openai", "anthropic", "gemini", "kimi", "qwen"}
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _Stub(BaseLLMBackend):
    """Minimal concrete backend; counts chat() calls."""

    default_url = "http://stub.invalid"
    name = "stub-unregistered"

    def __init__(self, name=None, response=None):
        if name is not None:
            self.name = name
        self._response = response or {"choices": [{"message": {"content": "ok"}}]}
        self.chat_calls = 0

    def get_model(self):
        return "stub-model"

    def list_models(self):
        return ["stub-model"]

    def health_check(self):
        return True, "ok"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking=False, reasoning_effort=None, tool_choice_mode=None):
        self.chat_calls += 1
        return self._response

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7, reasoning_effort=None):
        yield ""

    def configured_model(self):
        return "stub-model"


# ---------------------------------------------------------------------------
# A. Legacy identity stability
# ---------------------------------------------------------------------------

def test_A_openai_and_anthropic_keep_their_existing_meaning():
    assert loader.BACKENDS["openai"].name == "openai"
    assert loader.BACKENDS["anthropic"].name == "anthropic"
    assert loader.BACKENDS["openai"].__name__ == "OpenAIBackend"
    assert loader.BACKENDS["anthropic"].__name__ == "AnthropicBackend"
    openai = identity_for_lane("openai")
    assert (openai.provider_family, openai.backend_lane) == ("openai", "openai")
    assert (openai.transport, openai.auth_source) == (bi.Transport.RESPONSES_API, bi.AuthSource.API_KEY)
    assert (openai.quota_class, openai.cost_class) == (QuotaClass.API_METERED, bi.CostClass.API_METERED)
    anthropic = identity_for_lane("anthropic")
    assert (anthropic.provider_family, anthropic.backend_lane) == ("anthropic", "anthropic")
    assert anthropic.transport is bi.Transport.MESSAGES_API


def test_A_loader_registry_retains_legacy_set_plus_01d_plan():
    """01D adds exactly one fuel-isolated lane to the 01B legacy set."""
    assert set(loader.BACKENDS) == {
        "lmstudio", "ollama", "llamacpp", "vllm", "openrouter", "deepseek", "groq",
        "openai", "openai_chatgpt_plan", "anthropic", "gemini", "kimi", "qwen", "custom", "omniroute",
    }
    assert loader.BACKENDS[PLAN].name == PLAN


def test_A_constructing_legacy_lanes_still_works_and_carries_identity(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "a" * 24)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-" + "b" * 24)
    openai = loader.get_llm_backend("openai")
    anthropic = loader.get_llm_backend("anthropic")
    assert type(openai).__name__ == "OpenAIBackend"
    assert type(anthropic).__name__ == "AnthropicBackend"
    assert openai.backend_identity().backend_lane == "openai"
    assert anthropic.backend_identity().backend_lane == "anthropic"
    assert openai.backend_identity().quota_class is QuotaClass.API_METERED


def test_A_existing_prefs_load_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    legacy = {
        "llm_backend": "openai",
        "backend_context": {"openai": {"max_context_tokens": 111, "memory_inject_limit": 3,
                                       "tool_result_max_chars": 99}},
        "backend_reasoning": {"openai": {"gpt-x": "high"}, "anthropic": {"claude-y": None}},
        "cloud_credentials": {"openai": {"default_model": "gpt-4o-mini"},
                              "anthropic": {"default_model": "claude-sonnet-4-6"}},
    }
    (tmp_path / "prefs.json").write_text(json.dumps(legacy))
    loaded = persistence.load()
    for key, value in legacy.items():
        assert loaded[key] == value
    assert "backend_models" not in loaded   # nothing migrated, nothing invented
    assert reasoning_preferences.get_saved_reasoning(loaded, "openai", "gpt-x") == "high"


# ---------------------------------------------------------------------------
# B. Lane / model independence
# ---------------------------------------------------------------------------

def test_B_same_family_same_model_different_lane_different_fuel():
    api = identity_for_lane("openai", model="gpt-shared-model")
    plan = identity_for_lane(PLAN, model="gpt-shared-model")
    assert api.model == plan.model
    assert api.provider_family == plan.provider_family == "openai"
    assert api.backend_lane != plan.backend_lane
    assert api != plan
    assert api.auth_source is not plan.auth_source
    assert api.quota_class is not plan.quota_class
    assert api.cost_class is not plan.cost_class
    assert api.transport is not plan.transport


def test_B_model_id_never_selects_a_lane():
    """The lane is the class's own `name` declaration. No model string, however
    plan-flavored, changes what get_llm_backend() constructs or what a backend
    object's identity says."""
    for model in ("gpt-5-codex", PLAN, "openai_chatgpt_plan/gpt-5", "chatgpt-plan"):
        backend = loader.get_llm_backend("openai", model=model)
        assert type(backend).__name__ == "OpenAIBackend"
        assert backend.backend_identity().backend_lane == "openai"
        assert backend.backend_identity().quota_class is QuotaClass.API_METERED


def test_B_lane_name_is_the_class_declaration_not_the_credentials(monkeypatch):
    """Populated paid credentials never make a stub claim a paid lane."""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "c" * 24)
    stub = _Stub(name=PLAN)
    ident = stub.backend_identity()
    assert ident.backend_lane == PLAN
    assert ident.auth_source is bi.AuthSource.OAUTH_SUBSCRIPTION
    assert ident.quota_class is QuotaClass.CHATGPT_PLAN_OR_CREDITS


def test_B_identity_is_immutable():
    ident = identity_for_lane("openai", model="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.backend_lane = PLAN
    swapped = ident.with_model("other")
    assert swapped.model == "other" and ident.model == "m"
    assert swapped.backend_lane == ident.backend_lane


def test_B_identity_carries_no_account_or_session_field():
    """Account/workspace/host/session identity must never ride a telemetry-
    bound record. Pinning the exact field set means adding one is a
    deliberate, reviewed act, not an accident."""
    assert {f.name for f in dataclasses.fields(BackendIdentity)} == {
        "provider_family", "backend_lane", "transport", "auth_source",
        "quota_class", "cost_class", "model",
    }
    assert set(identity_for_lane(PLAN, "m").telemetry_fields(OperationKind.UTILITY)) == {
        "provider_family", "backend_lane", "transport", "access_source",
        "quota_class", "cost_class", "operation_kind",
    }


def test_B_identity_of_unregistered_backend_is_unknown_never_free():
    ident = identity_of_backend(types.SimpleNamespace(name="mystery"))
    assert ident.quota_class is QuotaClass.UNKNOWN
    assert ident.auth_source is bi.AuthSource.UNKNOWN
    assert ident.cost_class is bi.CostClass.UNKNOWN_DEBIT
    assert identity_of_backend(None).backend_lane == "unknown"
    assert identity_of_backend(types.SimpleNamespace()).quota_class is QuotaClass.UNKNOWN


def test_B_identity_of_backend_is_total_even_when_configured_model_explodes():
    class Boom:
        name = "openai"

        def configured_model(self):
            raise RuntimeError("boom")

    ident = identity_of_backend(Boom())
    assert ident.backend_lane == "openai" and ident.model is None


# ---------------------------------------------------------------------------
# Registry <-> loader parity (a new lane cannot skip declaring its fuel)
# ---------------------------------------------------------------------------

def test_registry_parity_every_backend_is_classified_and_constructible_matches():
    assert set(loader.BACKENDS) == {n for n, d in bi.LANES.items() if d.constructible}
    for name, cls in loader.BACKENDS.items():
        descriptor = bi.lane_descriptor(name)
        assert descriptor is not None, f"{name} has no fuel classification"
        assert cls.name == descriptor.lane == name
        assert descriptor.quota_class is not QuotaClass.UNKNOWN
        assert descriptor.cost_class is not None
        expected = (frozenset({OperationKind.FOREGROUND_CHAT}) if name == PLAN
                    else frozenset(OperationKind))
        assert descriptor.admitted_operations == expected


def test_registry_is_read_only():
    assert isinstance(bi.LANES, MappingProxyType)
    with pytest.raises(TypeError):
        bi.LANES["rogue"] = bi.LANES["openai"]


def test_unclassified_backend_entry_refuses_to_construct(monkeypatch):
    class Rogue(_Stub):
        name = "rogue"
        default_url = ""

    monkeypatch.setitem(loader.BACKENDS, "rogue", Rogue)
    with pytest.raises(bi.UnclassifiedBackendLaneError):
        loader.get_llm_backend("rogue")


def test_unknown_names_keep_the_existing_error():
    with pytest.raises(ValueError, match="Unknown backend"):
        loader.get_llm_backend("no-such-backend")


def test_lane_descriptor_normalizes_and_rejects_non_strings():
    assert bi.lane_descriptor(" OpenAI ").lane == "openai"
    for junk in (None, 0, b"openai", ["openai"], ""):
        assert bi.lane_descriptor(junk) is None


# ---------------------------------------------------------------------------
# Plan lane: construction is separate from the API-key sibling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", [PLAN, PLAN.upper(), "OpenAI_ChatGPT_Plan"])
def test_plan_lane_constructs_without_paid_fuel_in_every_spelling(spelling, monkeypatch):
    # Populate every paid credential: a fall-through would have something to use.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "d" * 24)
    assert type(loader.get_llm_backend(spelling)).__name__ == "ChatGPTPlanBackend"
    assert type(loader.get_llm_backend(spelling, api_key="sk-" + "e" * 24)).__name__ == "ChatGPTPlanBackend"
    assert loader.get_llm_backend(spelling, model="gpt-whatever").get_model() == "gpt-whatever"


def test_reserved_error_is_a_valueerror_so_existing_handlers_treat_it_as_failure():
    assert issubclass(ReservedBackendLaneError, ValueError)


def test_selected_plan_lane_does_not_fall_through_to_another_backend(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", PLAN)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "f" * 24)
    for name in (None, ""):
        assert type(loader.get_llm_backend(name=name)).__name__ == "ChatGPTPlanBackend"


def test_plan_lane_has_fixed_endpoint():
    assert loader.get_backend_endpoint(PLAN) == "https://api.openai.com/v1"
    assert loader.endpoint_is_configurable(PLAN) is False


def test_plan_lane_is_only_in_general_backend_settings():
    general = open(os.path.join(ROOT, "ui/settings/general_tab.py"), encoding="utf-8").read()
    tts = open(os.path.join(ROOT, "ui/settings/tts_tab.py"), encoding="utf-8").read()
    assert PLAN in general
    assert PLAN not in tts


def test_only_chatgpt_plan_class_declares_plan_lane_and_no_claude_plan():
    import importlib
    import pkgutil
    import core.backends as pkg

    declared = set()
    for mod in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"core.backends.{mod.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, BaseLLMBackend):
                declared.add(getattr(obj, "name", None))
    assert PLAN in declared
    assert "anthropic_claude_plan" not in declared


def test_claude_plan_lane_does_not_exist_anywhere():
    name = "anthropic_claude_plan"
    assert name not in loader.BACKENDS
    assert name not in bi.LANES
    assert bi.lane_descriptor(name) is None
    with pytest.raises(ValueError, match="Unknown backend"):
        loader.get_llm_backend(name)
    ident = identity_for_lane(name)
    assert ident.quota_class is QuotaClass.UNKNOWN          # classified as nothing
    assert ident.auth_source is bi.AuthSource.UNKNOWN
    with pytest.raises(ValueError):
        lane_preferences.set_lane_model({}, name, "m")       # no preference slot either


# ---------------------------------------------------------------------------
# C. Preference separation
# ---------------------------------------------------------------------------

def test_C_lane_state_is_independent_and_survives_reload(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    prefs = persistence.load()

    # API lane (legacy slots).
    prefs["backend_context"] = {"openai": {"max_context_tokens": 111}}
    prefs["cloud_credentials"] = {"openai": {"default_model": "model-A"}}
    reasoning_preferences.set_saved_reasoning(prefs, "openai", "model-A", "high")
    # Plan lane (sibling slots): same provider family, different everything.
    prefs["backend_context"][PLAN] = {"max_context_tokens": 222}
    lane_preferences.set_lane_model(prefs, PLAN, "model-B")
    reasoning_preferences.set_saved_reasoning(prefs, PLAN, "model-B", "medium")
    assert persistence.save(prefs)

    reloaded = persistence.load()
    assert reloaded["cloud_credentials"]["openai"]["default_model"] == "model-A"
    assert lane_preferences.get_lane_model(reloaded, PLAN) == "model-B"
    assert lane_preferences.get_lane_model(reloaded, "openai") is None
    assert reloaded["backend_context"]["openai"]["max_context_tokens"] == 111
    assert reloaded["backend_context"][PLAN]["max_context_tokens"] == 222
    assert reasoning_preferences.get_saved_reasoning(reloaded, "openai", "model-A") == "high"
    assert reasoning_preferences.get_saved_reasoning(reloaded, PLAN, "model-B") == "medium"
    # Same model string on the two lanes still never collides.
    assert reasoning_preferences.get_saved_reasoning(reloaded, PLAN, "model-A") is None
    assert reasoning_preferences.get_saved_reasoning(reloaded, "openai", "model-B") is None


def test_C_changing_one_lane_never_overwrites_the_other(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    prefs = persistence.load()
    prefs["cloud_credentials"] = {"openai": {"default_model": "api-model"}}
    reasoning_preferences.set_saved_reasoning(prefs, "openai", "api-model", "low")
    lane_preferences.set_lane_model(prefs, PLAN, "plan-1")
    persistence.save(prefs)

    prefs = persistence.load()
    lane_preferences.set_lane_model(prefs, PLAN, "plan-2")
    reasoning_preferences.set_saved_reasoning(prefs, PLAN, "plan-2", "high")
    persistence.update({"backend_models": prefs["backend_models"],
                        "backend_reasoning": prefs["backend_reasoning"]})

    final = persistence.load()
    assert final["cloud_credentials"]["openai"]["default_model"] == "api-model"
    assert reasoning_preferences.get_saved_reasoning(final, "openai", "api-model") == "low"
    assert lane_preferences.get_lane_model(final, PLAN) == "plan-2"


def test_C_lane_model_slot_is_for_subscription_lanes_only():
    """Legacy lanes keep their legacy slots: two sources of truth for one lane
    is how a stale value wins."""
    for lane in ("openai", "anthropic", "lmstudio", "custom", "no-such-lane", "", None):
        with pytest.raises(ValueError):
            lane_preferences.set_lane_model({}, lane, "m")


@pytest.mark.parametrize("bad", [
    "", "   ", "has space", "two\nlines", "x" * 201, "sk-" + "a" * 20,
    "Bearer " + "a" * 20, "ghp_" + "a" * 24, "m;rm -rf", 5, ["m"], "../../etc",
])
def test_C_lane_model_rejects_non_model_values_without_touching_prefs(bad):
    prefs = {}
    with pytest.raises(ValueError):
        lane_preferences.set_lane_model(prefs, PLAN, bad)
    assert prefs == {}


def test_C_lane_model_accepts_ordinary_model_ids_and_none_clears():
    prefs = {}
    for model in ("gpt-5-codex", "o3-mini", "vendor/model:free", "m@2025-01+x"):
        lane_preferences.set_lane_model(prefs, PLAN, model)
        assert lane_preferences.get_lane_model(prefs, PLAN) == model
    lane_preferences.set_lane_model(prefs, PLAN, None)
    assert lane_preferences.get_lane_model(prefs, PLAN) is None


def test_C_lane_model_reads_never_raise_on_hand_edited_prefs():
    for bad in ({"backend_models": None}, {"backend_models": []}, {"backend_models": {PLAN: 5}},
                {"backend_models": {PLAN: ""}}, {}):
        assert lane_preferences.get_lane_model(bad, PLAN) is None
    healed = {"backend_models": "garbage"}
    lane_preferences.set_lane_model(healed, PLAN, "m")
    assert healed["backend_models"] == {PLAN: "m"}


def _general_tab_save_function():
    path = os.path.join(ROOT, "ui", "settings", "general_tab.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GeneralTab":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_save":
                    return item
    raise AssertionError("GeneralTab._save not found")


def test_C_settings_save_is_fresh_load_then_mutate_owned_keys_only():
    """Blocks in CI (no PySide6 needed). GeneralTab._save() must never
    replace the prefs document: exactly one assignment to `prefs` (the fresh
    load), and every other write is a subscript on it. Unowned keys -- another
    lane's backend_context/backend_reasoning/backend_models slot -- therefore
    survive a Save by construction."""
    fn = _general_tab_save_function()
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "prefs" for t in n.targets)]
    assert len(assigns) == 1
    call = assigns[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "load_prefs"
    # The per-backend context is read-modify-write on the persisted dict and
    # only the SELECTED backend's key is written.
    src = ast.unparse(fn)
    assert "backend_context = prefs.get('backend_context', {})" in src
    assert "backend_context[config.LLM_BACKEND] = {" in src
    assert "prefs['backend_context'] = backend_context" in src


def test_C_real_settings_save_preserves_sibling_lane_slots(tmp_path, monkeypatch):
    """Real GeneralTab._save() round trip (runs where PySide6 is installed;
    the AST pin above is the CI-blocking half). Saving the OpenAI API lane
    must leave the plan lane's model/context/reasoning slots untouched, and
    write only the API lane's own."""
    pytest.importorskip("PySide6")
    import core.secrets as secrets_module
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(config, "LLM_BACKEND", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "h" * 24)
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "")

    seeded = persistence.load()
    lane_preferences.set_lane_model(seeded, PLAN, "plan-model-1")
    seeded["backend_context"] = {PLAN: {"max_context_tokens": 222, "memory_inject_limit": 5,
                                          "tool_result_max_chars": 77}}
    reasoning_preferences.set_saved_reasoning(seeded, PLAN, "plan-model-1", "medium")
    persistence.save(seeded)

    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab

    agent = LuminaAgent(owner=True, channel_id="01b-settings-save", backend="openai")
    tab = GeneralTab(agent, COLORS)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_key.setText("sk-" + "h" * 24)
    tab.cloud_model.setCurrentText("api-model-1")
    tab._save()

    disk = persistence.load()
    assert disk["llm_backend"] == "openai"
    assert disk["cloud_credentials"]["openai"]["default_model"] == "api-model-1"
    assert "openai" in disk["backend_context"]
    # The plan lane's slots are exactly as seeded.
    assert lane_preferences.get_lane_model(disk, PLAN) == "plan-model-1"
    assert disk["backend_context"][PLAN] == {"max_context_tokens": 222, "memory_inject_limit": 5,
                                              "tool_result_max_chars": 77}
    assert reasoning_preferences.get_saved_reasoning(disk, PLAN, "plan-model-1") == "medium"
    assert PLAN not in disk.get("cloud_credentials", {})   # never created a plan credential record


# ---------------------------------------------------------------------------
# H. Backup / persistence boundary
# ---------------------------------------------------------------------------

def test_H_new_preference_metadata_is_backup_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    prefs = persistence.load()
    lane_preferences.set_lane_model(prefs, PLAN, "gpt-5-codex")
    reasoning_preferences.set_saved_reasoning(prefs, PLAN, "gpt-5-codex", "medium")
    prefs["backend_context"] = {PLAN: {"max_context_tokens": 1}}
    persistence.save(prefs)

    blob = (tmp_path / "prefs.json").read_text()
    assert SECRET_VALUE_RE.search(blob) is None
    data = json.loads(blob)

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not is_secret_key(key), f"secret-shaped key persisted: {key}"
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    # Only the structures 01B introduces are key-checked: backend_context's
    # long-standing "max_context_tokens" legitimately contains "token", and
    # prefs.json never passes through the recorder's key redaction anyway.
    _walk(data["backend_models"])
    _walk(data["backend_reasoning"][PLAN])


def test_H_agent_backup_copies_prefs_whole_so_nothing_credential_may_enter_it():
    """Agent Backup includes prefs.json as one logical member with no key
    allowlist (see core/agent_backup.py state.preferences.prefs_json): whatever
    a preference module writes travels with the backup. This pins why the lane
    preference module is model-id-only and secret-refusing."""
    src = open(os.path.join(ROOT, "core", "agent_backup.py"), encoding="utf-8").read()
    assert '"state.preferences.prefs_json"' in src
    assert "backend_models" not in src      # no per-key handling to forget


def test_H_no_oauth_or_session_material_is_defined_anywhere_in_01b():
    """01B lands no OAuth/session surface. Guard the obvious names so a stray
    01C-shaped implementation cannot ride in under this slice."""
    forbidden = ("code_verifier", "dynamic_agent_client", "refresh_token", "id_token",
                 "chatgpt.tokens.use.direct", "/auth/callback", "issued_client_id",
                 "host_id")
    for rel in ("core/backend_identity.py", "core/lane_preferences.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for needle in forbidden:
            assert needle not in text, f"{needle!r} found in {rel}"


# ---------------------------------------------------------------------------
# Module purity (the router imports it)
# ---------------------------------------------------------------------------

def test_backend_identity_module_is_pure():
    path = os.path.join(ROOT, "core", "backend_identity.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"__future__", "dataclasses", "enum", "types", "typing"}, sorted(imported)
    for node in tree.body:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant)


# ---------------------------------------------------------------------------
# Fuel crossing policy
# ---------------------------------------------------------------------------

_PLAN_Q = QuotaClass.CHATGPT_PLAN_OR_CREDITS


@pytest.mark.parametrize("other", [
    QuotaClass.API_METERED, QuotaClass.LOCAL_COMPUTE, QuotaClass.ENDPOINT_DEFINED,
    QuotaClass.UNKNOWN, None, "api_metered", "garbage",
])
def test_F_protected_fuel_is_denied_in_both_directions(other):
    assert bi.fuel_crossing_admitted(_PLAN_Q, other) is False
    assert bi.fuel_crossing_admitted(other, _PLAN_Q) is False


def test_F_same_fuel_is_admitted():
    for q in QuotaClass:
        assert bi.fuel_crossing_admitted(q, q) is True


def test_F_legacy_crossings_are_grandfathered_exactly_as_before_01b():
    """local <-> metered <-> endpoint-defined routing is what the product did
    before 01B (explicit owner route, or Auto's local-first-then-cloud
    tiering). 01B does not tighten it; this test makes the choice visible so
    a later slice changes it deliberately."""
    legacy = [QuotaClass.LOCAL_COMPUTE, QuotaClass.API_METERED, QuotaClass.ENDPOINT_DEFINED]
    for a in legacy:
        for b in legacy:
            assert bi.fuel_crossing_admitted(a, b) is True


def test_F_policy_accepts_identities_lane_strings_and_rejects_junk_as_unknown():
    assert bi.fuel_crossing_admitted(identity_for_lane("openai"), identity_for_lane(PLAN)) is False
    assert bi.fuel_crossing_admitted(identity_for_lane("openai"), identity_for_lane("anthropic")) is True
    assert bi.quota_class_of(object()) is QuotaClass.UNKNOWN
    assert bi.quota_class_of("chatgpt_plan_or_credits") is _PLAN_Q


def test_model_equality_never_implies_lane_equality_in_the_policy():
    a = identity_for_lane("openai", model="same")
    b = identity_for_lane(PLAN, model="same")
    assert a.model == b.model
    assert bi.fuel_crossing_admitted(a, b) is False


# ---------------------------------------------------------------------------
# Operation admission
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", list(OperationKind))
def test_plan_lane_admits_only_foreground_in_01d(op):
    expected = op is not OperationKind.FOREGROUND_CHAT
    assert (bi.operation_refusal(PLAN, op) is not None) is expected
    assert (bi.operation_refusal(identity_for_lane(PLAN), op) is not None) is expected


@pytest.mark.parametrize("lane", sorted(set(loader.BACKENDS) - {PLAN}))
@pytest.mark.parametrize("op", list(OperationKind))
def test_every_legacy_lane_admits_every_operation(lane, op):
    assert bi.operation_refusal(lane, op) is None


def test_operation_refusal_text_is_bounded_and_credential_free():
    for op in OperationKind:
        text = bi.operation_refusal(PLAN, op)
        if op is OperationKind.FOREGROUND_CHAT:
            assert text is None
            continue
        assert text and len(text) < 300
        assert SECRET_VALUE_RE.search(text) is None
        assert "sk-" not in text


def test_unregistered_lane_carries_no_operation_restriction():
    assert bi.operation_refusal("fake-backend", OperationKind.UTILITY) is None


# ---------------------------------------------------------------------------
# F. Router quota-class denial
# ---------------------------------------------------------------------------

def _record(name, quota, local=False):
    return cr.SpecialistRecord(
        name=name, kind="llm_backend", local=local,
        capabilities={cr.Capability.VISION_UNDERSTANDING: cr.EvidenceClass.EVIDENCED},
        quota_class=quota,
    )


def _route(mode, specialist=None, fallbacks=()):
    return cr.RoutingPolicy(routes={"vision_understanding": cr.CapabilityRoute(
        mode=mode, specialist=specialist, fallbacks=tuple(fallbacks))})


def _resolve(records, policy, primary_backend, primary_quota):
    return cr.resolve_capability(
        cr.CapabilityRegistry(records), policy, cr.Capability.VISION_UNDERSTANDING,
        primary_backend=primary_backend, primary_quota_class=primary_quota,
    )


def test_F_explicit_specialist_across_protected_fuel_is_denied():
    decision = _resolve(
        [_record("openai", QuotaClass.API_METERED)],
        _route("specialist", "openai"), PLAN, _PLAN_Q)
    assert decision.outcome == cr.OUTCOME_NO_BACKEND
    assert "cross-fuel candidate denied" in decision.evidence["openai"]["skip_reason"]
    assert decision.evidence["openai"]["admitted"] is False
    assert decision.evidence["openai"]["quota_class"] == "api_metered"


def test_F_fallback_list_never_crosses_protected_fuel():
    records = [_record(n, QuotaClass.API_METERED) for n in ("openai", "anthropic", "openrouter")]
    decision = _resolve(records, _route("specialist", "openai", ("anthropic", "openrouter")),
                        PLAN, _PLAN_Q)
    assert decision.outcome == cr.OUTCOME_NO_BACKEND
    for name in ("openai", "anthropic", "openrouter"):
        assert "cross-fuel candidate denied" in decision.evidence[name]["skip_reason"]


def test_F_auto_never_crosses_protected_fuel_even_when_it_is_the_only_cloud_candidate():
    records = [_record("openai", QuotaClass.API_METERED), _record("anthropic", QuotaClass.API_METERED)]
    decision = _resolve(records, _route("auto"), PLAN, _PLAN_Q)
    assert decision.outcome == cr.OUTCOME_NO_BACKEND


def test_F_a_protected_candidate_is_denied_for_a_non_protected_primary():
    """The reverse direction: a paid/local primary must not silently drain the
    owner's plan allowance through a vision route."""
    for primary_q in (QuotaClass.API_METERED, QuotaClass.LOCAL_COMPUTE, QuotaClass.UNKNOWN, None):
        decision = _resolve(
            [_record(PLAN, _PLAN_Q)], _route("auto"), "lmstudio", primary_q)
        assert decision.outcome == cr.OUTCOME_NO_BACKEND, primary_q
        assert "cross-fuel" in decision.evidence[PLAN]["skip_reason"]


def test_F_same_fuel_candidate_is_considered_per_existing_policy():
    decision = _resolve(
        [_record("anthropic", QuotaClass.API_METERED)],
        _route("specialist", "anthropic"), "openai", QuotaClass.API_METERED)
    assert decision.outcome == cr.OUTCOME_ROUTED and decision.selected == "anthropic"
    # And a primary that IS protected may still not select itself (existing law).
    again = _resolve([_record(PLAN, _PLAN_Q)], _route("specialist", PLAN), PLAN, _PLAN_Q)
    assert again.outcome == cr.OUTCOME_NO_BACKEND
    assert "outside the specialist candidate space" in again.evidence[PLAN]["skip_reason"]


def test_F_legacy_cross_fuel_routing_is_unchanged():
    """local primary -> paid cloud specialist (Auto's cloud tier) still routes."""
    decision = _resolve(
        [_record("openai", QuotaClass.API_METERED)],
        _route("auto"), "lmstudio", QuotaClass.LOCAL_COMPUTE)
    assert decision.outcome == cr.OUTCOME_ROUTED and decision.selected == "openai"
    assert decision.classification == cr.CLASSIFICATION_AUTO_CLOUD


def test_F_unclassified_records_and_unspecified_primary_keep_pre_01b_behavior():
    plain = cr.SpecialistRecord(
        name="alpha", kind="llm_backend", local=True,
        capabilities={cr.Capability.VISION_UNDERSTANDING: cr.EvidenceClass.EVIDENCED})
    assert plain.quota_class is None
    decision = cr.resolve_capability(
        cr.CapabilityRegistry([plain]), _route("specialist", "alpha"),
        cr.Capability.VISION_UNDERSTANDING, primary_backend="beta")
    assert decision.outcome == cr.OUTCOME_ROUTED
    assert "quota_class" not in decision.evidence["alpha"]   # ledger shape unchanged


def test_F_denied_candidates_are_never_health_probed():
    probes = []
    registry = cr.CapabilityRegistry(
        [_record("openai", QuotaClass.API_METERED)],
        health_lookup=lambda name: probes.append(name) or (True, "ok"),
        support_lookup=lambda name, cap: probes.append(("support", name)) or cr.EvidenceClass.EVIDENCED,
    )
    decision = cr.resolve_capability(
        registry, _route("specialist", "openai"), cr.Capability.VISION_UNDERSTANDING,
        primary_backend=PLAN, primary_quota_class=_PLAN_Q)
    assert decision.outcome == cr.OUTCOME_NO_BACKEND
    assert probes == []      # denied before any live-probe primitive runs


def test_F_record_normalizes_and_validates_quota_class():
    assert _record("x", "api_metered").quota_class is QuotaClass.API_METERED
    with pytest.raises(ValueError):
        _record("x", "not-a-class")


def test_F_router_ledger_text_is_redacted_and_bounded_for_denials():
    decision = _resolve([_record("openai", QuotaClass.API_METERED)],
                        _route("specialist", "openai"), PLAN, _PLAN_Q)
    text = decision.reason
    assert SECRET_VALUE_RE.search(text) is None


def test_F_placement_is_registry_derived_and_identical_to_pre_01b_for_every_lane():
    from core import vision_lane
    for name in set(loader.BACKENDS) - {PLAN}:
        assert bi.is_local_placement(name) == (name not in _PRE_01B_CLOUD_SET), name
    assert not hasattr(vision_lane, "_CLOUD_LLM_BACKENDS")


def test_F_remote_lanes_and_unknown_names_are_never_local():
    assert bi.is_local_placement(PLAN) is False
    assert bi.is_local_placement("brand-new-remote-thing") is False
    assert bi.is_local_placement(None) is False


# ---------------------------------------------------------------------------
# F (vision admission through the real vision lane)
# ---------------------------------------------------------------------------

def _vision_agent(primary_name, primary_model="m"):
    primary = types.SimpleNamespace(name=primary_name, configured_model=lambda: primary_model)
    ctx = types.SimpleNamespace(history=[], mark_untrusted_seen=lambda: None)
    return types.SimpleNamespace(llm=primary, ctx=ctx)


def _img():
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_F_vision_route_from_plan_primary_cannot_admit_a_paid_specialist(monkeypatch):
    from core import vision_lane
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": {
        "mode": "specialist", "specialist": "openai", "fallbacks": ["anthropic", "openrouter"]}})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    constructed = []
    monkeypatch.setattr(loader, "get_llm_backend",
                        lambda **kw: constructed.append(kw) or pytest.fail("constructed"))
    agent = _vision_agent(PLAN)
    content, ctx = vision_lane.prepare_routed_turn(agent, [_img(), {"type": "text", "text": "q"}])
    assert ctx.decision.outcome == cr.OUTCOME_NO_BACKEND
    assert all(b.get("type") != "image_url" for b in content)        # routed semantics held
    agent.ctx.history.append({"role": "user", "content": list(content)})
    result = vision_lane.execute_routed_vision(agent, ctx)
    assert result.outcome == "no_specialist"
    assert constructed == []


def test_F_vision_route_for_a_paid_primary_cannot_admit_the_plan_specialist(monkeypatch):
    from core import vision_lane
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": {
        "mode": "specialist", "specialist": PLAN}})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    _content, ctx = vision_lane.prepare_routed_turn(
        _vision_agent("openai"), [_img(), {"type": "text", "text": "q"}])
    assert ctx.decision.outcome == cr.OUTCOME_NO_BACKEND


@pytest.mark.parametrize("spelling", ["OpenAI_ChatGPT_Plan", PLAN.upper(), f" {PLAN} "])
def test_F_a_respelled_plan_lane_in_a_route_is_still_recognized_and_denied(spelling, monkeypatch):
    """Lane recognition normalizes case/whitespace exactly as the loader does,
    so a hand-edited route cannot smuggle the protected lane past the fuel
    gate (or into the local tier) by respelling it."""
    from core import vision_lane
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": {
        "mode": "specialist", "specialist": spelling}})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    registry, _p, _r = vision_lane._build_routing()
    record = registry.get(spelling)
    assert record.quota_class is _PLAN_Q and record.local is False
    _content, ctx = vision_lane.prepare_routed_turn(
        _vision_agent("lmstudio"), [_img(), {"type": "text", "text": "q"}])
    assert ctx.decision.outcome == cr.OUTCOME_NO_BACKEND
    assert "cross-fuel" in ctx.decision.evidence[spelling]["skip_reason"]


def test_F_vision_registry_records_carry_lane_derived_fuel_and_placement(monkeypatch):
    from core import vision_lane
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": {
        "mode": "specialist", "specialist": "openai", "fallbacks": ["lmstudio", PLAN, "typo-lane"]}})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    registry, _policy, _routes = vision_lane._build_routing()
    assert registry.get("openai").quota_class is QuotaClass.API_METERED and not registry.get("openai").local
    assert registry.get("lmstudio").quota_class is QuotaClass.LOCAL_COMPUTE and registry.get("lmstudio").local
    plan = registry.get(PLAN)
    assert plan.quota_class is _PLAN_Q and plan.local is False       # remote, never "local by omission"
    typo = registry.get("typo-lane")
    assert typo.quota_class is QuotaClass.UNKNOWN and typo.local is False


def test_F_vision_telemetry_event_carries_specialist_lane_and_primary_fuel(monkeypatch, tmp_path):
    from core import vision_lane
    recorder = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    agent = _vision_agent("lmstudio")
    agent.flight_recorder = recorder
    result = vision_lane.VisionLaneResult(outcome="success", provider="openai", model="gpt-x",
                                          route_classification="explicit", image_count=1)
    ctx = types.SimpleNamespace(decision=types.SimpleNamespace(outcome="routed"), images=(1,))
    vision_lane._record_route_event(agent, ctx, result, turn_id="t", chat_id=1,
                                    primary_backend="lmstudio")
    fields = json.loads(_events(recorder, "turn.vision_route")[0]["fields_json"])
    assert fields["backend_lane"] == "openai"
    assert fields["quota_class"] == "api_metered"
    assert fields["operation_kind"] == "vision_specialist"
    assert fields["primary_backend"] == "lmstudio"
    assert fields["primary_quota_class"] == "local_compute"
    assert fields["access_source"] == "api_key"          # survived redaction


# ---------------------------------------------------------------------------
# Utility / Reforge fuel lock (the shared seam in BaseLLMBackend)
# ---------------------------------------------------------------------------

def test_utility_on_unregistered_and_legacy_lanes_is_unchanged():
    for name in ("stub-unregistered", "openai", "lmstudio"):
        stub = _Stub(name=name)
        assert stub.complete_utility("p", prefill="T:") == "ok"
        assert stub.complete_utility_content_only("p") == "ok"
        assert stub.chat_calls == 2


def test_utility_on_the_plan_lane_fails_unavailable_without_dispatch(capsys):
    stub = _Stub(name=PLAN)
    assert stub.complete_utility("auto-name this", prefill="TITLE:") is None
    assert stub.chat_calls == 0
    assert "reason=lane_unavailable" in capsys.readouterr().out


def test_reforge_on_the_plan_lane_fails_unavailable_without_dispatch(capsys):
    stub = _Stub(name=PLAN)
    assert stub.complete_utility_content_only("compile this") is None
    assert stub.chat_calls == 0
    out = capsys.readouterr().out
    assert "complete_utility_content_only failed" in out and "lane_unavailable" in out


def test_utility_lock_is_per_operation_so_a_future_slice_can_enable_them_separately(monkeypatch):
    """01F will enable background utility and Reforge independently. The seam
    must already distinguish them: admitting UTILITY must not admit REFORGE."""
    base = bi.LANES[PLAN]
    only_utility = dataclasses.replace(base, admitted_operations=frozenset({OperationKind.UTILITY}))
    monkeypatch.setattr(bi, "LANES", MappingProxyType({**bi.LANES, PLAN: only_utility}))
    stub = _Stub(name=PLAN)
    assert stub.complete_utility("p") == "ok"
    assert stub.complete_utility_content_only("p") is None
    assert stub.chat_calls == 1


# ---------------------------------------------------------------------------
# Subagents / background tasks (model-supplied `backend` override)
# ---------------------------------------------------------------------------

def test_subagent_override_cannot_cross_protected_fuel_in_either_direction():
    assert bi.subagent_lane_refusal("openai", "openai") is None
    assert bi.subagent_lane_refusal("openai", None) is None
    assert bi.subagent_lane_refusal("openai", "anthropic") is None       # grandfathered
    assert bi.subagent_lane_refusal("lmstudio", "openai") is None        # grandfathered
    # Child on the protected lane: it admits no SUBAGENT work.
    assert "does not admit subagent" in bi.subagent_lane_refusal("openai", PLAN)
    assert "does not admit subagent" in bi.subagent_lane_refusal(PLAN, None)
    # Selected lane protected: any other lane would change fuel.
    refusal = bi.subagent_lane_refusal(PLAN, "openai")
    assert refusal and "change fuel" in refusal


@pytest.mark.parametrize("spelling", [PLAN.upper(), f"  {PLAN}  ", "OpenAI_ChatGPT_Plan"])
def test_subagent_override_respelling_does_not_evade_the_lane_guard(spelling):
    assert "does not admit subagent" in bi.subagent_lane_refusal("openai", spelling)
    assert "does not admit subagent" in bi.subagent_lane_refusal(spelling, None)


def test_subagent_override_crossing_check_works_even_if_the_plan_lane_admits_subagents(monkeypatch):
    """Independent of the operation flag: once a later slice enables subagent
    work on the plan lane, an override to ANOTHER lane still must not cross."""
    open_plan = dataclasses.replace(bi.LANES[PLAN], admitted_operations=frozenset(OperationKind))
    monkeypatch.setattr(bi, "LANES", MappingProxyType({**bi.LANES, PLAN: open_plan}))
    assert bi.subagent_lane_refusal(PLAN, None) is None
    assert bi.subagent_lane_refusal(PLAN, PLAN) is None
    assert "change fuel" in bi.subagent_lane_refusal(PLAN, "openai")
    assert "change fuel" in bi.subagent_lane_refusal("openai", PLAN)


def test_spawn_subagent_refuses_before_constructing_anything(monkeypatch):
    import tools.subagent as subagent

    monkeypatch.setattr(config, "LLM_BACKEND", PLAN)
    built = []
    monkeypatch.setattr(subagent, "LuminaAgent", lambda **kw: built.append(kw) or pytest.fail("built"))
    for backend in (None, "openai", "anthropic", PLAN):
        out = subagent.spawn_subagent("do a thing", backend=backend)
        assert out["success"] is False and out["tool_calls_made"] == 0
        assert out["error"]
    assert built == []


def test_scheduled_task_binds_its_lane_at_fire_time_not_schedule_time(monkeypatch):
    """A task queued while an API lane was selected must refuse if, by the time
    it fires, the owner has since selected the protected lane."""
    import tools.subagent as subagent
    built = []
    monkeypatch.setattr(subagent, "LuminaAgent", lambda **kw: built.append(kw) or pytest.fail("built"))
    monkeypatch.setattr(config, "LLM_BACKEND", "openai")      # "schedule time": fine
    assert bi.subagent_lane_refusal(config.LLM_BACKEND, None) is None
    monkeypatch.setattr(config, "LLM_BACKEND", PLAN)           # "fire time"
    out = subagent.spawn_subagent("deferred task")
    assert out["success"] is False and built == []


def test_spawn_subagent_legacy_lanes_still_construct(monkeypatch):
    import tools.subagent as subagent

    class _Agent:
        def __init__(self, **kw):
            self.kw = kw
            self.registry = types.SimpleNamespace(all_tool_names=lambda: [], set_disabled=lambda x: None)
            self.on_tool_call = None

        def apply_persona(self, p):
            pass

        def chat(self, *a, **k):
            return "done"

    monkeypatch.setattr(subagent, "LuminaAgent", _Agent)
    monkeypatch.setattr(subagent, "apply_tool_profile", lambda *a, **k: None)
    monkeypatch.setattr(config, "LLM_BACKEND", "lmstudio")
    out = subagent.spawn_subagent("task", backend="anthropic")
    assert out["success"] is True, out


# ---------------------------------------------------------------------------
# D. Dispatch immutability (the pure guard; agent-loop proofs are in the
#    fuel-isolation file)
# ---------------------------------------------------------------------------

def test_D_dispatch_guard_matrix():
    api = identity_for_lane("openai")
    anth = identity_for_lane("anthropic")
    plan = identity_for_lane(PLAN)
    chat = OperationKind.FOREGROUND_CHAT
    assert bi.dispatch_lane_refusal(None, api, chat) is None
    assert bi.dispatch_lane_refusal(api, api, chat) is None
    assert bi.dispatch_lane_refusal(api, anth, chat) is None             # grandfathered legacy swap
    assert bi.dispatch_lane_refusal(api, api.with_model("other"), chat) is None   # model != lane
    assert bi.dispatch_lane_refusal(plan, api, chat) is not None
    assert bi.dispatch_lane_refusal(api, plan, chat) is not None
    assert bi.dispatch_lane_refusal(plan, plan, chat) is None            # 01D foreground only


def test_D_dispatch_guard_with_the_plan_lane_enabled_still_forbids_leaving_it(monkeypatch):
    open_plan = dataclasses.replace(bi.LANES[PLAN], admitted_operations=frozenset(OperationKind))
    monkeypatch.setattr(bi, "LANES", MappingProxyType({**bi.LANES, PLAN: open_plan}))
    plan = identity_for_lane(PLAN)
    chat = OperationKind.FOREGROUND_CHAT
    assert bi.dispatch_lane_refusal(plan, plan, chat) is None
    assert "does not change fuel" in bi.dispatch_lane_refusal(plan, identity_for_lane("openai"), chat)
    assert bi.dispatch_lane_refusal(plan, identity_for_lane("unregistered-x"), chat) is not None


# ---------------------------------------------------------------------------
# G. Telemetry identity
# ---------------------------------------------------------------------------

def _events(recorder, event_type=None):
    conn = sqlite3.connect(recorder.db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    conn.close()
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def test_G_telemetry_projection_distinguishes_family_from_lane():
    api = identity_for_lane("openai", "m").telemetry_fields(OperationKind.FOREGROUND_CHAT)
    plan = identity_for_lane(PLAN, "m").telemetry_fields(OperationKind.FOREGROUND_CHAT)
    assert api["provider_family"] == plan["provider_family"] == "openai"
    assert api["backend_lane"] == "openai" and plan["backend_lane"] == PLAN
    assert api["quota_class"] == "api_metered" and plan["quota_class"] == "chatgpt_plan_or_credits"
    assert api["cost_class"] == "api_metered" and plan["cost_class"] == "unknown_debit"
    assert api["transport"] == "responses_api" and plan["transport"] == "responses_siwc"
    assert api["access_source"] == "api_key" and plan["access_source"] == "oauth_subscription"
    assert plan["operation_kind"] == "foreground_chat"


def test_G_unknown_cost_is_never_represented_as_zero():
    for lane in (PLAN, "never-heard-of-it"):
        fields = identity_for_lane(lane).telemetry_fields()
        assert fields["cost_class"] == "unknown_debit"
        blob = json.dumps(fields)
        assert "cost_amount" not in blob and ": 0" not in blob and "free" not in blob.lower()
    # The only two ways a cost is ever "known" are metered or local -- both named.
    assert {c.value for c in bi.CostClass} == {
        "local_compute", "api_metered", "endpoint_defined", "unknown_debit"}


def test_G_projection_values_survive_the_real_recorder_unredacted(tmp_path):
    """The auth-source value rides under `access_source`, not `auth_source`:
    SECRET_KEY_MARKERS redacts any key containing "auth". Round-trip through a
    real FlightRecorder so a rename back to the obvious name fails loudly."""
    assert is_secret_key("auth_source") is True            # the trap, pinned
    recorder = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    fields = identity_for_lane(PLAN, "m").telemetry_fields(OperationKind.UTILITY)
    recorder.record_machine_event("provider.dispatch", backend=PLAN, model="m", fields=fields)
    row = _events(recorder, "provider.dispatch")[0]
    stored = json.loads(row["fields_json"])
    assert stored == fields
    assert "[REDACTED]" not in row["fields_json"]
    assert row["backend"] == PLAN                           # the lane, never the family


def test_G_no_account_or_credential_material_in_identity_text():
    for lane in bi.LANES:
        text = json.dumps(identity_for_lane(lane, "m").telemetry_fields(OperationKind.UTILITY))
        assert SECRET_VALUE_RE.search(text) is None
        for needle in ("@", "token", "secret", "password", "subject", "workspace"):
            assert needle not in text.lower()


def test_G_openai_api_backend_keeps_its_own_lane_in_its_hardcoded_events():
    """openai_backend.py records backend="openai" for its own capability
    events. That is correct precisely because OpenAIBackend IS the API lane;
    the future plan lane must be a different class and never reuse it."""
    src = open(os.path.join(ROOT, "core", "backends", "openai_backend.py"), encoding="utf-8").read()
    assert 'backend="openai"' in src
    assert loader.BACKENDS["openai"].name == "openai"


# ---------------------------------------------------------------------------
# Secrets / config: the plan lane has no credential path of its own
# ---------------------------------------------------------------------------

def test_plan_identity_never_reads_api_key_state(monkeypatch):
    """Instrument every credential accessor, then exercise every identity,
    policy and preference function for the plan lane. None may touch them."""
    import core.secrets as secrets

    touched = []
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: touched.append(a) or "sk-" + "x" * 24)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "g" * 24)

    stub = _Stub(name=PLAN)
    stub.backend_identity()
    identity_for_lane(PLAN, "m").telemetry_fields(OperationKind.UTILITY)
    bi.fuel_crossing_admitted(identity_for_lane(PLAN), identity_for_lane("openai"))
    bi.operation_refusal(PLAN, OperationKind.UTILITY)
    bi.dispatch_lane_refusal(identity_for_lane(PLAN), identity_for_lane("openai"), OperationKind.FOREGROUND_CHAT)
    bi.subagent_lane_refusal(PLAN, "openai")
    lane_preferences.set_lane_model({}, PLAN, "m")
    stub.complete_utility("p")
    assert type(loader.get_llm_backend(PLAN)).__name__ == "ChatGPTPlanBackend"
    assert touched == []


def test_openai_api_key_is_not_inherited_by_provider_family():
    """The API key stays in the paid OpenAI transport only."""
    offenders = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "core")):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            if "OPENAI_API_KEY" in open(path, encoding="utf-8").read():
                offenders.append(os.path.relpath(path, ROOT))
    assert sorted(offenders) == ["core/backends/openai_backend.py"]
