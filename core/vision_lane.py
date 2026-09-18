"""
core/vision_lane.py -- MULTIMODAL-M2-BOUNDED-VISION-LANE-01

The bounded vision-understanding specialist lane: the first EXECUTABLE
capability of the Multimodal Capability Router. M1 (core/capability_router.py)
established the provider-neutral vocabulary, deterministic routing policy,
provenance, owner control, and the default-ephemeral specialist contract.
M2 consumes M1 -- it does NOT duplicate or replace any of it -- and makes
exactly one capability real:

    vision_understanding

Canonical flow:

    user supplies image(s) + current-turn task
        -> M1 policy selects the configured vision specialist
        -> fresh bounded specialist call receives ONLY:
               the current turn's image payload(s), in order
               + a bounded task prompt derived from the current request
               + no conversational history, no system prompt, no memory
        -> specialist returns a normalized textual observation
        -> observation enters the PRIMARY turn as machine-derived,
           non-authoritative DATA
        -> the primary backend continues reasoning/tool work unchanged
        -> raw routed image blocks NEVER enter primary provider history

Design laws honored here:

  THE SESSION NEVER CARRIES WHAT IT CAN'T NATIVELY DIGEST.
      Routed mode replaces the current turn's raw image blocks with a
      bounded text placeholder BEFORE they can enter primary history --
      on success, on failure, and on cancellation alike.

  SPECIALISTS OBSERVE; LOAD-BEARING FACTS ARE PROMOTED DELIBERATELY.
      The observation is hot-only workbench data appended to the current
      user turn. This module performs ZERO writes to Palace, My Human,
      the memory table, the Knowledge Base, Project docs, the SYSTEM
      prompt, or the owner profile. The durable chat row keeps the GUI's
      display text (established contract -- ui/main_window.py writes
      display_text, never raw content). Deliberate promotion happens
      through the primary agent's own trusted mechanisms.

  SPECIALIST OUTPUT IS DATA, NOT OWNER OR SYSTEM AUTHORITY.
      The observation is framed as machine-derived data inside a
      user-role message. It can never become a SYSTEM message, owner
      authority, or a tool/action trigger merely by being present.

  NO FULL-HISTORY REPLAY TO A SPECIALIST BY DEFAULT.
      The specialist request contains the current turn's images and a
      bounded task prompt -- nothing else. V1 admits no supporting
      context at all; a future slice may add explicitly contracted
      support items, never wholesale history.

  PAY SPECIALIST-MODEL PRICES ONLY FOR SPECIALIST-MODEL WORK.
      One bounded request per routed turn: current-turn images + capped
      prompt. No history replay, no system prompt, no tool schemas.

Lane activation policy (encoded by the M2 scroll, not invented here):

  - No route configured for vision_understanding, or the lane is
    owner-disabled (mode "disabled"): the lane is INERT -- the turn
    proceeds exactly as before this module existed (raw image blocks
    enter primary history; the MB-34 transport guard remains the
    defense-in-depth truth-teller). This is the default-install
    guarantee: upgrading must not spend cloud money or change behavior.

  - A route IS configured (mode "auto" or "specialist"): the turn is
    committed to routed semantics -- raw image blocks NEVER enter
    primary history, whatever the outcome. Resolution failure or
    specialist failure produces a truthful bounded failure notice in
    the primary turn; vision is never improvised and no fallback
    outside M1 policy ever executes.

MB-34 relationship: complementary, not competing. Routed turns keep raw
image blocks out of primary history entirely, so the inherited has_vision
transport guard is never even consulted for them. Legacy turns (lane
inert) keep full MB-34 defense-in-depth. Neither protection weakens the
other.

This module never imports core.agent (no cycles): cancellation is
reported as an outcome the AGENT translates into its own TurnCancelled.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Bounded-lane constants. Central definitions, each covered by tests.
# ---------------------------------------------------------------------------

VISION_CAPABILITY = "vision_understanding"

# The specialist task prompt is derived from the CURRENT turn's text and is
# hard-capped here. Bounded means testably bounded: the cap is asserted by
# tests, never silently exceeded. The MEDIA payload is never truncated.
VISION_PROMPT_MAX_CHARS = 4000

# Bounded specialist output budget -- a description, not an essay.
VISION_MAX_OUTPUT_TOKENS = 1024

# Failure diagnostics are truncated to keep provider error bodies bounded
# and to make accidental secret-bearing content structurally unlikely to
# propagate. Provider error bodies are diagnostics, never credentials, and
# the established format_provider_error() path already excludes headers.
_DIAGNOSTIC_MAX_CHARS = 300

# Cloud LLM backends (established classification -- mirrors the
# CLOUD_BACKENDS set ui/settings/general_tab.py has carried since Patch 3A.4;
# duplicated here rather than imported from the UI layer on purpose: core
# must not import ui). Anything else known to the loader is treated as a
# local placement fact for M1's local-first tiering.
_CLOUD_LLM_BACKENDS = frozenset(
    {"openrouter", "deepseek", "groq", "openai", "anthropic", "gemini", "kimi", "qwen"}
)

_OUTCOME_SUCCESS = "success"
_OUTCOME_CANCELLED = "cancelled"
_OUTCOME_NO_SPECIALIST = "no_specialist"
_OUTCOME_PROVIDER_UNAVAILABLE = "provider_unavailable"
_OUTCOME_TIMEOUT = "timeout"
_OUTCOME_PROVIDER_ERROR = "provider_error"
_OUTCOME_MALFORMED_RESPONSE = "malformed_response"


@dataclass(frozen=True)
class VisionRouteContext:
    """Everything the agent turn needs to execute one routed vision
    operation. Built once at chat() entry (pure, no network), consumed
    once in _chat_impl() after the user turn is added to history.

    decision:
        M1's own RoutingDecision -- the routing provenance record. M2
        never re-derives provider priority, disabled policy, fallback
        ordering, quality floors, or primary-backend exclusion.
    images:
        The current turn's image blocks, attachment order preserved.
        These live in memory only and are carried exclusively into the
        bounded specialist request.
    task_text:
        The current turn's text request (joined text parts), already
        capped at VISION_PROMPT_MAX_CHARS.
    primary_content:
        The image-free content list the primary session carries for this
        turn: one text block (routed notice + the user's own words).
    model_override:
        MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 -- the owner's
        capability-specific model for the admitted specialist, resolved
        once here via core.capability_router.resolve_capability_target()
        (never re-derived at call time). "" (never None, so this stays a
        plain falsy default like every other str field here) means no
        override: the specialist's own provider-global/default model
        applies, exactly as before this field existed.
    """

    decision: object
    images: Tuple[dict, ...]
    task_text: str
    primary_content: Tuple[dict, ...]
    model_override: str = ""


@dataclass(frozen=True)
class VisionLaneResult:
    """Provider-neutral result of one bounded vision operation.

    outcome: "success" | "cancelled" | "no_specialist" |
             "provider_unavailable" | "timeout" | "provider_error" |
             "malformed_response" | "empty_response"
    observation: normalized textual observation ("" unless success)
    provider / model: specialist identity ("" when none was reached)
    route_classification: M1's classification for the selected specialist
    image_count / prompt_chars: boundedness facts (counts/lengths only --
                                 never payload content)
    diagnostic: sanitized, truncated failure text ("" on success)
    """

    outcome: str
    observation: str = ""
    provider: str = ""
    model: str = ""
    route_classification: str = ""
    image_count: int = 0
    prompt_chars: int = 0
    diagnostic: str = ""


# ---------------------------------------------------------------------------
# Ingress helpers
# ---------------------------------------------------------------------------

def _image_blocks(content) -> Tuple[dict, ...]:
    """Current-turn image blocks in attachment order. Only OpenAI-shaped
    image_url blocks count (the GUI's established multipart shape)."""
    if not isinstance(content, list):
        return ()
    return tuple(
        block for block in content
        if isinstance(block, dict) and block.get("type") == "image_url"
    )


def _text_of_content(content) -> str:
    """The user's own words from the current turn: text blocks joined."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "\n".join(parts)


def _routed_primary_content(images_count: int, user_text: str) -> Tuple[dict, ...]:
    """The image-free content the PRIMARY session carries for a routed
    turn: one text block pairing the truthful routed notice with the
    user's own words. Raw image blocks never appear here -- on success,
    failure, or cancellation."""
    notice = (
        f"[{images_count} image(s) routed to the vision lane -- raw images "
        "are not carried in this session]"
    )
    if user_text:
        return ({"type": "text", "text": f"{notice}\n{user_text}"},)
    return ({"type": "text", "text": notice},)


# ---------------------------------------------------------------------------
# M1 integration (M2 CONSUMES M1 -- no second router)
# ---------------------------------------------------------------------------

def _build_routing():
    """Build the M1 registry + policy from the established config seams.

    Registry contents: one SpecialistRecord per provider the owner's
    configured vision route references (explicit specialist + permitted
    fallbacks), each DECLARING vision_understanding as EVIDENCED. In V1
    the owner's route configuration IS the capability declaration -- the
    owner names a provider they know performs vision work; M3's Settings
    surface will make that declaration explicit and editable. Declared
    capability is an architectural concept distinct from legacy
    primary-backend transport-guard state (MB-34 boundary).

    health_lookup is deliberately None: M1 treats specialists as healthy
    by default (no information != unhealthy) and NO network call is made
    merely to decide. Real unavailability surfaces as a bounded, truthful
    specialist-call failure instead.
    """
    import config
    from core import capability_router as cr

    routes, _warnings = cr.parse_routes(getattr(config, "MULTIMODAL_ROUTES", {}))
    policy = cr.RoutingPolicy(
        routes=routes,
        disabled_providers=tuple(getattr(config, "MULTIMODAL_DISABLED_PROVIDERS", []) or ()),
    )

    names = set()
    for route in routes.values():
        if route.specialist:
            names.add(route.specialist)
        for fallback in (route.fallbacks or ()):
            names.add(fallback)

    records = [
        cr.SpecialistRecord(
            name=name,
            kind="llm_backend",
            local=name not in _CLOUD_LLM_BACKENDS,
            capabilities={cr.Capability.VISION_UNDERSTANDING: cr.EvidenceClass.EVIDENCED},
        )
        for name in sorted(names)
    ]
    registry = cr.CapabilityRegistry(records)
    return registry, policy, routes


def prepare_routed_turn(agent, user_input):
    """Called at chat() entry for multipart turns that carry image blocks.

    Returns ``(primary_content, route_ctx)``:

      (user_input, None)          -- lane INERT: no route configured or the
                                     lane is owner-disabled. The caller
                                     proceeds with the original content --
                                     established behavior, byte-identical.
      (placeholder_content, ctx)  -- a vision route IS configured: the turn
                                     is committed to routed semantics. The
                                     returned content carries NO raw image
                                     blocks (truthful routed notice + the
                                     user's own words), whatever happens
                                     later -- success, failure, or cancel.

    Pure with respect to the agent: reads config, resolves M1 policy,
    builds frozen structures. Never constructs a backend, never touches
    the network, never mutates agent state.
    """
    from core import capability_router as cr

    images = _image_blocks(user_input)
    if not images:
        return user_input, None

    registry, policy, routes = _build_routing()
    route = routes.get(cr.Capability.VISION_UNDERSTANDING.value)
    if route is None or route.mode == cr.LANE_DISABLED:
        # Routing OFF: established behavior preserved (MB-34 guard applies).
        return user_input, None

    primary_backend = getattr(getattr(agent, "llm", None), "name", None)
    decision = cr.resolve_capability(
        registry, policy, cr.Capability.VISION_UNDERSTANDING,
        primary_backend=primary_backend,
    )

    user_text = _text_of_content(user_input)
    primary_content = _routed_primary_content(len(images), user_text)
    model_override = cr.resolve_capability_target(route, decision) or ""
    route_ctx = VisionRouteContext(
        decision=decision,
        images=tuple(images),
        task_text=_capped_task_text(user_text),
        primary_content=tuple(primary_content),
        model_override=model_override,
    )
    return list(primary_content), route_ctx


# ---------------------------------------------------------------------------
# Bounded specialist execution
# ---------------------------------------------------------------------------

def _capped_task_text(text: str) -> str:
    """The specialist sees the CURRENT turn's request, capped. Truncation
    is explicit (never silent) and never applies to media. Applied at
    prepare time so the bounded text is visible in the route context."""
    text = (text or "").strip()
    if len(text) > VISION_PROMPT_MAX_CHARS:
        text = text[:VISION_PROMPT_MAX_CHARS] + " …[current-turn request truncated at the bounded cap]"
    return text


def _bounded_task_prompt(task_text: str) -> Tuple[str, int]:
    """Wrap the (already capped) current-turn request into the specialist
    prompt. Defense-in-depth: the cap is re-checked here so the bounded
    contract holds even if a future caller bypasses prepare."""
    text = _capped_task_text(task_text)
    prompt = (
        "You are a vision specialist acting as a sensor for another AI "
        "agent. Describe and analyze the attached image(s) faithfully and "
        "completely, as needed to answer this request:\n\n"
        f"{text}\n\n"
        "Respond with a factual textual observation only."
    )
    return prompt, len(prompt)


def _sanitize_diagnostic(text: str) -> str:
    """Sanitize, then truncate. Sanitization uses the established
    redaction primitive (core/redaction.redact_secret_shapes -- the same
    one M1's provenance path uses) so key-shaped material can never ride a
    failure diagnostic into primary history; truncation keeps provider
    error bodies bounded."""
    text = (text or "").strip()
    try:
        from core.redaction import redact_secret_shapes
        text = redact_secret_shapes(text)
    except Exception:
        pass  # redaction must never break the failure path itself
    if len(text) > _DIAGNOSTIC_MAX_CHARS:
        text = text[:_DIAGNOSTIC_MAX_CHARS] + " …"
    return text


def _observation_block(result: VisionLaneResult) -> str:
    """The provenance-framed observation block appended to the primary
    user turn. Framing is descriptive, never authority-granting: this is
    machine-derived DATA in a user-role message -- structurally incapable
    of becoming SYSTEM, owner authority, or a tool trigger."""
    header = (
        f"[Vision specialist observation — machine-derived data, not owner "
        f"or system authority | capability: {VISION_CAPABILITY} | "
        f"specialist: {result.provider}"
        f"{(' / ' + result.model) if result.model else ''} | "
        f"route: {result.route_classification} | images: {result.image_count}]"
    )
    return f"{header}\n{result.observation}"


def _failure_block(result: VisionLaneResult) -> str:
    reason = result.diagnostic or result.outcome
    return (
        f"[Vision routing failed — no specialist observation available | "
        f"reason: {reason} | raw images not carried in this session]"
    )


def execute_routed_vision(agent, route_ctx, cancel_event=None,
                          turn_id=None, chat_id=None) -> VisionLaneResult:
    """Execute the bounded specialist call and inject the outcome into the
    current primary user turn (already added to hot history as image-free
    content by the caller).

    Contract:
      - The specialist is a FRESH backend instance per operation via the
        established loader (credentials flow through the backend's own
        config handling -- never duplicated here). It is NOT agent.llm,
        never replaces it, owns no conversation state, and dies with the
        operation.
      - The request carries ONLY the current turn's images (order
        preserved) + the bounded task prompt. No history, no system
        prompt, no Palace, no tool transcripts -- testably bounded.
      - Failure is bounded and truthful: the primary backend is never
        touched, no raw media enters primary history, no vision is
        improvised, and the sanitized reason is appended as data.
      - Cancellation (checked before and after the bounded call -- a
        blocked HTTP call cannot be interrupted, matching the
        established cooperative-cancellation contract) returns
        outcome="cancelled" with NOTHING appended; the agent raises its
        own TurnCancelled so this module never imports core.agent.

    Never raises. The caller decides what a cancelled outcome means.
    """
    decision = route_ctx.decision
    provider = getattr(decision, "selected", None) or ""
    classification = getattr(decision, "classification", None) or ""
    primary_backend = getattr(getattr(agent, "llm", None), "name", None)

    def _finish(result):
        _record_route_event(agent, route_ctx, result, turn_id=turn_id, chat_id=chat_id,
                            primary_backend=primary_backend)
        return result

    if decision.outcome != "routed" or not provider:
        # M1 could not admit a specialist (owner-disabled, unknown name,
        # exhausted candidates). Routed semantics hold: NO raw media enters
        # primary history; the failure is truthful and bounded.
        result = VisionLaneResult(
            outcome=_OUTCOME_NO_SPECIALIST,
            provider=provider,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images),
            diagnostic=_sanitize_diagnostic(
                f"M1 route resolution: {getattr(decision, 'outcome', 'unknown')} "
                f"({getattr(decision, 'reason', '') or 'no eligible specialist'})"
            ),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)

    if cancel_event is not None and cancel_event.is_set():
        return _finish(VisionLaneResult(
            outcome="cancelled", provider=provider,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images),
        ))

    prompt, prompt_chars = _bounded_task_prompt(route_ctx.task_text)

    try:
        from core.backends.loader import get_llm_backend
        # MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01: `model=` is only ever
        # passed when the owner configured an explicit capability-specific
        # model for THIS admitted specialist (resolved once in
        # prepare_routed_turn() via core.capability_router.
        # resolve_capability_target()). Omitting the kwarg entirely when
        # there is no override reproduces the exact pre-binding call shape
        # byte-for-byte -- no behavior change for every install that has
        # never configured one. get_llm_backend() always hands back a
        # fresh, non-shared instance, so this is a per-invocation
        # parameter, never a mutation of any provider-global/shared state.
        if route_ctx.model_override:
            specialist = get_llm_backend(name=provider, model=route_ctx.model_override)
        else:
            specialist = get_llm_backend(name=provider)
    except Exception as exc:
        result = VisionLaneResult(
            outcome=_OUTCOME_PROVIDER_UNAVAILABLE,
            provider=provider,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images),
            prompt_chars=prompt_chars,
            diagnostic=_sanitize_diagnostic(
                f"specialist backend construction failed ({type(exc).__name__})"
            ),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)

    model = getattr(specialist, "configured_model", lambda: None)() or ""

    try:
        response = specialist.chat(
            messages=[{
                "role": "user",
                "content": list(route_ctx.images) + [{"type": "text", "text": prompt}],
            }],
            max_tokens=VISION_MAX_OUTPUT_TOKENS,
        )
    except ConnectionError as exc:
        result = VisionLaneResult(
            outcome=_OUTCOME_PROVIDER_UNAVAILABLE, provider=provider, model=model,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images), prompt_chars=prompt_chars,
            diagnostic=_sanitize_diagnostic(str(exc)),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)
    except TimeoutError as exc:
        result = VisionLaneResult(
            outcome=_OUTCOME_TIMEOUT,
            provider=provider, model=model,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images), prompt_chars=prompt_chars,
            diagnostic=_sanitize_diagnostic(str(exc)),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)
    except RuntimeError as exc:
        result = VisionLaneResult(
            outcome=_OUTCOME_PROVIDER_ERROR,
            provider=provider, model=model,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images), prompt_chars=prompt_chars,
            diagnostic=_sanitize_diagnostic(str(exc)),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)
    except Exception as exc:
        result = VisionLaneResult(
            outcome=_OUTCOME_PROVIDER_ERROR,
            provider=provider, model=model,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images), prompt_chars=prompt_chars,
            diagnostic=_sanitize_diagnostic(f"{type(exc).__name__}: {exc}"),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)

    if cancel_event is not None and cancel_event.is_set():
        # Cancelled while the bounded call was in flight: no half-observation,
        # no raw-media residue, no durable false completion.
        return _finish(VisionLaneResult(
            outcome="cancelled", provider=provider, model=model,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images), prompt_chars=prompt_chars,
        ))

    observation = _normalize_observation(specialist, response)
    if observation is None:
        result = VisionLaneResult(
            outcome=_OUTCOME_MALFORMED_RESPONSE,
            provider=provider, model=model,
            route_classification=getattr(decision, "classification", None) or "",
            image_count=len(route_ctx.images), prompt_chars=prompt_chars,
            diagnostic=_sanitize_diagnostic(
                "specialist returned no usable textual content"
            ),
        )
        _append_to_current_user_turn(agent, _failure_block(result))
        return _finish(result)

    result = VisionLaneResult(
        outcome=_OUTCOME_SUCCESS,
        observation=observation,
        provider=provider, model=model,
        route_classification=getattr(decision, "classification", None) or "",
        image_count=len(route_ctx.images), prompt_chars=prompt_chars,
    )
    _append_to_current_user_turn(agent, _observation_block(result))
    return _finish(result)


def _normalize_observation(specialist, response) -> Optional[str]:
    """Normalized textual observation from a specialist response. Uses the
    specialist backend's own established extract_message() normalization;
    reasoning-lane siblings are NEVER carried into the observation. Returns
    None when the response carries no usable textual content -- a failed
    specialist never yields an invented observation."""
    try:
        message = specialist.extract_message(response)
    except Exception:
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        joined = "\n".join(parts).strip()
        return joined or None
    return None


def _append_to_current_user_turn(agent, text: str) -> None:
    """Append the observation/failure block to the CURRENT turn's user
    message (history[-1] immediately after add_user). Hot-only workbench
    data: the durable chat row keeps the GUI's display text (established
    contract), so this append creates no durable write and no memory write.
    Defensive shape checks keep a malformed history from crashing the turn.
    """
    history = getattr(getattr(agent, "ctx", None), "history", None)
    if not history:
        return
    message = history[-1]
    if not isinstance(message, dict) or message.get("role") != "user":
        return
    content = message.get("content")
    if isinstance(content, list):
        content.append({"type": "text", "text": text})
    elif isinstance(content, str):
        message["content"] = f"{content}\n{text}" if content else text


def _record_route_event(agent, route_ctx, result, turn_id=None, chat_id=None,
                        primary_backend=None):
    """Minimum structured observability (M2 scroll §12): counts, identities,
    classifications, lengths -- never image bytes/base64, never the prompt
    text, never credentials."""
    try:
        from core.agent import _fr_machine
        _fr_machine(
            agent, "turn.vision_route", turn_id=turn_id, chat_id=chat_id,
            fields={
                "outcome": result.outcome,
                "capability": VISION_CAPABILITY,
                "m1_outcome": getattr(route_ctx.decision, "outcome", None),
                "provider": result.provider,
                "model": result.model,
                "route_classification": result.route_classification,
                "image_count": result.image_count,
                "prompt_chars": result.prompt_chars,
                "observation_chars": len(result.observation or ""),
                "primary_backend": primary_backend,
                "primary_backend_unchanged": True,
                "bounded": True,
            },
        )
    except Exception:
        # Observability must never break a turn (same posture as every
        # other _fr_machine call site).
        pass