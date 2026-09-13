"""
MULTIMODAL-M1-CAPABILITY-REGISTRY-01 — provider-neutral capability registry
+ deterministic specialist selection.

M1 slice of MULTIMODAL-CAPABILITY-ROUTER-01: pure registry + policy layer.
No media execution, no history mutation, no UI, no MB-34 repair, no network,
no persistence. Verified, not assumed: every design law from the canonical
Multimodal technical note (with Other-Claude's §16 amendments) is pinned by
a named test here.

Owner-scroll test mapping (all 18 required proofs):
  1  test_canonical_vocabulary_exact_set
  2  test_specialist_advertises_multiple_capabilities
  3  test_unsupported_capability_explicit_no_route
  4  test_record_disabled_provider_never_selected
     test_policy_disabled_provider_never_selected
  5  test_unhealthy_provider_skipped
  6  test_owner_preference_wins_among_eligible_candidates
  7  test_local_preferred_only_when_quality_floor_met
  8  test_below_floor_local_falls_through_deterministically
  9  test_explicit_fallback_when_preferred_unavailable
  10 test_fallback_never_uses_owner_disabled_provider
  11 test_identical_inputs_produce_identical_decisions
  12 test_primary_backend_never_selected_or_mutated
  13 test_provenance_explains_selected_and_skipped
  14 test_provenance_contains_no_secrets
  15 test_defaults_preserve_existing_behavior
  16 test_import_purity_no_network_io
     test_disabled_lane_performs_no_lookups
  17 test_durability_ephemeral_contract
  18 test_unknown_capability_fails_truthfully
     test_unknown_provider_fails_safely

Extras: evidence honesty (UNKNOWN never collapsed into UNSUPPORTED — the
MB-34 live shape must stay truthfully representable), floor gates explicit
specialists too, parse_routes fail-closed discipline.
"""

import ast
import dataclasses
import inspect
import os
import random

import pytest

import config
from core.capability_router import (
    CANONICAL_CAPABILITIES,
    CLASSIFICATION_AUTO_CLOUD,
    CLASSIFICATION_AUTO_LOCAL,
    CLASSIFICATION_EXPLICIT,
    CLASSIFICATION_FALLBACK,
    Capability,
    CapabilityRegistry,
    CapabilityRoute,
    EvidenceClass,
    LANE_AUTO,
    LANE_DISABLED,
    LANE_SPECIALIST,
    OUTCOME_DISABLED,
    OUTCOME_INVALID_CAPABILITY,
    OUTCOME_NO_BACKEND,
    OUTCOME_ROUTED,
    RoutingDecision,
    RoutingPolicy,
    SpecialistRecord,
    parse_routes,
    resolve_capability,
)

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "core", "capability_router.py")

FAKE_SECRET = "sk-fakekey1234567890abcdef"  # matches redaction SECRET_VALUE_RE sk- shape


def _llm(name, caps, local=False, enabled=True, label=None):
    return SpecialistRecord(
        name=name,
        kind="llm_backend",
        local=local,
        capabilities=caps,
        enabled=enabled,
        quality_label=label,
    )


def _health_map(unhealthy=()):
    calls = []

    def look(name):
        calls.append(name)
        if name in unhealthy:
            return False, f"{name} unreachable (synthetic diagnostic {FAKE_SECRET})"
        return True, "ok"

    look.calls = calls
    return look


def _auto_policy(vision_route, disabled=(), quality_order=()):
    return RoutingPolicy(
        routes={"vision_understanding": vision_route},
        disabled_providers=disabled,
        quality_order=quality_order,
    )


# ---------------------------------------------------------------------------
# 1. Vocabulary
# ---------------------------------------------------------------------------

def test_canonical_vocabulary_exact_set():
    # Exact-set guard (tool-profile exact-set convention): the vocabulary is
    # frozen; adding/removing a capability must break this test loudly.
    assert {c.value for c in CANONICAL_CAPABILITIES} == {
        "vision_understanding",
        "image_generation",
        "audio_understanding",
        "speech_synthesis",
        "video_understanding",
        "video_generation",
    }
    assert all(isinstance(c, Capability) for c in CANONICAL_CAPABILITIES)


# ---------------------------------------------------------------------------
# 2. Multi-capability specialists
# ---------------------------------------------------------------------------

def test_specialist_advertises_multiple_capabilities():
    registry = CapabilityRegistry([
        _llm("luna", {
            "vision_understanding": EvidenceClass.EVIDENCED,
            "audio_understanding": EvidenceClass.EVIDENCED,
        }),
    ])
    policy = RoutingPolicy(routes={
        "vision_understanding": CapabilityRoute(mode=LANE_AUTO),
        "audio_understanding": CapabilityRoute(mode=LANE_AUTO),
    })
    for capability in (Capability.VISION_UNDERSTANDING, Capability.AUDIO_UNDERSTANDING):
        decision = resolve_capability(registry, policy, capability)
        assert decision.outcome == OUTCOME_ROUTED
        assert decision.selected == "luna"
        assert decision.classification == CLASSIFICATION_AUTO_CLOUD


# ---------------------------------------------------------------------------
# 3. Unsupported capability -> explicit no-route
# ---------------------------------------------------------------------------

def test_unsupported_capability_explicit_no_route():
    registry = CapabilityRegistry([
        _llm("painter", {"image_generation": EvidenceClass.EVIDENCED}, local=True),
    ])
    policy = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST, specialist="painter"))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_NO_BACKEND
    assert decision.selected is None
    assert decision.evidence["painter"]["evidence"] == "unsupported"
    assert "declared unsupported" in decision.evidence["painter"]["skip_reason"]
    # auto mode behaves identically for the same space
    auto = resolve_capability(registry, _auto_policy(CapabilityRoute(mode=LANE_AUTO)),
                              Capability.VISION_UNDERSTANDING)
    assert auto.outcome == OUTCOME_NO_BACKEND


# ---------------------------------------------------------------------------
# 4. Disabled providers never selected (record-level and policy-level)
# ---------------------------------------------------------------------------

def test_record_disabled_provider_never_selected():
    registry = CapabilityRegistry([
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}, enabled=False),
        _llm("glm-vision", {"vision_understanding": EvidenceClass.EVIDENCED}),
    ])
    policy = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST, specialist="luna"))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_NO_BACKEND
    assert decision.evidence["luna"]["skip_reason"] == "specialist disabled"
    assert decision.selected != "luna"


def test_policy_disabled_provider_never_selected():
    registry = CapabilityRegistry([
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
        _llm("glm-vision", {"vision_understanding": EvidenceClass.EVIDENCED}),
    ])
    policy = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST, specialist="luna"),
                          disabled=("luna",))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_NO_BACKEND
    assert decision.evidence["luna"]["skip_reason"] == "owner-disabled provider"
    assert decision.selected != "luna"


# ---------------------------------------------------------------------------
# 5. Unhealthy providers skipped
# ---------------------------------------------------------------------------

def test_unhealthy_provider_skipped():
    health = _health_map(unhealthy=("luna",))
    registry = CapabilityRegistry(
        [
            _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("glm-vision", {"vision_understanding": EvidenceClass.EVIDENCED}),
        ],
        health_lookup=health,
    )
    policy = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST, specialist="luna",
                                          fallbacks=("glm-vision",)))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_ROUTED
    assert decision.selected == "glm-vision"
    assert decision.classification == CLASSIFICATION_FALLBACK
    assert decision.evidence["luna"]["health_ok"] is False
    assert "unhealthy" in decision.evidence["luna"]["skip_reason"]
    assert health.calls == ["luna", "glm-vision"]  # sampled once per candidate, in order


# ---------------------------------------------------------------------------
# 6. Owner preference wins among otherwise eligible candidates
# ---------------------------------------------------------------------------

def test_owner_preference_wins_among_eligible_candidates():
    registry = CapabilityRegistry([
        _llm("anthropic-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
        _llm("openai-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
    ])
    # owner preference "luna" must beat alphabetically-earlier registry peers
    policy = _auto_policy(CapabilityRoute(mode=LANE_AUTO, specialist="luna"))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_ROUTED
    assert decision.selected == "luna"
    assert decision.classification == CLASSIFICATION_EXPLICIT
    assert "owner-configured preferred specialist" in decision.reason


# ---------------------------------------------------------------------------
# 7/8. Local-first ordering strictly floor-gated
# ---------------------------------------------------------------------------

def test_local_preferred_only_when_quality_floor_met():
    registry = CapabilityRegistry([
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}, label="best"),
        _llm("local-vision", {"vision_understanding": EvidenceClass.EVIDENCED},
             local=True, label="good"),
    ])
    policy = _auto_policy(
        CapabilityRoute(mode=LANE_AUTO, quality_floor="good"),
        quality_order=("basic", "good", "best"),
    )
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_ROUTED
    assert decision.selected == "local-vision"
    assert decision.classification == CLASSIFICATION_AUTO_LOCAL


def test_below_floor_local_falls_through_deterministically():
    registry = CapabilityRegistry([
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}, label="best"),
        _llm("local-vision", {"vision_understanding": EvidenceClass.EVIDENCED},
             local=True, label="basic"),
    ])
    policy = _auto_policy(
        CapabilityRoute(mode=LANE_AUTO, quality_floor="good"),
        quality_order=("basic", "good", "best"),
    )
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_ROUTED
    assert decision.selected == "luna"  # below-floor local demoted, cloud admitted
    assert decision.classification == CLASSIFICATION_AUTO_CLOUD
    assert decision.evidence["local-vision"]["floor_met"] is False
    assert "quality floor not satisfied" in decision.evidence["local-vision"]["skip_reason"]
    # unlabeled local also never satisfies a set floor (fail closed)
    registry2 = CapabilityRegistry([
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}, label="best"),
        _llm("local-vision", {"vision_understanding": EvidenceClass.EVIDENCED}, local=True),
    ])
    decision2 = resolve_capability(registry2, policy, Capability.VISION_UNDERSTANDING)
    assert decision2.selected == "luna"
    assert decision2.evidence["local-vision"]["floor_met"] is False


# ---------------------------------------------------------------------------
# 9. Explicit fallback when preferred unavailable (order honored)
# ---------------------------------------------------------------------------

def test_explicit_fallback_when_preferred_unavailable():
    health = _health_map(unhealthy=("luna",))
    registry = CapabilityRegistry(
        [
            _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("gemini-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("anthropic-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
        ],
        health_lookup=health,
    )
    policy = _auto_policy(CapabilityRoute(
        mode=LANE_SPECIALIST,
        specialist="luna",
        fallbacks=("gemini-cloud", "anthropic-cloud"),
    ))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.selected == "gemini-cloud"  # first permitted fallback, stored order
    assert decision.classification == CLASSIFICATION_FALLBACK

    health2 = _health_map(unhealthy=("luna", "gemini-cloud"))
    registry2 = CapabilityRegistry(
        [
            _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("gemini-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("anthropic-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
        ],
        health_lookup=health2,
    )
    decision2 = resolve_capability(registry2, policy, Capability.VISION_UNDERSTANDING)
    assert decision2.selected == "anthropic-cloud"  # second permitted fallback


# ---------------------------------------------------------------------------
# 10. Fallback never uses an owner-disabled provider
# ---------------------------------------------------------------------------

def test_fallback_never_uses_owner_disabled_provider():
    health = _health_map(unhealthy=("luna",))
    registry = CapabilityRegistry(
        [
            _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("rogue-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("anthropic-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
        ],
        health_lookup=health,
    )
    # "rogue-cloud" is a permitted fallback NAME but owner-disabled: it must
    # never be selected even when it is the first surviving candidate.
    policy = _auto_policy(
        CapabilityRoute(mode=LANE_SPECIALIST, specialist="luna",
                        fallbacks=("rogue-cloud", "anthropic-cloud")),
        disabled=("rogue-cloud",),
    )
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.selected == "anthropic-cloud"
    assert decision.evidence["rogue-cloud"]["skip_reason"] == "owner-disabled provider"


# ---------------------------------------------------------------------------
# 11. Determinism
# ---------------------------------------------------------------------------

def test_identical_inputs_produce_identical_decisions():
    records = [
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
        _llm("local-vision", {"vision_understanding": EvidenceClass.EVIDENCED},
             local=True, label="good"),
        _llm("unknown-vision", {"vision_understanding": EvidenceClass.UNKNOWN}),
    ]
    shuffled = list(records)
    random.Random(42).shuffle(shuffled)
    assert shuffled != records  # input order genuinely differs

    route = CapabilityRoute(mode=LANE_AUTO, quality_floor="good")
    policy = _auto_policy(route, quality_order=("basic", "good", "best"))
    policy_shuffled = RoutingPolicy(
        routes={"vision_understanding": CapabilityRoute(
            mode=LANE_AUTO, quality_floor="good")},
        quality_order=("basic", "good", "best"),
    )

    d1 = resolve_capability(CapabilityRegistry(records), policy, Capability.VISION_UNDERSTANDING)
    d2 = resolve_capability(CapabilityRegistry(records), policy, Capability.VISION_UNDERSTANDING)
    d3 = resolve_capability(CapabilityRegistry(shuffled), policy_shuffled,
                            Capability.VISION_UNDERSTANDING)
    d4 = resolve_capability(CapabilityRegistry(shuffled), policy_shuffled,
                            "vision_understanding")  # canonical string == enum
    assert d1 == d2 == d3 == d4
    assert d1.selected == "local-vision"  # floor-satisfied local first, regardless of input order


# ---------------------------------------------------------------------------
# 12. Primary backend structural exclusion
# ---------------------------------------------------------------------------

def test_primary_backend_never_selected_or_mutated():
    registry = CapabilityRegistry([
        _llm("glm", {"vision_understanding": EvidenceClass.EVIDENCED}),
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
    ])
    # primary named as the explicit preference AND as a fallback
    policy = _auto_policy(CapabilityRoute(
        mode=LANE_SPECIALIST, specialist="glm", fallbacks=("glm", "luna")))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING,
                                  primary_backend="glm")
    assert decision.selected == "luna"
    assert decision.excluded_primary_backend == "glm"
    assert decision.evidence["glm"]["skip_reason"] == (
        "primary conversational backend is outside the specialist candidate space"
    )
    # the decision type offers no mutation surface at all
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.selected = "glm"
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.excluded_primary_backend = None
    # auto mode: even if the primary advertises the capability in the
    # registry, it is skipped with the structural reason
    auto_policy = _auto_policy(CapabilityRoute(mode=LANE_AUTO))
    auto_decision = resolve_capability(registry, auto_policy, Capability.VISION_UNDERSTANDING,
                                       primary_backend="glm")
    assert auto_decision.selected == "luna"
    assert auto_decision.evidence["glm"]["skip_reason"].startswith("primary conversational")


# ---------------------------------------------------------------------------
# 13. Provenance explains selected and skipped candidates
# ---------------------------------------------------------------------------

def test_provenance_explains_selected_and_skipped():
    health = _health_map(unhealthy=("luna",))
    registry = CapabilityRegistry(
        [
            _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}, label="best"),
            _llm("rogue-cloud", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("local-vision", {"vision_understanding": EvidenceClass.EVIDENCED},
                 local=True, label="basic"),
            _llm("anthropic-cloud", {"vision_understanding": EvidenceClass.EVIDENCED},
                 label="best"),
        ],
        health_lookup=health,
    )
    # preferred head unhealthy, permitted fallback owner-disabled, local
    # below floor, cloud admitted: every consulted candidate is explained.
    policy = RoutingPolicy(
        routes={"vision_understanding": CapabilityRoute(
            mode=LANE_AUTO, specialist="luna", fallbacks=("rogue-cloud",),
            quality_floor="good")},
        disabled_providers=("rogue-cloud",),
        quality_order=("basic", "good", "best"),
    )
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING,
                                  primary_backend="glm")
    assert decision.outcome == OUTCOME_ROUTED
    assert decision.selected == "anthropic-cloud"
    assert decision.classification == CLASSIFICATION_AUTO_CLOUD
    ledger = decision.evidence
    assert ledger["luna"]["skip_reason"].startswith("unhealthy")
    assert ledger["rogue-cloud"]["skip_reason"] == "owner-disabled provider"
    assert ledger["local-vision"]["skip_reason"] == (
        "quality floor not satisfied by owner-declared quality label"
    )
    assert "local specialist meeting the configured quality floor" not in decision.reason
    assert "cloud specialist" in decision.reason
    assert ledger["anthropic-cloud"]["admitted"] is True
    # requested capability + exclusion fact are recorded
    assert decision.capability == "vision_understanding"
    assert decision.excluded_primary_backend == "glm"


# ---------------------------------------------------------------------------
# 14. Provenance contains no secrets
# ---------------------------------------------------------------------------

def test_provenance_contains_no_secrets():
    health = _health_map(unhealthy=("luna",))
    registry = CapabilityRegistry(
        [
            _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
            _llm("glm-vision", {"vision_understanding": EvidenceClass.EVIDENCED}),
        ],
        health_lookup=health,
    )
    policy = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST, specialist="luna",
                                          fallbacks=("glm-vision",)))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    rendered = repr(decision) + repr(decision.evidence) + decision.reason
    assert FAKE_SECRET not in rendered  # redaction applied to recorded free text
    assert "[REDACTED]" in decision.evidence["luna"]["health_detail"]
    assert "[REDACTED]" in decision.evidence["luna"]["skip_reason"]
    # structural: the public API accepts no credential-bearing parameters
    # and the decision record has no credential-bearing fields
    forbidden = ("key", "secret", "token", "credential", "password", "auth")
    params = set(inspect.signature(resolve_capability).parameters)
    assert not any(p in params for p in ("api_key", "secret", "token", "credentials"))
    assert not any(any(w in p.lower() for w in forbidden) for p in params)
    fields = {f.name for f in dataclasses.fields(RoutingDecision)}
    assert not any(any(w in name.lower() for w in forbidden) for name in fields)


# ---------------------------------------------------------------------------
# 15. Defaults preserve existing behavior
# ---------------------------------------------------------------------------

def test_defaults_preserve_existing_behavior():
    # no routes configured -> every canonical lane default-disabled
    empty_policy = RoutingPolicy(routes=parse_routes({})[0])
    registry = CapabilityRegistry([
        _llm("luna", {c.value: EvidenceClass.EVIDENCED for c in CANONICAL_CAPABILITIES}),
    ])
    for capability in CANONICAL_CAPABILITIES:
        decision = resolve_capability(registry, empty_policy, capability)
        assert decision.outcome == OUTCOME_DISABLED
        assert decision.selected is None
        assert decision.evidence == {}
    # parse_routes fail-closed defaults
    routes, warnings = parse_routes(None)
    assert routes == {} and warnings == []
    # real install defaults: both prefs keys exist and are empty
    assert config.MULTIMODAL_ROUTES == {}
    assert config.MULTIMODAL_DISABLED_PROVIDERS == []


# ---------------------------------------------------------------------------
# 16. Import purity / no network / no wasted lookups
# ---------------------------------------------------------------------------

def test_import_purity_no_network_io():
    with open(MODULE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {"__future__", "dataclasses", "enum", "typing", "core.redaction"}
    assert imported <= allowed, f"unexpected imports: {sorted(imported - allowed)}"
    forbidden = {"requests", "urllib", "http", "socket", "httpx", "aiohttp",
                 "ssl", "subprocess", "pathlib", "sqlite3", "PySide6"}
    assert not (imported & forbidden)
    # nothing executes at import time: the only allowed bare top-level
    # expression is the module docstring (a constant expression)
    for node in tree.body:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant), (
                "module-level side-effect statement found"
            )


def test_disabled_lane_performs_no_lookups():
    health = _health_map()
    supports = []

    def support_lookup(name, capability):
        supports.append((name, capability))
        return EvidenceClass.EVIDENCED

    registry = CapabilityRegistry(
        [_llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED})],
        health_lookup=health,
        support_lookup=support_lookup,
    )
    policy = _auto_policy(CapabilityRoute(mode=LANE_DISABLED))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_DISABLED
    assert decision.reason == "owner-disabled lane"
    assert health.calls == []
    assert supports == []


# ---------------------------------------------------------------------------
# 17. Durability boundary: ephemeral by default, in the public contract
# ---------------------------------------------------------------------------

def test_durability_ephemeral_contract():
    import core.capability_router as module

    source = inspect.getsource(module)
    # the law is stated in the module contract, not merely implied
    assert "EPHEMERAL BY DEFAULT" in source
    assert "deliberately" in source
    # structural: the module cannot persist anything — no persistence
    # machinery is imported and no file-writing primitive exists
    with open(MODULE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    persistence_surfaces = {"core.persistence", "core.memory_backup", "core.db",
                            "core.chat_history", "core.context", "json", "pickle"}
    assert not (imported & persistence_surfaces)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open", "module must not perform file I/O"
    # no persistence-flavored public names
    for name in module.__all__:
        assert not any(word in name.lower() for word in ("save", "store", "persist", "write"))


# ---------------------------------------------------------------------------
# 18. Unknown capability / provider fail safely
# ---------------------------------------------------------------------------

def test_unknown_capability_fails_truthfully():
    registry = CapabilityRegistry([_llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED})])
    policy = _auto_policy(CapabilityRoute(mode=LANE_AUTO))
    for garbage in ("holography", "VISION_UNDERSTANDING", "", None, 42):
        decision = resolve_capability(registry, policy, garbage)
        assert decision.outcome == OUTCOME_INVALID_CAPABILITY
        assert decision.selected is None
        assert "not in the canonical vocabulary" in decision.reason


def test_unknown_provider_fails_safely():
    registry = CapabilityRegistry([_llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED})])
    policy = _auto_policy(CapabilityRoute(
        mode=LANE_SPECIALIST, specialist="ghost", fallbacks=("phantom", "luna")))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_ROUTED  # falls through to the real candidate
    assert decision.selected == "luna"
    assert decision.evidence["ghost"]["skip_reason"] == "unknown specialist (not present in registry)"
    assert decision.evidence["phantom"]["skip_reason"] == "unknown specialist (not present in registry)"
    # all-unknown candidate space -> truthful no_backend, never a crash
    only_ghosts = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST, specialist="ghost",
                                               fallbacks=("phantom",)))
    decision2 = resolve_capability(registry, only_ghosts, Capability.VISION_UNDERSTANDING)
    assert decision2.outcome == OUTCOME_NO_BACKEND


# ---------------------------------------------------------------------------
# Extra: evidence honesty — UNKNOWN is representable and never collapsed
# ---------------------------------------------------------------------------

def test_unknown_evidence_representable_never_collapsed():
    # The MB-34 live shape: a backend whose capability truth cannot be
    # determined. The registry must represent that truthfully.
    registry = CapabilityRegistry([
        _llm("openrouter-live", {"vision_understanding": EvidenceClass.UNKNOWN}),
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}),
    ])
    policy = _auto_policy(CapabilityRoute(mode=LANE_SPECIALIST,
                                          specialist="openrouter-live"))
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == OUTCOME_NO_BACKEND
    assert decision.evidence["openrouter-live"]["evidence"] == "unknown"  # NOT "unsupported"
    assert "evidence unknown" in decision.evidence["openrouter-live"]["skip_reason"]
    # support_lookup returning junk is treated as UNKNOWN, never UNSUPPORTED
    def junk_lookup(name, capability):
        return "totally-not-an-evidence-class"

    junk_registry = CapabilityRegistry(
        [_llm("openrouter-live", {"vision_understanding": EvidenceClass.UNKNOWN})],
        support_lookup=junk_lookup,
    )
    junk_decision = resolve_capability(junk_registry, policy, Capability.VISION_UNDERSTANDING)
    assert junk_decision.evidence["openrouter-live"]["evidence"] == "invalid"
    assert junk_decision.outcome == OUTCOME_NO_BACKEND


# ---------------------------------------------------------------------------
# Extra: the floor gates explicit specialists and fallbacks too
# ---------------------------------------------------------------------------

def test_floor_gates_explicit_specialist_too():
    registry = CapabilityRegistry([
        _llm("local-vision", {"vision_understanding": EvidenceClass.EVIDENCED},
             local=True, label="basic"),
        _llm("luna", {"vision_understanding": EvidenceClass.EVIDENCED}, label="best"),
    ])
    policy = _auto_policy(CapabilityRoute(
        mode=LANE_SPECIALIST, specialist="local-vision", quality_floor="good"),
        quality_order=("basic", "good", "best"),
    )
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    # owner explicitly named a below-floor specialist: fail closed and
    # explain, never silently downgrade to another specialist
    assert decision.outcome == OUTCOME_NO_BACKEND
    assert decision.evidence["local-vision"]["skip_reason"] == (
        "quality floor not satisfied by owner-declared quality label"
    )


# ---------------------------------------------------------------------------
# Extra: parse_routes fail-closed discipline (hand-edited prefs)
# ---------------------------------------------------------------------------

def test_parse_routes_fail_closed():
    routes, warnings = parse_routes({
        "vision_understanding": {"mode": "auto"},
        "holography": {"mode": "auto"},                      # unknown capability -> dropped
        "image_generation": "not-a-mapping",                 # malformed -> dropped
        "audio_understanding": {"quality_floor": "x"},       # missing mode -> dropped
    })
    assert set(routes) == {"vision_understanding"}
    assert len(warnings) == 3
    assert all("dropped" in w for w in warnings)
    # non-mapping input -> everything dropped, nothing raised
    routes2, warnings2 = parse_routes("garbage")
    assert routes2 == {} and len(warnings2) == 1
    # fail-closed end-to-end: dropped lanes resolve disabled
    policy = RoutingPolicy(routes=routes)
    registry = CapabilityRegistry([_llm("luna", {
        "vision_understanding": EvidenceClass.EVIDENCED,
        "image_generation": EvidenceClass.EVIDENCED,
    })])
    assert resolve_capability(registry, policy, "vision_understanding").outcome == OUTCOME_ROUTED
    assert resolve_capability(registry, policy, "image_generation").outcome == OUTCOME_DISABLED
