"""
core/backend_identity.py -- SUBSCRIPTION-PLAN-BACKENDS-01B

Backend lane identity, fuel isolation vocabulary, and the single fuel-
crossing policy. This module is pure infrastructure: stdlib-only, no I/O, no
network, no config reads, no backend imports, nothing executes at import
time. It is the one place that says WHAT a backend lane is, which fuel it
burns, and which operations it may serve -- so the loader, the capability
router, the vision lane, the agent's dispatch seam, utility calls and
subagent spawning all consult one table instead of each carrying its own
hardcoded cloud/local set.

Foundational law (frozen by 01A / 01B):

  MODEL IDENTITY DOES NOT DEFINE BACKEND LANE IDENTITY.

  A backend lane is the registry key Lumina dispatches through ("openai",
  "anthropic", ...). It is NOT a model id and NOT a provider family. The
  persisted keys "openai" and "anthropic" keep their existing meaning: the
  API-key backends. A future ChatGPT-plan lane is a SIBLING lane
  ("openai_chatgpt_plan") that shares a provider family with "openai" but
  shares NOTHING else -- not credentials, not quota, not cost, not
  transport, not preference slots.

  provider family  != auth source      (same family, different credentials)
  provider family  != quota source     (same family, different tank)
  model id         != backend lane     (the same model string can be served
                                        by two lanes burning different fuel)
  backend failure  != permission to change fuel

What lives here:

  - BackendIdentity: the immutable record a dispatch captures (provider
    family, lane, transport, auth source, quota class, cost class, model).
    It carries NO account/workspace/session identifier by construction --
    private session binding belongs to a session manager, never to
    telemetry or preferences (a test pins the field set).
  - LANES: one LaneDescriptor per lane, including lanes that are known but
    NOT constructible ("reserved"). Every constructible backend in
    core.backends.loader.BACKENDS must have a descriptor (parity test), so
    a new lane cannot be added without declaring its fuel.
  - fuel_crossing_admitted(): the one default-deny policy for "may an
    operation that began on fuel A be served by fuel B".
  - operation_refusal(): per-lane admitted operation kinds. Subscription
    lanes admit NOTHING until a later slice proves a capability live.
  - Refusal helpers used by the agent dispatch seam and subagent spawning.

What does NOT live here: any OAuth/session/token code, any request
translation, any network, any preference I/O. 01B pours the slab; the
plumbing (session manager, wire contract) lands in later slices.

Claude subscription ("claude plan") decision, recorded: there is NO lane,
NO descriptor, NO constant and NO placeholder class for it anywhere. A
reserved-but-registered identity would be one rename away from a
dispatchable backend; absence cannot construct. Anthropic currently
requires prior approval before a third-party product may offer claude.ai
login or subscription rate limits, so the truthful state in code is that
the lane does not exist. A test pins that nothing resolves it.

Unregistered lane names (test doubles, typos) carry no descriptor and so no
restriction HERE -- they cannot reach production dispatch because
get_llm_backend() refuses any name that is not a classified BACKENDS
entry. They are labelled UNKNOWN in identity records, treated as NOT local
for placement, and denied outright wherever a protected (subscription)
fuel class is on the other side of a crossing.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Union

__all__ = [
    "QuotaClass",
    "CostClass",
    "AuthSource",
    "Transport",
    "OperationKind",
    "BackendIdentity",
    "LaneDescriptor",
    "LANES",
    "SUBSCRIPTION_QUOTA_CLASSES",
    "OPENAI_CHATGPT_PLAN_LANE",
    "ReservedBackendLaneError",
    "UnclassifiedBackendLaneError",
    "LaneOperationUnavailable",
    "LaneFuelViolation",
    "lane_descriptor",
    "identity_for_lane",
    "identity_of_backend",
    "quota_class_of",
    "is_local_placement",
    "fuel_crossing_admitted",
    "operation_refusal",
    "dispatch_lane_refusal",
    "subagent_lane_refusal",
]


class QuotaClass(str, Enum):
    """Which tank an operation draws from. Deliberately minimal -- this is
    NOT a billing engine. It exists so routing can answer one question:
    does this candidate use the same authorized fuel as the operation?"""

    LOCAL_COMPUTE = "local_compute"
    API_METERED = "api_metered"
    # User-configured endpoint (Custom / OmniRoute): could be a LAN box or a
    # paid gateway; Lumina cannot know, so it does not pretend to.
    ENDPOINT_DEFINED = "endpoint_defined"
    CHATGPT_PLAN_OR_CREDITS = "chatgpt_plan_or_credits"
    UNKNOWN = "unknown"


class CostClass(str, Enum):
    """How money is debited. UNKNOWN_DEBIT is the honest state for a
    subscription/credits lane: a debit may or may not occur and Lumina has
    no enforceable per-request signal. Unknown cost is NEVER zero."""

    LOCAL_COMPUTE = "local_compute"
    API_METERED = "api_metered"
    ENDPOINT_DEFINED = "endpoint_defined"
    UNKNOWN_DEBIT = "unknown_debit"


class AuthSource(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH_SUBSCRIPTION = "oauth_subscription"
    UNKNOWN = "unknown"


class Transport(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES_API = "responses_api"
    MESSAGES_API = "messages_api"
    GENERATE_CONTENT_API = "generate_content_api"
    RESPONSES_SIWC = "responses_siwc"
    UNKNOWN = "unknown"


class OperationKind(str, Enum):
    """The kinds of inference operation a lane may be asked to serve.
    Backgrounded work (scheduled/background tasks) runs as SUBAGENT; the
    non-agentic completions (auto-name, Dream, My Human curation,
    compaction) are UTILITY; the owner-triggered continuity compiler is
    REFORGE."""

    FOREGROUND_CHAT = "foreground_chat"
    VISION_SPECIALIST = "vision_specialist"
    UTILITY = "utility"
    REFORGE = "reforge"
    SUBAGENT = "subagent"


# Fuel classes that must never be crossed implicitly, in either direction:
# a pooled personal allowance (and possibly the owner's credits) that is
# neither metered per request nor Lumina's to spend on the owner's behalf
# for anything the owner has not explicitly selected.
SUBSCRIPTION_QUOTA_CLASSES = frozenset({QuotaClass.CHATGPT_PLAN_OR_CREDITS})

OPENAI_CHATGPT_PLAN_LANE = "openai_chatgpt_plan"


class ReservedBackendLaneError(ValueError):
    """The lane is a known identity that has no installed implementation.
    ValueError subclass so every existing `except ValueError` around backend
    construction keeps treating it as a construction failure -- and so it
    can never be mistaken for a reason to try another backend."""


class UnclassifiedBackendLaneError(ValueError):
    """A BACKENDS entry exists with no fuel classification. Refusing to
    construct is what turns "new lane forgot to declare its fuel" from a
    silent hole into a loud failure."""


class LaneOperationUnavailable(RuntimeError):
    """The selected lane does not admit this operation kind. Raised at the
    utility seam so BaseLLMBackend's existing never-raise public methods log
    it and return None: the operation fails UNAVAILABLE ON THE SELECTED
    LANE, it does not go looking for another backend."""


class LaneFuelViolation(RuntimeError):
    """An operation would run on a lane its dispatch identity forbids."""


@dataclass(frozen=True)
class BackendIdentity:
    """What a dispatch is, captured BEFORE the request and immutable after.

    There is intentionally no account, subject, workspace, host or session
    field: those never belong in telemetry, logs, prefs, transcripts or
    backups. A subscription backend keeps its private account binding in
    its own session manager and selects the right credential from there.
    """

    provider_family: str
    backend_lane: str
    transport: Transport
    auth_source: AuthSource
    quota_class: QuotaClass
    cost_class: CostClass
    model: Optional[str] = None

    def with_model(self, model: Optional[str]) -> "BackendIdentity":
        return dataclasses.replace(self, model=model)

    def telemetry_fields(self, operation_kind: Optional[OperationKind] = None) -> dict:
        """Flight-Recorder-safe projection (strings only).

        The auth-source value is emitted under the key ``access_source``,
        NOT ``auth_source``: core.redaction.SECRET_KEY_MARKERS redacts any
        field whose key contains "auth", so the semantically-named key would
        be stored as "[REDACTED]". (Same class of collision AGENT-FLIGHT-
        RECORDER-01A1 hit with a "token" field name.) A test drives this
        through a real recorder write/read so a rename cannot regress it.
        ``model`` is excluded: the recorder already has a model column.
        """
        fields = {
            "provider_family": self.provider_family,
            "backend_lane": self.backend_lane,
            "transport": self.transport.value,
            "access_source": self.auth_source.value,
            "quota_class": self.quota_class.value,
            "cost_class": self.cost_class.value,
        }
        if operation_kind is not None:
            fields["operation_kind"] = OperationKind(operation_kind).value
        return fields


@dataclass(frozen=True)
class LaneDescriptor:
    """Static, reviewed facts about one backend lane.

    local: placement fact for the capability router's local-first tiering
        (unchanged from the pre-01B hardcoded cloud set). It is NOT a fuel
        claim -- Custom/OmniRoute are placed local but their fuel is
        endpoint-defined.
    constructible: False marks a RESERVED lane: the identity vocabulary
        exists, but get_llm_backend() refuses it. Preferred over registering
        a nonfunctional placeholder class that could someday be built by
        accident.
    admitted_operations: the operation kinds this lane may serve. Legacy
        lanes admit everything (byte-identical behavior). A subscription
        lane starts with NOTHING and gains kinds one at a time, each only
        after live acceptance in its own slice.
    """

    lane: str
    provider_family: str
    transport: Transport
    auth_source: AuthSource
    quota_class: QuotaClass
    cost_class: CostClass
    local: bool
    constructible: bool = True
    admitted_operations: frozenset = frozenset(OperationKind)


def _legacy(lane, transport, auth, quota, cost, local, family=None):
    return LaneDescriptor(
        lane=lane,
        provider_family=family or lane,
        transport=transport,
        auth_source=auth,
        quota_class=quota,
        cost_class=cost,
        local=local,
    )


_LOCAL = (AuthSource.NONE, QuotaClass.LOCAL_COMPUTE, CostClass.LOCAL_COMPUTE, True)
_METERED = (AuthSource.API_KEY, QuotaClass.API_METERED, CostClass.API_METERED, False)
_ENDPOINT = (AuthSource.API_KEY, QuotaClass.ENDPOINT_DEFINED, CostClass.ENDPOINT_DEFINED, True)
_CC = Transport.CHAT_COMPLETIONS

_DESCRIPTORS = (
    # Self-hosted servers (placement: local).
    _legacy("lmstudio", _CC, *_LOCAL),
    _legacy("ollama", _CC, *_LOCAL),
    _legacy("llamacpp", _CC, *_LOCAL),
    _legacy("vllm", _CC, *_LOCAL),
    # User-defined OpenAI-compatible endpoints: established local placement,
    # honestly undeclared fuel.
    _legacy("custom", _CC, *_ENDPOINT),
    _legacy("omniroute", _CC, *_ENDPOINT),
    # API-key cloud providers. Each keeps its own persisted key and meaning.
    _legacy("openrouter", _CC, *_METERED),
    _legacy("deepseek", _CC, *_METERED),
    _legacy("groq", _CC, *_METERED),
    _legacy("openai", Transport.RESPONSES_API, *_METERED),
    _legacy("anthropic", Transport.MESSAGES_API, *_METERED),
    _legacy("gemini", Transport.GENERATE_CONTENT_API, *_METERED),
    _legacy("kimi", _CC, *_METERED),
    _legacy("qwen", _CC, *_METERED),
    # RESERVED -- schema/design only in 01B. Sibling of "openai" in provider
    # family only. Not constructible, not selectable, admits no operation.
    LaneDescriptor(
        lane=OPENAI_CHATGPT_PLAN_LANE,
        provider_family="openai",
        transport=Transport.RESPONSES_SIWC,
        auth_source=AuthSource.OAUTH_SUBSCRIPTION,
        quota_class=QuotaClass.CHATGPT_PLAN_OR_CREDITS,
        cost_class=CostClass.UNKNOWN_DEBIT,
        local=False,
        constructible=False,
        admitted_operations=frozenset(),
    ),
)

LANES: Mapping[str, LaneDescriptor] = MappingProxyType({d.lane: d for d in _DESCRIPTORS})


def _normalize(name) -> str:
    return name.strip().lower() if isinstance(name, str) else ""


def lane_descriptor(name) -> Optional[LaneDescriptor]:
    """The descriptor for a lane name, or None if the name is not a known
    lane (including every non-string input)."""
    return LANES.get(_normalize(name))


def identity_for_lane(name, model: Optional[str] = None) -> BackendIdentity:
    """Identity record for a lane name. An unregistered name yields an
    UNKNOWN identity -- never a guess at local/free."""
    descriptor = lane_descriptor(name)
    if descriptor is None:
        return BackendIdentity(
            provider_family="unknown",
            backend_lane=_normalize(name) or "unknown",
            transport=Transport.UNKNOWN,
            auth_source=AuthSource.UNKNOWN,
            quota_class=QuotaClass.UNKNOWN,
            cost_class=CostClass.UNKNOWN_DEBIT,
            model=model,
        )
    return BackendIdentity(
        provider_family=descriptor.provider_family,
        backend_lane=descriptor.lane,
        transport=descriptor.transport,
        auth_source=descriptor.auth_source,
        quota_class=descriptor.quota_class,
        cost_class=descriptor.cost_class,
        model=model,
    )


def identity_of_backend(backend) -> BackendIdentity:
    """Identity of a live backend object (or any duck-typed double).

    Total by construction -- never raises -- because it runs on the hot
    dispatch path against a long tail of test doubles. The lane comes from
    the backend's own ``name`` class attribute (the established registry
    key); the model from the network-free ``configured_model()`` when one
    exists.
    """
    name = getattr(backend, "name", None)
    model = None
    configured = getattr(backend, "configured_model", None)
    if callable(configured):
        try:
            value = configured()
            model = value if isinstance(value, str) and value else None
        except Exception:
            model = None
    return identity_for_lane(name, model=model)


def is_local_placement(name) -> bool:
    """Placement fact for local-first tiering. An unregistered name is NOT
    local: absence from a known list must never read as "runs on this box"."""
    descriptor = lane_descriptor(name)
    return bool(descriptor and descriptor.local)


def quota_class_of(subject) -> QuotaClass:
    """Coerce a BackendIdentity / QuotaClass / string to a QuotaClass.
    Anything unrecognizable is UNKNOWN, never a guess."""
    if isinstance(subject, BackendIdentity):
        return subject.quota_class
    if isinstance(subject, QuotaClass):
        return subject
    if isinstance(subject, str):
        try:
            return QuotaClass(subject)
        except ValueError:
            return QuotaClass.UNKNOWN
    return QuotaClass.UNKNOWN


def fuel_crossing_admitted(origin: Union[BackendIdentity, QuotaClass, str, None],
                           candidate: Union[BackendIdentity, QuotaClass, str, None]) -> bool:
    """May an operation that began on ``origin`` fuel be served by
    ``candidate`` fuel without a separate owner decision?

    DEFAULT DENY for protected (subscription) fuel, in BOTH directions:
    subscription -> paid API must never be an implicit fallback, and paid /
    local -> subscription must never silently drain the owner's allowance.
    Any protected class on either side that is not the same class on the
    other side is refused, including when the other side is unknown or
    unspecified.

    Crossings that involve no protected class (local <-> metered <->
    endpoint-defined) are the routing the product already performed before
    01B -- an explicit owner route, or Auto's local-first-then-cloud
    tiering -- and are grandfathered unchanged here. Tightening them is a
    deliberate future policy decision, not an 01B side effect. The test
    suite pins this so the grandfathering is a visible choice.
    """
    origin_class = quota_class_of(origin)
    candidate_class = quota_class_of(candidate)
    if origin_class is candidate_class:
        return True
    if origin_class in SUBSCRIPTION_QUOTA_CLASSES or candidate_class in SUBSCRIPTION_QUOTA_CLASSES:
        return False
    return True


def operation_refusal(subject, operation: OperationKind) -> Optional[str]:
    """None if the lane may serve ``operation``; otherwise a bounded,
    credential-free reason. ``subject`` is a BackendIdentity or a lane name.
    An unregistered lane carries no restriction here (see module docstring:
    it cannot be constructed in production)."""
    lane = subject.backend_lane if isinstance(subject, BackendIdentity) else subject
    descriptor = lane_descriptor(lane)
    if descriptor is None:
        return None
    operation = OperationKind(operation)
    if operation in descriptor.admitted_operations:
        return None
    return (
        f"Backend lane '{descriptor.lane}' does not admit {operation.value} "
        "operations in this build. Lumina will not substitute another backend "
        "or credential source for it."
    )


def dispatch_lane_refusal(origin: Optional[BackendIdentity], current: BackendIdentity,
                          operation: OperationKind) -> Optional[str]:
    """Dispatch-time guard. ``origin`` is the identity captured when the
    operation began (None when unknown); ``current`` is what is about to be
    dispatched. Refuses when the current lane does not admit the operation,
    or when the lane changed in a way that crosses protected fuel."""
    refusal = operation_refusal(current, operation)
    if refusal is not None:
        return refusal
    if (
        origin is not None
        and origin.backend_lane != current.backend_lane
        and not fuel_crossing_admitted(origin, current)
    ):
        return (
            f"This operation began on lane '{origin.backend_lane}' "
            f"({origin.quota_class.value}) and the backend is now "
            f"'{current.backend_lane}' ({current.quota_class.value}). A "
            "running operation does not change fuel source; start a new one."
        )
    return None


def subagent_lane_refusal(selected_lane, requested_lane) -> Optional[str]:
    """Guard for model-supplied ``backend`` overrides on spawn_subagent /
    run_background_subagent / schedule_background_subagent.

    ``selected_lane`` is the owner's currently selected lane (what a default
    subagent inherits, evaluated at the moment the child is built -- so a
    task scheduled earlier cannot silently run on a lane the owner selected
    since). ``requested_lane`` is the explicit override or None.
    """
    requested = _normalize(requested_lane)
    selected = _normalize(selected_lane)
    child = identity_for_lane(requested or selected)
    refusal = operation_refusal(child, OperationKind.SUBAGENT)
    if refusal is not None:
        return refusal
    if requested and selected and requested != selected:
        origin = identity_for_lane(selected)
        if not fuel_crossing_admitted(origin, child):
            return (
                f"A subagent on lane '{requested}' ({child.quota_class.value}) "
                f"would change fuel from the selected lane '{selected}' "
                f"({origin.quota_class.value}). Backend overrides may not "
                "cross subscription fuel."
            )
    return None
