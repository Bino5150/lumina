"""
BaseLLMBackend — abstract interface all LLM backends must implement.
"""

import re
from abc import ABC, abstractmethod
from typing import Optional, Generator

from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL


class BaseLLMBackend(ABC):

    # Endpoint ownership contract.  Provider backends are fixed by default;
    # only explicitly self-hosted/configurable subclasses opt in below.  The
    # guarded property makes that classification load-bearing at the request
    # layer: stale Settings state and post-construction assignments cannot
    # redirect a fixed provider's credential-bearing requests.
    default_url = ""
    endpoint_configurable = False

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
        """Return all available model IDs from this backend."""
        ...

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
             reasoning_effort: Optional[str] = None) -> dict:
        """
        Non-streaming chat. Used for tool call turns.
        Returns raw response dict with OpenAI-compatible shape.
        disable_thinking: best-effort hint for backends whose server
        rejects (or silently mishandles) an assistant-prefill turn while
        thinking/reasoning mode is active — see complete_utility() below,
        which is the only caller that sets this True. Backends that don't
        have this failure mode may ignore it.
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
        successful list_models()-driven capability-cache refresh has ever
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
        list_models()-driven discovery and report whether THIS refresh
        attempt specifically succeeded (see core/backends/openrouter.py).

        Stays fully separate from reasoning_capabilities(), which remains a
        pure cache read forever -- this is the only method in the whole
        reasoning-capability surface that is allowed to perform network I/O,
        and only when a caller explicitly invokes it.
        """
        return True

    def _effective_reasoning_effort(self, reasoning_effort: Optional[str],
                                     disable_thinking: bool) -> Optional[str]:
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
        """
        return None if disable_thinking else reasoning_effort

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
        """
        messages = [{"role": "user", "content": prompt}]
        if prefill:
            messages.append({"role": "assistant", "content": prefill})

        try:
            response = self.chat(messages=messages, tools=None,
                                  temperature=temperature, max_tokens=max_tokens,
                                  disable_thinking=True)
            message = self.extract_message(response)
            content = (message.get("content") or "").strip()
            if not content:
                content = (message.get("reasoning_content") or "").strip()
        except Exception as e:
            print(f"[UTILITY] complete_utility failed: {e}", flush=True)
            return None

        # Inlined rather than importing core.agent.strip_think_blocks — that
        # function is trivial, but importing core.agent at all pulls in its
        # full transitive chain (every tool registration module) just to
        # reach a one-line regex. Not worth the weight or the fragility for
        # a backend-layer utility method that should stay lightweight.
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)  # unclosed — truncated mid-think
        content = content.strip()
        if prefill:
            content = re.sub(rf'^{re.escape(prefill)}\s*', '', content, flags=re.IGNORECASE)
        content = content.strip()
        return content or None
