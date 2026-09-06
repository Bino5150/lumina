"""
BaseLLMBackend — abstract interface all LLM backends must implement.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Generator

from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL


class ModelDiscoveryOutcome(str, Enum):
    """Truthful result of one live provider/server enumeration attempt."""

    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ToolChoiceMode(str, Enum):
    """AGENT-CONTINUATION-01B -- provider-neutral tool-selection intent the
    agent layer requests on a given chat() call. Purely a request-side
    signal (contrast TerminationStatus, which reads a response) -- what it
    actually does to the wire payload is entirely up to
    BaseLLMBackend._resolve_tool_choice_mode()/each backend's own request
    translation, never inspected or branched on by core/agent.py itself.

    AUTO: today's existing behavior -- the model may call zero or more of
    the offered tools, or none at all. This is what every chat() call
    already does before this patch; requesting it explicitly changes
    nothing about the wire payload.
    REQUIRED: the agent has already begun tool work this turn and wants
    the transport to structurally guarantee a tool call (whichever tool,
    including the finish_tool_work sentinel) rather than allowing a bare
    prose-only response. Only ever honored by a backend whose
    supports_required_tool_choice is True (live-verified support, not
    assumed) -- every other backend silently treats this exactly like
    AUTO, which is the safe, unchanged-behavior fallback.
    """

    AUTO = "auto"
    REQUIRED = "required"


class TerminationStatus(str, Enum):
    """AGENT-CONTINUATION-01A -- provider-neutral classification of why one
    non-streaming generation ended, independent of whether it contained a
    tool call. Exists so core/agent.py's tool loop can tell a positively
    truncated generation apart from a clean stop without string-matching a
    provider's raw finish/stop-reason vocabulary at the call site.

    COMPLETE: the provider positively reports the generation ended cleanly
    (a normal stop, or a clean stop into a tool call).
    INCOMPLETE: the provider positively reports the generation was cut off
    before the model chose to stop (token-budget truncation). A message in
    this state must never be treated as a genuine final answer, regardless
    of what content it happens to contain.
    UNKNOWN: no positively-understood signal either way (missing field,
    unrecognized value, malformed response). Never treated as an all-clear
    for anything beyond "the INCOMPLETE guard does not apply" -- UNKNOWN is
    not treated as proof of completion by any caller.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelDiscoveryResult:
    """Structured model discovery result consumed by Settings.

    ``models`` contains only IDs returned by a successful live request.
    Static catalogs belong in ``offline_suggestions`` and can therefore
    never be mistaken for provider results. ``diagnostic`` must already be
    safe for direct UI display: no raw response bodies, credentials, or
    provider exception text.
    """

    outcome: ModelDiscoveryOutcome
    models: tuple[str, ...] = ()
    offline_suggestions: tuple[str, ...] = ()
    diagnostic: str = ""


# UTILITY-RUNTIME-01: base-level, provider-neutral reasoning-field priority
# for complete_utility()'s empty-content fallback (below). Deliberately
# duplicated from -- not imported from -- lmstudio.py's own
# _REASONING_FIELD_PRIORITY/_first_reasoning_field: those back the OpenAI-
# compatible-family extract_reasoning() override, an OPT-IN telemetry hook
# a subclass only implements after live-verifying its own response shape
# (see extract_reasoning()'s docstring below). complete_utility()'s
# fallback must work identically for every BaseLLMBackend subclass --
# including a test stub or a future backend that never overrides
# extract_reasoning() at all -- so it reads the message dict directly
# rather than delegating to that narrower, opt-in seam. Same priority
# order and live-verification history as lmstudio.py's copy: "reasoning_
# content" (Qwen3/DeepSeek-R1-style local servers) before "reasoning"
# (OpenRouter's own unified field, live-verified against the real
# openrouter/z-ai/glm-5.3-flash route) before "thinking".
_UTILITY_REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")


def _first_utility_reasoning_field(message: dict) -> str:
    """First non-empty-after-strip reasoning-lane field on `message`, in
    _UTILITY_REASONING_FIELDS priority order, or "" if none qualify. Only
    ever consumes a value that is already a string -- a non-string value
    (malformed/unexpected shape) is skipped, never coerced."""
    for key in _UTILITY_REASONING_FIELDS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class BaseLLMBackend(ABC):

    # Endpoint ownership contract.  Provider backends are fixed by default;
    # only explicitly self-hosted/configurable subclasses opt in below.  The
    # guarded property makes that classification load-bearing at the request
    # layer: stale Settings state and post-construction assignments cannot
    # redirect a fixed provider's credential-bearing requests.
    default_url = ""
    endpoint_configurable = False

    # UTILITY-RUNTIME-01: safe fallback for any subclass that doesn't
    # override this (every real backend does; discover_models()'s own
    # base default above already implicitly assumed this attribute
    # exists). Not an abstractmethod/required override -- a minimal test
    # double implementing only the ABC's actual abstract surface must
    # still be able to call complete_utility()'s diagnostic logging
    # without an AttributeError.
    display_name = "backend"

    # AGENT-CONTINUATION-01B -- live-verified capability flag, False (safe
    # default) unless a subclass has been individually confirmed to accept
    # a required/forced tool-choice mode against its real production
    # transport. See _resolve_tool_choice_mode() below and each override
    # site's own comment for exactly what was verified and how.
    supports_required_tool_choice: bool = False

    @property
    def base_url(self) -> str:
        if not self.endpoint_configurable:
            return self.default_url.rstrip("/")
        return getattr(self, "_base_url", self.default_url).rstrip("/")

    @base_url.setter
    def base_url(self, value: Optional[str]) -> None:
        if self.endpoint_configurable:
            # None means "use this backend's default"; an explicit empty
            # string remains empty so Custom/unconfigured validation still
            # fails before any network request is attempted.
            self._base_url = (self.default_url if value is None else value).rstrip("/")
        else:
            # Fixed providers own their endpoint.  Silently retaining the
            # declared endpoint preserves constructor interface parity while
            # refusing generic/user-controlled redirection.
            self._base_url = self.default_url.rstrip("/")

    @property
    def current_endpoint(self) -> str:
        """Endpoint Settings should display and requests should consume."""
        return self.base_url

    @abstractmethod
    def get_model(self) -> str:
        """Return the active model ID. Auto-detect if not set."""
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return compatibility model output for non-Settings callers.

        This legacy surface is intentionally not the source of truth for a
        Refresh action: implementations may return offline suggestions or a
        configured-model fallback when live discovery fails. Call
        ``discover_models()`` whenever the distinction matters.
        """
        ...

    def discover_models(self) -> ModelDiscoveryResult:
        """Attempt live enumeration, or explicitly report it unsupported."""
        return ModelDiscoveryResult(
            ModelDiscoveryOutcome.UNSUPPORTED,
            diagnostic=f"{self.display_name} does not support live model enumeration.",
        )

    @abstractmethod
    def health_check(self) -> tuple[bool, str]:
        """
        Check if backend is reachable.
        Returns (ok: bool, message: str)
        e.g. (True, "Connected — qwopus-4b") or (False, "Connection refused")
        """
        ...

    @abstractmethod
    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.7, max_tokens: int = 1024,
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None,
             tool_choice_mode: Optional["ToolChoiceMode"] = None) -> dict:
        """
        Non-streaming chat. Used for tool call turns.
        Returns raw response dict with OpenAI-compatible shape.
        tool_choice_mode (AGENT-CONTINUATION-01B): the agent's requested
        ToolChoiceMode for this call, or None -- None and AUTO are always
        equivalent to each other and to every pre-01B caller's behavior
        (omitting the argument entirely reproduces today's exact wire
        payload on every backend). Only a backend with
        supports_required_tool_choice = True does anything different for
        REQUIRED; see _resolve_tool_choice_mode() below. Never inspected
        by core/agent.py -- it only ever sets the value it wants and lets
        each backend's own resolution decide the wire representation.
        disable_thinking: best-effort hint for backends whose server
        rejects (or silently mishandles) an assistant-prefill turn while
        thinking/reasoning mode is active — see complete_utility() below,
        which is the only caller that sets this True. Backends that don't
        have this failure mode may ignore it. How the intent reaches the
        wire is decided per transport by _apply_disable_thinking() --
        never by inlining provider-specific fields into a shared chat()
        body (UTILITY-RUNTIME-01: the LM Studio fields were once inlined
        here and leaked to every cloud subclass).
        reasoning_effort: Patch 3A.4 Part 3 -- the caller's raw requested
        reasoning-effort selection for this turn (or None for Provider
        Default), forwarded unchanged from LuminaAgent.chat(). Per-call
        only -- never stored on the backend instance. A backend with no
        real capability data ignores it safely (reasoning_capabilities()
        stays NO_REASONING_CONTROL, so apply_reasoning() is a no-op).
        disable_thinking=True always takes precedence over a non-None
        reasoning_effort -- see _effective_reasoning_effort() below.
        """
        ...

    @abstractmethod
    def chat_stream(self, messages: list, max_tokens: int = 1024,
                    temperature: float = 0.7,
                    reasoning_effort: Optional[str] = None) -> Generator[str, None, None]:
        """
        Streaming chat. Yields text chunks + think markers.
        Special yields: '__THINK_START__', '__THINK_END__'
        reasoning_effort: same per-call contract as chat() above. This
        signature has no disable_thinking param (never had one), so
        implementations forward reasoning_effort into apply_reasoning()
        directly, with no precedence guard needed.
        """
        ...

    # --- Helpers (concrete, shared across all backends) ---

    def _resolve_tool_choice_mode(self, tool_choice_mode: Optional["ToolChoiceMode"]) -> "ToolChoiceMode":
        """AGENT-CONTINUATION-01B -- resolve the agent's requested mode
        against this backend's live-verified capability. REQUIRED only
        survives the resolution if supports_required_tool_choice is True;
        None, AUTO, or an unsupported REQUIRED request all resolve to
        AUTO -- the safe, unchanged-behavior default every backend already
        implements today. Each concrete chat()/_build_payload() calls this
        once and translates ONLY the REQUIRED case onto its own wire shape
        (a bare string for OpenAI-compatible transports, a nested object
        for Anthropic/Gemini-shaped ones) -- AUTO never adds or changes a
        field versus pre-01B payloads."""
        if tool_choice_mode == ToolChoiceMode.REQUIRED and self.supports_required_tool_choice:
            return ToolChoiceMode.REQUIRED
        return ToolChoiceMode.AUTO

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        """
        Patch 3A.4 Part 1 -- backend-level reasoning capability contract.

        Return what this backend (optionally for a specific `model`)
        advertises about reasoning/thinking-effort control. See
        core/backends/reasoning.py for the full field semantics and the
        Provider Default contract.

        Fail-safe default: this base implementation always returns
        NO_REASONING_CONTROL (no positively advertised capability, so
        Lumina sends no reasoning override). Subclasses only need to
        override this when they actually have real capability data to
        advertise for a provider/model -- the base default requires zero
        subclass implementation to already be safe, which is the point:
        an unknown backend, an unknown model, missing metadata, or an old
        install that predates this method entirely (nothing to override)
        must all collapse to the same safe "no override" behavior.

        Must stay side-effect-free. Deliberately does NOT fall back to
        `self.get_model()` when `model` is omitted: get_model() is
        network-free for most backends, but not universally -- e.g.
        LMStudioBackend and OllamaBackend fall through to a live HTTP call
        when no model is configured yet. Silently triggering that just to
        answer a capability query (e.g. to populate a Settings dropdown)
        would be a surprising, provider-inconsistent side effect. Callers
        that want a model-aware answer must pass `model` explicitly; a
        subclass override that wants to use its own already-configured
        model is responsible for confirming that specific path is local
        for itself before doing so.
        """
        return NO_REASONING_CONTROL

    def supports_vision(self, model: Optional[str] = None) -> bool:
        """
        ANTHROPIC-VISION-CAPABILITY-01 -- backend/model capability
        contract: whether this backend can accept image content for
        `model` AT ALL (a strictly more fundamental question than
        supports_vision_with_tools() above, which asks whether vision and
        tool-calling can be COMBINED -- a backend/model could support
        vision alone, tools alone, both, or neither).

        Fail-safe default: False for every backend/model without live-
        verified or authoritatively-documented evidence otherwise. Every
        OpenAI-compatible (LMStudioBackend-family) backend and
        GeminiBackend already accept image content structurally (the
        request shape itself is OpenAI-shaped / already translated) and
        have never needed this gate -- only AnthropicBackend currently
        overrides this, using Anthropic's own documented model history
        (vision shipped with the Claude 3 family; every model released
        since supports it) rather than a guess. See that override for the
        full reasoning.

        Same contract as reasoning_capabilities()/supports_vision_with_
        tools() above: must stay side-effect-free, and `model=None`
        always returns False.
        """
        return False

    def supports_vision_with_tools(self, model: Optional[str] = None) -> bool:
        """
        VISION-TOOL-INTEROP-01 -- backend/model capability contract:
        whether this backend can send image content AND tools (with a
        resolved tool_choice) in the SAME request without the underlying
        provider silently misbehaving or rejecting the combination.

        Fail-safe default: False for every backend/model without live-
        verified evidence otherwise. LMStudioBackend.chat()'s has_vision
        guard uses this to decide whether to keep its existing
        conservative "no tools once any image is in this request" default
        (False) or trust a backend/model that has actually been confirmed
        to handle both together (True) -- see OpenRouterBackend's
        override, which reuses its existing per-model discover_models()
        cache (OpenRouter's own /models "architecture.input_modalities"
        and "supported_parameters" fields) rather than a new fetch or a
        hardcoded guess.

        Same contract as reasoning_capabilities() above: must stay side-
        effect-free (no HTTP, no get_model() fallback), and `model=None`
        always returns False -- a caller that wants a model-aware answer
        must pass `model` explicitly.
        """
        return False

    def apply_reasoning(self, payload: dict, requested: Optional[str],
                         model: Optional[str] = None) -> None:
        """
        Patch 3A.4 Part 2A -- shared, pure payload-translation entry point.

        Validates `requested` against this backend's (optionally
        model-specific) reasoning_capabilities() and, only if it validates
        to a real advertised effort, hands off to _apply_reasoning_override()
        to mutate `payload` in place.

        Critical invariant: if `requested` is None, or fails validation
        (unsupported/stale/unknown effort string, or a model this backend
        has no positive capability data for), `payload` is left
        byte-for-byte unchanged with respect to reasoning configuration --
        _apply_reasoning_override() is not even called in that case. This
        always routes through reasoning_capabilities(model).validate()
        rather than letting each provider hand-roll its own copy of that
        fail-safe rule (see core/backends/reasoning.py for why "nearest
        match" degradation is deliberately not a thing here).

        Must stay side-effect-free apart from mutating the supplied
        `payload` dict in place: no HTTP, no model discovery, no prefs
        reads/writes. Mutates in place (rather than returning a
        replacement) to match the existing request builders, which already
        hand back plain mutable dicts.
        """
        effort = self.reasoning_capabilities(model).validate(requested)
        if effort is None:
            return
        self._apply_reasoning_override(payload, effort, model)

    def _apply_reasoning_override(self, payload: dict, effort: str,
                                   model: Optional[str] = None) -> None:
        """
        Provider-specific wire translation hook. No-op by default.

        Only ever invoked by apply_reasoning() above, and only after
        `effort` has already been validated as a real, positively
        advertised value for this backend/model -- implementations do not
        need to re-validate `effort` themselves. Must mutate `payload` in
        place and do nothing else (no HTTP, no I/O, no reads of prefs or
        config) -- see apply_reasoning()'s side-effect-free contract above.
        """
        pass

    def configured_model(self) -> Optional[str]:
        """
        Patch 3A.4 Part 4 -- side-effect-free read of this backend's
        currently-configured model id, for reasoning-preference restoration
        (core/reasoning_preferences.py) and any other caller that needs a
        model id without risking network I/O.

        Deliberately does NOT call self.get_model(): get_model() is
        network-free for most backends, but LMStudioBackend/OllamaBackend
        fall through to a live HTTP call when no model is configured yet
        (see reasoning_capabilities()'s docstring above for the same
        concern) -- silently triggering that just to answer "what model is
        configured" would be a surprising, provider-inconsistent side
        effect, especially from a cold/fresh-process restoration path that
        never opened Settings or touched the network.

        Base implementation reads self._model, which is how every backend
        EXCEPT AnthropicBackend and GeminiBackend tracks its configured
        model (both of those use self.default_model instead and override
        this method accordingly -- see their own configured_model()).
        getattr() (not direct attribute access) so a hypothetical subclass
        that never sets self._model at all still safely returns None
        rather than raising. A falsy value (None or "", e.g.
        OmniRouteBackend's unconfigured default) is treated identically to
        "no configured model" -- never returned as an empty string.
        """
        model = getattr(self, "_model", None)
        return model or None

    def reasoning_capabilities_ready(self, model: Optional[str] = None) -> bool:
        """
        Patch 3A.4 Part 4 -- capability-discovery readiness seam.

        True by default: every backend in this patch except OpenRouterBackend
        advertises reasoning capability from a static, hardcoded table (or
        NO_REASONING_CONTROL) that is always immediately available -- there
        is nothing to discover, so there is nothing to ever be "not ready"
        for. OpenRouterBackend overrides this to report whether a
        successful discover_models()-driven capability-cache refresh has ever
        happened on this instance (see core/backends/openrouter.py).

        Side-effect-free: must never perform I/O itself, only report state.
        """
        return True

    def refresh_reasoning_capabilities(self) -> bool:
        """
        Patch 3A.4 Part 4 -- explicit, opt-in dynamic capability refresh.

        Base/static default: nothing to refresh, so this is a no-op that
        always reports success (True) without touching the network.
        OpenRouterBackend overrides this to actually perform its
        discover_models()-driven discovery and report whether THIS refresh
        attempt specifically succeeded (see core/backends/openrouter.py).

        Stays fully separate from reasoning_capabilities(), which remains a
        pure cache read forever -- this is the only method in the whole
        reasoning-capability surface that is allowed to perform network I/O,
        and only when a caller explicitly invokes it.
        """
        return True

    def _apply_disable_thinking(self, payload: dict) -> None:
        """
        UTILITY-RUNTIME-01 -- provider-boundary wire translation for the
        disable_thinking intent.

        complete_utility() expresses a provider-neutral intent: "suppress
        thinking/reasoning for this background completion." HOW that intent
        reaches the wire is transport-specific: LM Studio's server needs its
        local `thinking` + `chat_template_kwargs` fields (without them it
        400s on a prefilled assistant turn while thinking is enabled -- the
        S41 correction), while cloud OpenAI-compatible providers (OpenAI,
        OpenRouter, DeepSeek, Groq, Kimi, Qwen DashScope, gateways, custom
        endpoints) reject those same fields as unrecognized arguments.
        Ollama/Anthropic/Gemini implement chat() themselves and neither
        accept nor need a translation today.

        Contract: translate the intent into THIS transport's wire syntax, or
        do nothing when the transport has no supported syntax for it. A no-op
        is always safe: the assistant-prefill + output-side <think>-strip
        (complete_utility's S23 defense) remains the universal anti-bleed
        mechanism, so suppressing the local fields never reintroduces bleed.

        Mirrors _apply_output_token_limit(): a hook called from the shared
        request builder, implemented (or no-op'd) at each provider boundary.
        Never called when disable_thinking is False, so ordinary chat turns
        are structurally unreachable from this hook. Side-effect-free apart
        from mutating `payload` in place -- no HTTP, no prefs, no I/O.
        """
        return None

    def _effective_reasoning_effort(self, reasoning_effort: Optional[str],
                                     disable_thinking: bool,
                                     model: Optional[str] = None) -> Optional[str]:
        """
        Patch 3A.4 Part 3 -- shared disable_thinking-wins precedence guard.

        complete_utility() (below) calls chat(..., disable_thinking=True)
        for non-agentic utility completions (dream-sweep summarization,
        chat auto-naming, ...) and never passes reasoning_effort itself, so
        this never fires from that real call site today. It exists purely
        as defense-in-depth for a FUTURE caller that might supply both
        disable_thinking=True and a non-None reasoning_effort to the same
        chat() call: disable_thinking must always win, so the request never
        ends up simultaneously asking a backend to suppress thinking AND
        to reason at some explicit effort level -- a self-contradictory
        combination no caller should be able to produce even by accident.

        Every chat() override that accepts disable_thinking (LMStudio-
        family, Anthropic, Gemini, Ollama) routes its reasoning_effort
        through this before calling apply_reasoning(), instead of each
        hand-rolling the same `None if disable_thinking else reasoning_effort`
        check. chat_stream() implementations never receive disable_thinking
        at all (confirmed against the abstract signature above), so they
        call apply_reasoning() with reasoning_effort directly and have no
        need for this guard.

        UTILITY-RUNTIME-01: "disable thinking" still always wins over any
        supplied reasoning_effort, but "disable" no longer unconditionally
        means "send no reasoning override at all". That was safe for every
        backend with static capability tables (OpenAI/Anthropic/Gemini/
        Ollama/LMStudio all hardcode mandatory=False, so this branch is
        structurally unreachable for them) but not for a backend whose
        capability data is live-discovered per model -- OpenRouterBackend,
        whose /models response can positively advertise mandatory=true
        ("this model cannot turn reasoning off at all"). Live-verified
        against z-ai/glm-5.3-flash (mandatory=true, default_effort="max"):
        sending no reasoning override at all leaves the PROVIDER's own
        default effort in force, which reliably consumed an entire
        30-token utility budget on reasoning alone with finish_reason=
        "length" and zero content tokens emitted -- while explicitly
        requesting "low" or "high" effort against the identical prompt
        returned a real answer with finish_reason="stop" and 0 reasoning
        tokens. So for a model whose reasoning_capabilities(model) reports
        mandatory=True, this requests that model's own cheapest advertised
        effort (reasoning.cheapest_effort()) instead of None -- a model
        that CAN actually disable reasoning is completely unaffected and
        still gets None here, unchanged. A mandatory model with no ranked
        effort at all still returns None: there is no verified lever left
        to pull, and this method must never guess one. Capability-driven,
        not model-name-driven -- this branch only ever activates because
        reasoning_capabilities(model) positively reported mandatory=True,
        never because of a string match against a model id.
        """
        if not disable_thinking:
            return reasoning_effort
        caps = self.reasoning_capabilities(model)
        if caps.mandatory:
            return caps.cheapest_effort()
        return None

    def extract_message(self, response: dict) -> dict:
        try:
            return response["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected response format: {e}")

    def is_tool_call(self, message: dict) -> bool:
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message: dict) -> list:
        return message.get("tool_calls", [])

    def parse_tool_call(self, tool_call: dict) -> tuple:
        import json
        fn = tool_call["function"]
        name = fn["name"]
        try:
            args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
        except json.JSONDecodeError:
            args = {}
        return name, args

    # OpenAI-compatible-family finish_reason vocabulary this codebase has
    # actually observed or already encoded elsewhere -- "stop"/"length"/"eos"
    # mirror lmstudio.py chat_stream()'s own `finish_reason in ("stop",
    # "length", "eos")` grouping; "tool_calls" and "length" were both
    # directly captured live against the real OpenRouter/GLM path during
    # AGENT-CONTINUATION-01. Deliberately narrow -- anything else (missing
    # field, "content_filter", a future provider value) stays UNKNOWN rather
    # than guessed. Concrete default: every LMStudioBackend descendant
    # (DeepSeek/Groq/Kimi/LlamaCpp/OmniRoute/OpenAI/OpenRouter/Qwen/VLLM/
    # Custom) and OllamaBackend (also OpenAI-shaped, never overrides
    # extract_message either) get this for free with zero per-file changes.
    _OAI_COMPLETE_FINISH_REASONS = frozenset({"stop", "eos", "tool_calls"})
    _OAI_INCOMPLETE_FINISH_REASONS = frozenset({"length"})

    def extract_termination(self, response: dict) -> "TerminationStatus":
        """AGENT-CONTINUATION-01A -- see TerminationStatus docstring above.
        Reads the raw response, NOT the already-normalized extract_message()
        output -- finish_reason lives on the response, not the message dict,
        and normalization elsewhere in this class deliberately does not
        carry it forward."""
        try:
            finish_reason = response["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            return TerminationStatus.UNKNOWN
        if finish_reason in self._OAI_COMPLETE_FINISH_REASONS:
            return TerminationStatus.COMPLETE
        if finish_reason in self._OAI_INCOMPLETE_FINISH_REASONS:
            return TerminationStatus.INCOMPLETE
        return TerminationStatus.UNKNOWN

    def extract_reasoning(self, response: dict) -> Optional[str]:
        """
        AGENT-TOOL-THINK-TELEMETRY-01A1 -- passive extraction of whatever
        legitimate, provider-exposed reasoning already accompanies a
        non-streaming chat() response (tool-bearing or not). Purely a
        read of `response`; never performs I/O, never mutates `response`,
        never requests reasoning that wasn't already coming back on its
        own -- no backend's chat()/_build_payload() is changed by this
        method existing.

        Fail-safe default: this base implementation always returns None
        (same "unknown backend / no positive capability -> no override"
        posture as reasoning_capabilities()'s NO_REASONING_CONTROL
        default above). A subclass only overrides this when it has live-
        verified evidence its own non-streaming response shape actually
        carries a reasoning field -- see LMStudioBackend/OllamaBackend for
        the one shared implementation every OpenAI-compatible-shaped
        backend gets for free. AnthropicBackend and GeminiBackend
        deliberately do NOT override this: A0's source-vet + this slice's
        live OpenRouter/GLM probe confirmed neither backend's chat()
        payload currently requests extended thinking / includeThoughts,
        so there is nothing on their wire to extract, and asking either
        provider to start reasoning is explicitly out of scope for this
        slice (a provider-behavior change, not observability).

        Callers must treat a non-string or empty-after-strip return
        identically to None -- absence, not a signal to fabricate
        anything.
        """
        return None

    def _complete_utility_request(self, prompt: str, prefill: str,
                                   max_tokens: int, temperature: float) -> dict:
        """
        CONTEXT-LIFECYCLE-A4I. Shared, unguarded transport for both
        complete_utility() and complete_utility_content_only() below: build
        the request, route through THIS backend's own chat() +
        extract_message() — same model resolution, same auth headers, same
        timeout config as every real turn this backend handles (see
        complete_utility()'s own docstring below for why that matters —
        this is a pure extraction of that method's pre-A4I request-building
        code, not a behavior change).

        May raise — deliberately unguarded. Each public method below wraps
        this in its own try/except so complete_utility()'s existing
        "[UTILITY] complete_utility failed: ..." log line is preserved
        byte-for-byte, rather than both methods sharing one generic
        message.

        UTILITY-RUNTIME-01: primes reasoning-capability discovery first
        (self.reasoning_capabilities_ready()/refresh_reasoning_capabilities(),
        both pre-existing Patch 3A.4 Part 4 seams) so _effective_reasoning_
        effort()'s mandatory-reasoning handling has real data to read.
        get_llm_backend() (core/backends/loader.py) hands back a brand new
        instance on every call -- every dreaming.py consumer (Dream sweep,
        My Human curation, compaction) constructs a fresh backend right
        before its one utility call -- so a per-instance discovery cache
        populated only by Settings/ordinary-chat's own call sites can never
        be relied on to already be warm here. Base-class default (every
        backend except OpenRouterBackend): reasoning_capabilities_ready()
        is always True, so this never performs I/O for them. OpenRouter
        only: one real /models GET, exactly once per instance (ready flips
        True the moment it completes, successfully or not), never raises
        (refresh_reasoning_capabilities() reports failure as False, same
        "no positive capability -> no override" fail-safe as an unreached
        model already gets from reasoning_capabilities()'s dict.get()
        default) -- a failed refresh just leaves this call exactly as
        unaware of mandatory-reasoning as it was pre-UTILITY-RUNTIME-01.
        """
        model = self.configured_model()
        if model is not None and not self.reasoning_capabilities_ready(model):
            self.refresh_reasoning_capabilities()

        messages = [{"role": "user", "content": prompt}]
        if prefill:
            messages.append({"role": "assistant", "content": prefill})
        response = self.chat(messages=messages, tools=None,
                              temperature=temperature, max_tokens=max_tokens,
                              disable_thinking=True)
        return self.extract_message(response)

    @staticmethod
    def _strip_utility_think_leakage(content: str, prefill: str) -> str:
        """
        CONTEXT-LIFECYCLE-A4I. Shared <think>-block + prefill-echo stripping
        for both complete_utility() and complete_utility_content_only() —
        pure extraction of complete_utility()'s pre-A4I cleanup code,
        identical behavior. Inlined rather than importing
        core.agent.strip_think_blocks — that function is trivial, but
        importing core.agent at all pulls in its full transitive chain
        (every tool registration module) just to reach a one-line regex.
        Not worth the weight or the fragility for a backend-layer utility
        method that should stay lightweight.
        """
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)  # unclosed — truncated mid-think
        content = content.strip()
        if prefill:
            content = re.sub(rf'^{re.escape(prefill)}\s*', '', content, flags=re.IGNORECASE)
        return content.strip()

    def _log_utility_failure(self, method: str, exc: Exception) -> None:
        """
        UTILITY-RUNTIME-01: truthful, bounded failure diagnostic for the
        exception paths of complete_utility()/complete_utility_content_only().
        Classifies purely by exception TYPE and, for a RuntimeError, by the
        already-established provider-error "kind" vocabulary embedded in
        its own message text (format_provider_error() in lmstudio.py always
        renders "<provider> error (<kind>, HTTP <code>): ...", so a
        RuntimeError raised by any real backend's chat() already carries
        this without new plumbing) -- never a new per-provider guess, never
        a model-name string match. Logs provider/model/reason only: no
        prompt text, no API keys, no full response bodies. `method` is the
        exact public method name so the pre-existing "distinct log labels"
        contract (byte-identical prefixes callers/tests already depend on)
        still holds.
        """
        if isinstance(exc, TimeoutError):
            reason = "timeout"
        elif isinstance(exc, ConnectionError):
            reason = "network_error"
        elif isinstance(exc, ValueError):
            reason = "malformed_response"
        elif isinstance(exc, RuntimeError):
            match = re.search(r"error \(([a-z_]+),", str(exc))
            reason = match.group(1) if match else "provider_error"
        else:
            reason = "provider_error"
        print(f"[UTILITY] {method} failed: provider={self.display_name} "
              f"model={self.configured_model()} reason={reason} ({exc})", flush=True)

    def _log_utility_empty_result(self, method: str, had_raw_text: bool) -> None:
        """
        UTILITY-RUNTIME-01: truthful diagnostic for the non-exception "we
        got a response but ended up with nothing usable" path.

        `had_raw_text` distinguishes two genuinely different failures using
        data the caller already has in hand, no new plumbing required:
          - False -- content (and, for complete_utility(), the reasoning-
            lane fallback) were both empty from the provider itself. The
            provider produced nothing usable at all: reason=empty_output.
          - True -- SOME text was present (content or reasoning-lane) but
            _strip_utility_think_leakage() reduced it to nothing -- an
            unclosed <think> block with no real text after it, or content
            that was purely the echoed prefill. The provider did respond;
            the stripping step is what rejected it: reason=parser_rejection.

        Either way this only makes the failure visible at the backend
        layer -- every real call site already logs its OWN "skip" message
        right after seeing None (see ui/main_window.py's "[AUTO-NAME]
        complete_utility returned nothing", core/continuity_compiler.py's
        "utility_call_returned_none"), so this doesn't duplicate each
        consumer's own bookkeeping.
        """
        reason = "parser_rejection" if had_raw_text else "empty_output"
        print(f"[UTILITY] {method}: no usable final content "
              f"provider={self.display_name} model={self.configured_model()} "
              f"reason={reason}", flush=True)

    def complete_utility(self, prompt: str, prefill: str = "",
                          max_tokens: int = 500, temperature: float = 0.3) -> Optional[str]:
        """
        S41 / F-62 real fix. Shared, concrete helper for non-agentic
        "utility" LLM calls — dream-sweep summarization, chat auto-naming,
        anything that just wants a plain completion with no tool access.

        Routes through THIS backend's own chat() + extract_message() —
        same model resolution, same auth headers, same timeout config as
        every real turn this backend handles — instead of the old pattern
        of a bespoke requests.post() hardcoding config.LLM_BACKEND_URL
        directly. That old pattern had two live consequences: (1) each
        call site hand-rolled its own timeout as a bare literal completely
        disconnected from config.TOOL_CALL_TIMEOUT — dreaming.py's was 30s,
        auto-naming's was 60s, neither tied to anything — which is exactly
        what caused the dream-sweep timeout bug once the server got
        measurably slower under load; and (2) it only ever worked against
        an OpenAI-compatible local server. Point LLM_BACKEND at Anthropic
        or Gemini and both call sites would silently POST to the wrong
        endpoint shape with no auth headers at all — untested because
        cloud backends hadn't been exercised in anger yet, but a guaranteed
        failure the moment they were.

        prefill: optional non-empty assistant-turn prefix (e.g. "SUMMARY:",
        "TITLE:") — the established, backend-agnostic fix for thinking-model
        bleed (S23's fix for dreaming, reused here), rather than a
        backend-specific "disable thinking" payload flag that not every
        backend (especially cloud ones) would even understand.

        Never raises — callers should treat None as "skip this utility
        call," matching the pre-existing contract both call sites already
        expected.

        CONTEXT-LIFECYCLE-A4I: falls back to a reasoning-lane field when
        `content` is empty (S41/F-62's original fix for thinking-model
        bleed) — unchanged in spirit from every pre-A4I release, but
        UTILITY-RUNTIME-01 widened WHICH field name that fallback reads
        (see _first_utility_reasoning_field() below): the pre-existing
        code only ever checked `reasoning_content`, which is correct for
        Qwen3/DeepSeek-R1-style local servers but not for OpenRouter's own
        unified field, live-verified as plain `reasoning` (see
        lmstudio.py's `_REASONING_FIELD_PRIORITY` docstring for the same
        live discovery, made independently for chat()'s telemetry path —
        this call site had drifted out of sync with that lesson). A caller
        that must never accept reasoning-lane text as its result (the
        continuity compiler) uses complete_utility_content_only() below
        instead, which has no code path capable of reading any
        reasoning-lane field at all.
        """
        try:
            message = self._complete_utility_request(prompt, prefill, max_tokens, temperature)
        except Exception as e:
            self._log_utility_failure("complete_utility", e)
            return None

        content = (message.get("content") or "").strip()
        if not content:
            content = _first_utility_reasoning_field(message)
        had_raw_text = bool(content)

        content = self._strip_utility_think_leakage(content, prefill)
        if not content:
            self._log_utility_empty_result("complete_utility", had_raw_text)
        return content or None

    def complete_utility_content_only(self, prompt: str, prefill: str = "",
                                       max_tokens: int = 500, temperature: float = 0.3) -> Optional[str]:
        """
        CONTEXT-LIFECYCLE-A4I. The smallest content-only utility completion
        path: identical to complete_utility() in every respect (same
        transport, same auth/timeout config, same disable_thinking=True
        intent, same <think>-block/prefill-echo stripping) EXCEPT this
        method never reads `message.get("reasoning_content")` — there is no
        code path here capable of returning reasoning-lane text under any
        circumstance, structurally rather than by convention.

        Exists for callers where a stray reasoning-derived string silently
        standing in for "the actual answer" would be worse than no answer
        at all — today, the continuity compiler (core/continuity_compiler.py),
        which must never let raw chain-of-thought/reasoning text pass as its
        JSON candidate. Ordinary utility callers (dream-sweep summarization,
        chat auto-naming, human-profile curation) are unaffected and
        continue to use complete_utility() unchanged — this method changes
        no existing caller's behavior; it is purely additive.

        Empty `content` after stripping → None (matching complete_utility()'s
        "never raises, None means skip" contract), even if the backend
        happened to put real text in a reasoning-lane field for this
        response.
        """
        try:
            message = self._complete_utility_request(prompt, prefill, max_tokens, temperature)
        except Exception as e:
            self._log_utility_failure("complete_utility_content_only", e)
            return None

        content = (message.get("content") or "").strip()
        had_raw_text = bool(content)
        content = self._strip_utility_think_leakage(content, prefill)
        if not content:
            self._log_utility_empty_result("complete_utility_content_only", had_raw_text)
        return content or None
