"""
core/capability_router.py -- MULTIMODAL-M1-CAPABILITY-REGISTRY-01

Provider-neutral capability registry + deterministic specialist selection
for the Multimodal Capability Router (MULTIMODAL-CAPABILITY-ROUTER-01, M1).

This module is pure infrastructure: Qt-free, stdlib-only (plus the
established ``core.redaction`` secret-shape primitive), zero I/O, zero
network, zero threads, and nothing executes at import time. It establishes
the architectural layer that later routing slices (M2 vision, M4 image
generation, M5 audio understanding, M6 speech presentation, M7 video) will
consume. It performs no media work itself and never will in this slice.

Design laws encoded here (canonical Multimodal design, technical note
2026-09-12, with Other-Claude's amendments as section 16):

- THE SESSION NEVER CARRIES WHAT IT CANNOT NATIVELY DIGEST.
  The registry describes specialists; it never touches conversation
  history, never handles media payloads, and exposes no code path that
  could. No function in this module accepts or returns media.

- SPECIALISTS OBSERVE; LOAD-BEARING FACTS ARE PROMOTED DELIBERATELY.
  Specialist results are EPHEMERAL BY DEFAULT (durability invariant 16.1).
  Nothing here can promote anything into durable chat text, the memory
  palace, My Human, project state, or KB material: promotion is always a
  deliberate act by the primary agent in a later slice. The module
  structurally cannot persist anything -- it performs no I/O and imports
  no persistence machinery.

- AUTO MUST BE DETERMINISTIC, EXPLAINABLE, AND OWNER-CONTROLLED
  (Amendment C, canonical design 16.3). resolve_capability() is a pure
  function: identical inputs produce identical decisions, candidate
  ordering is explicit (never dict-iteration order), health is sampled
  exactly once per candidate per decision, and every decision records why
  each candidate was selected or skipped. Auto NEVER silently switches the
  primary conversational backend: the primary backend is structurally
  outside the candidate space and is recorded as an exclusion fact.

- DECLARED SPECIALIST CAPABILITY IS NOT LEGACY TRANSPORT-GUARD STATE.
  (MB-34 boundary.) Capability claims recorded here are registry/owner
  declarations about specialists. This module never imports a backend
  class, never reads ``_vision_tool_cache`` or any primary-backend
  transport guard, and never contacts the live primary conversation
  backend. Unknown capability evidence is representable (UNKNOWN) and is
  never silently collapsed into UNSUPPORTED or EVIDENCED.

Quality floors (honest V1 scope): no quality measurement exists anywhere
in the codebase today. A route's ``quality_floor`` is therefore compared
ONLY against owner-declared per-specialist quality labels, ordered by the
owner-declared ``RoutingPolicy.quality_order`` (worst to best). A
specialist with no label, or a label absent from the ordering, never
satisfies a set floor (fail closed). ``None`` floor = unconstrained. Real
quality measurement is explicitly future work.

Provenance and secrets: free text recorded in a RoutingDecision (health
details, skip reasons) passes through the established
``core.redaction.redact_secret_shapes`` primitive -- the same protection
Flight Recorder and the continuity compiler use. The public API accepts no
credential parameters and the decision record has no credential fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional, Sequence, Tuple

from core.redaction import redact_secret_shapes

__all__ = [
    "Capability",
    "EvidenceClass",
    "SpecialistRecord",
    "CapabilityRoute",
    "RoutingPolicy",
    "CapabilityRegistry",
    "RoutingDecision",
    "resolve_capability_target",
    "CANONICAL_CAPABILITIES",
    "LANE_DISABLED",
    "LANE_AUTO",
    "LANE_SPECIALIST",
    "OUTCOME_ROUTED",
    "OUTCOME_DISABLED",
    "OUTCOME_NO_BACKEND",
    "OUTCOME_INVALID_CAPABILITY",
    "CLASSIFICATION_EXPLICIT",
    "CLASSIFICATION_FALLBACK",
    "CLASSIFICATION_AUTO_LOCAL",
    "CLASSIFICATION_AUTO_CLOUD",
    "parse_routes",
    "resolve_capability",
]


class Capability(str, Enum):
    """Frozen canonical capability vocabulary (M1 scope)."""

    VISION_UNDERSTANDING = "vision_understanding"
    IMAGE_GENERATION = "image_generation"
    AUDIO_UNDERSTANDING = "audio_understanding"
    SPEECH_SYNTHESIS = "speech_synthesis"
    VIDEO_UNDERSTANDING = "video_understanding"
    VIDEO_GENERATION = "video_generation"


CANONICAL_CAPABILITIES = frozenset(Capability)

_LANE_MODES = ("disabled", "auto", "specialist")
LANE_DISABLED = "disabled"
LANE_AUTO = "auto"
LANE_SPECIALIST = "specialist"

OUTCOME_ROUTED = "routed"
OUTCOME_DISABLED = "disabled"
OUTCOME_NO_BACKEND = "no_backend"
OUTCOME_INVALID_CAPABILITY = "invalid_capability"

CLASSIFICATION_EXPLICIT = "explicit"
CLASSIFICATION_FALLBACK = "fallback"
CLASSIFICATION_AUTO_LOCAL = "auto_local"
CLASSIFICATION_AUTO_CLOUD = "auto_cloud"


class EvidenceClass(str, Enum):
    """Evidence class for a (specialist, capability) claim.

    Evidence-class discipline mirrors the backend capability contract
    (``BaseLLMBackend.supports_vision`` and friends): fail-safe defaults,
    no inferred positives, and no collapsing of UNKNOWN into UNSUPPORTED.
    """

    EVIDENCED = "evidenced"      # live-verified or authoritatively documented
    STRUCTURAL = "structural"    # structurally accepted, not contract-advertised
    UNKNOWN = "unknown"          # cannot be determined (e.g. undiscovered live state)
    UNSUPPORTED = "unsupported"  # declared absent


def _normalize_capability(value) -> Optional[Capability]:
    """Canonical capability lookup. Exact vocabulary names only; anything
    else returns None (the caller fails truthfully -- never guesses)."""
    if isinstance(value, Capability):
        return value
    if isinstance(value, str):
        try:
            return Capability(value)
        except ValueError:
            return None
    return None


def _capability_key(value) -> str:
    cap = _normalize_capability(value)
    if cap is None:
        raise ValueError(f"not a canonical capability: {value!r}")
    return cap.value


def _as_evidence_class(value) -> Optional[EvidenceClass]:
    """Defensive runtime normalization of lookup results. Junk -> None,
    which callers must treat as UNKNOWN, never as UNSUPPORTED."""
    if isinstance(value, EvidenceClass):
        return value
    if isinstance(value, str):
        try:
            return EvidenceClass(value)
        except ValueError:
            return None
    return None


def _normalize_capability_map(capabilities) -> dict:
    """Construction-time validation of a declared capability map (loud:
    authoring errors raise). Keys must be canonical capability names,
    values must be valid EvidenceClass values."""
    normalized = {}
    for key, value in dict(capabilities or {}).items():
        cap = _normalize_capability(key)
        if cap is None:
            raise ValueError(f"SpecialistRecord capability key is not canonical: {key!r}")
        ev = _as_evidence_class(value)
        if ev is None:
            raise ValueError(f"SpecialistRecord[{cap.value}] has invalid evidence class: {value!r}")
        normalized[cap.value] = ev
    return normalized


@dataclass(frozen=True)
class SpecialistRecord:
    """A specialist/provider/model as the registry sees it.

    name:
        Stable provider/backend identity. For LLM backends this is a
        ``core.backends.loader.BACKENDS`` key (validated by the CALLER at
        resolve/binding time -- this module stays import-pure and never
        imports the loader). Subsystem specialists use namespaced ids such
        as ``"tts:chatterbox"`` or ``"stt:whisper"``.
    kind:
        Coarse execution family: ``"llm_backend" | "tts" | "stt" |
        "external"`` (future image/video workers).
    local:
        Local vs cloud execution class. This is a placement fact used for
        local-first ordering, NOT a quality claim.
    capabilities:
        Declared capability -> EvidenceClass map. This is the specialist's
        DECLARED capability -- an architectural concept distinct from any
        legacy primary-backend transport-guard state (MB-34 boundary).
    enabled:
        Provider-level switch. Owner-level disablement additionally comes
        via ``RoutingPolicy.disabled_providers``; both paths feed the same
        never-select rule.
    quality_label:
        Owner-declared quality label (V1: declared, never measured). Only
        meaningful against a route's ``quality_floor`` via the policy's
        ``quality_order``.
    """

    name: str
    kind: str
    local: bool
    capabilities: Mapping[str, EvidenceClass]
    enabled: bool = True
    quality_label: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("SpecialistRecord.name must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("SpecialistRecord.kind must be a non-empty string")
        object.__setattr__(self, "capabilities", _normalize_capability_map(self.capabilities))


@dataclass(frozen=True)
class CapabilityRoute:
    """One row of the owner's routing table, for one capability.

    mode:
        ``disabled`` -- lane off; no lookups, no candidates.
        ``auto`` -- deterministic Auto policy (Amendment C).
        ``specialist`` -- owner names the specialist explicitly.
    specialist:
        Explicit/preferred specialist (required for ``specialist`` mode,
        optional preferred head for ``auto``).
    fallbacks:
        Owner-permitted, ordered fallback list. Never implicitly expanded.
    quality_floor:
        Owner-declared floor label; ``None`` = unconstrained. Compared
        only against owner-declared specialist labels via
        ``RoutingPolicy.quality_order``.
    model:
        MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 -- optional capability-
        specific model override, meaningful ONLY for the specialist named
        in ``specialist`` above (never a fallback, never an Auto pick --
        see ``resolve_capability_target()`` below for the exact admission
        rule). ``None`` (the default, and what every route stored before
        this field existed still parses as) means "no override": the
        admitted specialist's own provider-global/default model applies,
        exactly as before this field was added. This module never reads,
        constructs, or mutates a provider/backend to apply the override --
        it only carries the owner's declared string; a caller (e.g.
        core.vision_lane, core.image_generation_service) is responsible
        for threading it into a per-invocation call, never into shared
        provider state.
    """

    mode: str
    specialist: Optional[str] = None
    fallbacks: Tuple[str, ...] = ()
    quality_floor: Optional[str] = None
    model: Optional[str] = None

    def __post_init__(self):
        if self.mode not in _LANE_MODES:
            raise ValueError(f"CapabilityRoute.mode must be one of {_LANE_MODES}, got {self.mode!r}")
        object.__setattr__(self, "fallbacks", tuple(self.fallbacks or ()))
        if self.mode == LANE_SPECIALIST and not (isinstance(self.specialist, str) and self.specialist):
            raise ValueError("specialist mode requires a non-empty specialist name")
        for fb in self.fallbacks:
            if not isinstance(fb, str) or not fb:
                raise ValueError(f"fallback entries must be non-empty strings, got {fb!r}")
        if self.quality_floor is not None and not isinstance(self.quality_floor, str):
            raise ValueError("quality_floor must be a string or None")
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model must be a string or None")


def _coerce_route(value) -> Tuple[Optional[CapabilityRoute], Optional[str]]:
    """Accept a CapabilityRoute or a raw mapping (config path). Returns
    (route, None) or (None, error). Unknown/missing mode fails closed."""
    if isinstance(value, CapabilityRoute):
        return value, None
    if isinstance(value, Mapping):
        mode = value.get("mode")
        if mode not in _LANE_MODES:
            return None, f"missing or invalid mode {mode!r} (expected one of {_LANE_MODES})"
        try:
            return (
                CapabilityRoute(
                    mode=mode,
                    specialist=value.get("specialist"),
                    fallbacks=value.get("fallbacks") or (),
                    quality_floor=value.get("quality_floor"),
                    model=value.get("model"),
                ),
                None,
            )
        except (TypeError, ValueError) as exc:
            return None, str(exc)
    return None, f"expected CapabilityRoute or mapping, got {type(value).__name__}"


@dataclass(frozen=True)
class RoutingPolicy:
    """Owner policy inputs for deterministic selection.

    routes:
        Capability name -> CapabilityRoute. Unconfigured capabilities are
        DEFAULT-DISABLED (existing behavior preserved; fail closed).
    disabled_providers:
        Owner-disabled provider names. A disabled provider is treated as
        non-existent for capability routing -- including as an explicit
        preference or permitted fallback. This does NOT affect primary
        backend selection in any way.
    quality_order:
        Owner-declared quality labels, worst to best. Defines the only
        meaning ``quality_floor`` comparisons have in V1.
    """

    routes: Mapping[str, CapabilityRoute]
    disabled_providers: Sequence[str] = ()
    quality_order: Sequence[str] = ()

    def __post_init__(self):
        object.__setattr__(self, "disabled_providers", tuple(self.disabled_providers or ()))
        object.__setattr__(self, "quality_order", tuple(self.quality_order or ()))
        normalized = {}
        if self.routes:
            for key, value in dict(self.routes).items():
                cap = _normalize_capability(key)
                if cap is None:
                    raise ValueError(f"RoutingPolicy.routes key is not a canonical capability: {key!r}")
                route, err = _coerce_route(value)
                if err is not None:
                    raise ValueError(f"RoutingPolicy.routes[{key!r}]: {err}")
                normalized[cap.value] = route
        object.__setattr__(self, "routes", normalized)


class CapabilityRegistry:
    """Static specialist knowledge + injectable lookups. Holds NO backend
    objects, performs NO I/O, and never touches the primary backend.

    health_lookup:
        Optional callable ``name -> (bool, str)`` matching the established
        backend health contract. When absent, specialists are treated
        healthy by default (no information != unhealthy) and the decision
        records that fact. Whatever callable is supplied must be a
        diagnostics-only primitive: no network call merely to decide,
        unless a caller deliberately wires an already-established safe
        health primitive.
    support_lookup:
        Optional callable ``(name, capability) -> EvidenceClass`` letting a
        later slice supply live discovery evidence (M2's fresh-instance
        pattern). Junk return values are treated as UNKNOWN, never as
        UNSUPPORTED. When absent, declared record capabilities are used.
    """

    def __init__(
        self,
        specialists: Sequence[SpecialistRecord],
        health_lookup: Optional[Callable[[str], Tuple[bool, str]]] = None,
        support_lookup: Optional[Callable[[str, Capability], EvidenceClass]] = None,
    ):
        records = {}
        for record in specialists:
            if record.name in records:
                raise ValueError(f"duplicate specialist name: {record.name!r}")
            records[record.name] = record
        self._records = records
        self._health_lookup = health_lookup
        self._support_lookup = support_lookup

    def __contains__(self, name) -> bool:
        return name in self._records

    def get(self, name) -> Optional[SpecialistRecord]:
        return self._records.get(name)

    def names(self) -> Tuple[str, ...]:
        """Deterministic name ordering (sorted), never dict order."""
        return tuple(sorted(self._records))

    def all_records(self) -> Tuple[SpecialistRecord, ...]:
        return tuple(self._records[name] for name in self.names())

    def health(self, name) -> Tuple[bool, str]:
        if self._health_lookup is None:
            return True, "no health lookup configured (treated healthy by default)"
        return self._health_lookup(name)

    def evidence(self, name, capability) -> Optional[EvidenceClass]:
        record = self._records.get(name)
        if record is None:
            return None
        if self._support_lookup is not None:
            return _as_evidence_class(self._support_lookup(name, capability))
        return record.capabilities.get(_capability_key(capability), EvidenceClass.UNSUPPORTED)


@dataclass(frozen=True)
class RoutingDecision:
    """The truthful, inspectable record of one selection decision.

    provenance fields answer: what capability was requested, what
    specialist was chosen (if any), why, whether it was the owner's
    explicit preference / a permitted fallback / an auto pick (local or
    cloud), and why every higher-priority candidate was skipped.

    There is NO field that could carry or mutate a primary conversation
    backend: ``excluded_primary_backend`` is a recorded exclusion fact and
    no code path returns or alters the primary. Free text is passed
    through ``redact_secret_shapes``; the API accepts no credentials.
    """

    capability: str
    outcome: str
    selected: Optional[str] = None
    classification: Optional[str] = None
    reason: str = ""
    evidence: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    excluded_primary_backend: Optional[str] = None


def _floor_met(record: SpecialistRecord, route: CapabilityRoute, policy: RoutingPolicy) -> bool:
    """Owner-declared floor semantics (honest V1). Unlabeled specialists
    and labels absent from the owner ordering never satisfy a set floor."""
    if route.quality_floor is None:
        return True
    order = tuple(policy.quality_order)
    label = record.quality_label
    if label is None or label not in order:
        return False
    if route.quality_floor not in order:
        return False
    return order.index(label) >= order.index(route.quality_floor)


def _auto_tier(record: SpecialistRecord, evidence: EvidenceClass, route: CapabilityRoute,
               policy: RoutingPolicy) -> int:
    """Registry-derived Auto ordering tiers (Amendment C):

    0: admissible evidence + local + floor satisfied   (local-first)
    1: admissible evidence + local but floor unmet     (walked, then skipped)
    2: admissible evidence + cloud
    3: inadmissible evidence (UNSUPPORTED / UNKNOWN / undeterminable)
    """
    if evidence not in (EvidenceClass.EVIDENCED, EvidenceClass.STRUCTURAL):
        return 3
    if not record.local:
        return 2
    return 0 if _floor_met(record, route, policy) else 1


def _admission_reason(classification: str, evidence: EvidenceClass,
                      floor_gated: bool, floor_met: bool) -> str:
    base = {
        CLASSIFICATION_EXPLICIT: "owner-configured preferred specialist",
        CLASSIFICATION_FALLBACK: "owner-permitted fallback",
        CLASSIFICATION_AUTO_LOCAL: "local specialist meeting the configured quality floor",
        CLASSIFICATION_AUTO_CLOUD: "cloud specialist (no floor-satisfied local candidate ahead)",
    }[classification]
    detail = f"admitted as {classification}: {base}; capability evidence: {evidence.value}"
    if floor_gated:
        detail += "; quality floor satisfied" if floor_met else ""
    return detail


def resolve_capability(
    registry: CapabilityRegistry,
    policy: RoutingPolicy,
    capability,
    primary_backend: Optional[str] = None,
) -> RoutingDecision:
    """Deterministic specialist selection. PURE: no I/O, no network, no
    mutation of registry or policy; identical inputs -> identical decision.

    Decision order (first match wins; every skip is recorded):
      1. unknown capability -> invalid_capability (never improvised)
      2. unconfigured or owner-disabled lane -> disabled (no lookups)
      3. candidates: [explicit preference] + [owner-permitted fallbacks in
         stored order] + [registry-derived candidates tiered: floor-met
         local first, then floor-unmet local, then cloud, then inadmissible
         evidence; name-ascending within tier]
      4. walk: skip owner-disabled, unknown, disabled, declared-UNSUPPORTED,
         evidence-UNKNOWN, below-floor, then unhealthy candidates; admit
         the first survivor. The ledger records every candidate CONSULTED
         before admission — candidates behind the winner are never probed.
      5. exhausted -> no_backend with a per-candidate rejection ledger

    ``primary_backend`` is recorded as an exclusion fact only: a candidate
    matching it is skipped with a structural reason, and no code path can
    select or mutate the primary conversational backend.
    """
    cap = _normalize_capability(capability)
    if cap is None:
        return RoutingDecision(
            capability=str(capability),
            outcome=OUTCOME_INVALID_CAPABILITY,
            reason="requested capability is not in the canonical vocabulary; refusing to improvise",
            excluded_primary_backend=primary_backend,
        )

    route = policy.routes.get(cap.value)
    if route is None:
        return RoutingDecision(
            capability=cap.value,
            outcome=OUTCOME_DISABLED,
            reason="no route configured for capability; unconfigured lanes are default-disabled (existing behavior preserved)",
            excluded_primary_backend=primary_backend,
        )
    if route.mode == LANE_DISABLED:
        # Owner-disabled lane: no health or support lookups are performed.
        return RoutingDecision(
            capability=cap.value,
            outcome=OUTCOME_DISABLED,
            reason="owner-disabled lane",
            excluded_primary_backend=primary_backend,
        )

    disabled = frozenset(policy.disabled_providers)
    candidates = []
    if route.specialist:
        candidates.append((route.specialist, CLASSIFICATION_EXPLICIT))
    for fallback in route.fallbacks:
        candidates.append((fallback, CLASSIFICATION_FALLBACK))
    if route.mode == LANE_AUTO:
        tiered = []
        for record in registry.all_records():
            evidence = registry.evidence(record.name, cap)
            tier = _auto_tier(record, evidence, route, policy)
            tiered.append((tier, record.name))
        tiered.sort()
        for _, name in tiered:
            tail_record = registry.get(name)
            candidates.append((
                name,
                CLASSIFICATION_AUTO_LOCAL if tail_record.local else CLASSIFICATION_AUTO_CLOUD,
            ))

    ledger = {}
    for name, classification in candidates:
        entry = {"classification": classification, "admitted": False}
        if primary_backend is not None and name == primary_backend:
            entry["skip_reason"] = (
                "primary conversational backend is outside the specialist candidate space"
            )
            ledger[name] = entry
            continue
        if name in disabled:
            entry["skip_reason"] = "owner-disabled provider"
            ledger[name] = entry
            continue
        record = registry.get(name)
        if record is None:
            entry["skip_reason"] = "unknown specialist (not present in registry)"
            ledger[name] = entry
            continue
        if not record.enabled:
            entry["skip_reason"] = "specialist disabled"
            ledger[name] = entry
            continue
        entry["local"] = record.local
        # Lookup order is cheapest-first by contract: declared evidence is
        # free, quality floors are pure computation, health is the caller's
        # most expensive primitive (potentially a live probe).
        evidence = registry.evidence(name, cap)
        entry["evidence"] = evidence.value if evidence is not None else "invalid"
        if evidence is None or evidence is EvidenceClass.UNSUPPORTED:
            entry["skip_reason"] = (
                "capability evidence could not be determined"
                if evidence is None
                else "declared unsupported for capability"
            )
            ledger[name] = entry
            continue
        if evidence is EvidenceClass.UNKNOWN:
            entry["skip_reason"] = (
                "capability evidence unknown — evidence must be established before "
                "Auto may route (never improvised)"
            )
            ledger[name] = entry
            continue
        floor_gated = route.quality_floor is not None
        floor_ok = _floor_met(record, route, policy)
        if floor_gated:
            entry["floor_met"] = floor_ok
        if not floor_ok:
            entry["skip_reason"] = "quality floor not satisfied by owner-declared quality label"
            ledger[name] = entry
            continue
        healthy, health_detail = registry.health(name)
        entry["health_ok"] = bool(healthy)
        entry["health_detail"] = redact_secret_shapes(str(health_detail))
        if not healthy:
            entry["skip_reason"] = f"unhealthy: {redact_secret_shapes(str(health_detail))}"
            ledger[name] = entry
            continue
        if classification is None:  # defensive: every candidate carries a classification
            classification = (
                CLASSIFICATION_AUTO_LOCAL if record.local else CLASSIFICATION_AUTO_CLOUD
            )
        entry["admitted"] = True
        ledger[name] = entry  # the winner's own provenance entry is recorded too
        return RoutingDecision(
            capability=cap.value,
            outcome=OUTCOME_ROUTED,
            selected=name,
            classification=classification,
            reason=_admission_reason(classification, evidence, floor_gated, floor_ok),
            evidence=ledger,
            excluded_primary_backend=primary_backend,
        )

    summary = "; ".join(
        f"{name}: {ledger[name].get('skip_reason', 'no reason recorded')}" for name in ledger
    )
    return RoutingDecision(
        capability=cap.value,
        outcome=OUTCOME_NO_BACKEND,
        reason=f"no eligible specialist: {redact_secret_shapes(summary) if summary else 'candidate space empty'}",
        evidence=ledger,
        excluded_primary_backend=primary_backend,
    )


def resolve_capability_target(route: CapabilityRoute, decision: RoutingDecision) -> Optional[str]:
    """MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 -- the model a caller
    should use for the specialist ``decision`` just admitted, or ``None``
    for "no override, use that specialist's own provider-global/default
    model" (existing pre-binding behavior, unchanged).

    PURE, like resolve_capability() itself: no I/O, no mutation, identical
    inputs -> identical output.

    Admission rule (deliberately narrow): ``route.model`` only ever
    applies when the admitted specialist IS the route's own explicit
    ``specialist`` -- i.e. ``decision.outcome == OUTCOME_ROUTED``,
    ``decision.selected == route.specialist``, and
    ``decision.classification == CLASSIFICATION_EXPLICIT``. An owner-
    permitted fallback or an Auto pick never inherits a model string that
    was declared for a DIFFERENT provider -- that would silently hand one
    provider's model id to another provider's request. This mirrors the
    architectural law that a capability-specific model is bound to the
    (capability, provider) pair, never to the capability alone.

    A falsy ``route.model`` (``None`` or ``""``) always resolves to
    ``None`` here -- "explicitly configured but empty" and "never
    configured" are treated identically: no override.
    """
    if decision.outcome != OUTCOME_ROUTED:
        return None
    if not route.specialist or decision.selected != route.specialist:
        return None
    if decision.classification != CLASSIFICATION_EXPLICIT:
        return None
    return route.model or None


def parse_routes(raw) -> Tuple[dict, list]:
    """Fail-closed config-path parser for hand-edited ``multimodal_routes``
    prefs. Malformed or unknown entries are DROPPED with a warning (the
    caller may print/log them); they never raise and never crash startup.
    A dropped lane resolves as default-disabled. Returns (routes, warnings).
    """
    warnings = []
    if raw is None:
        return {}, warnings
    if not isinstance(raw, Mapping):
        return {}, ["multimodal_routes: expected a mapping; value dropped, all lanes default-disabled"]
    routes = {}
    for key, value in raw.items():
        cap = _normalize_capability(key)
        if cap is None:
            warnings.append(f"multimodal_routes: dropped route for unknown capability {key!r}")
            continue
        route, err = _coerce_route(value)
        if err is not None:
            warnings.append(f"multimodal_routes: dropped malformed route for {key!r}: {err}")
            continue
        routes[cap.value] = route
    return routes, warnings
