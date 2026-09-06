"""
OpenAI backend — official OpenAI API, native Responses transport.
Set OPENAI_API_KEY in config.py

OPENAI-RESPONSES-01: this backend now speaks /v1/responses exclusively
instead of /chat/completions. Root cause (live-verified 2026-09-05 against
the real api.openai.com endpoint): OpenAI's Chat Completions API returns a
hard HTTP 400 for gpt-5.6-family reasoning models the moment BOTH function
tools AND a non-"none" reasoning_effort are present in the same request --
"Function tools with reasoning_effort are not supported for gpt-5.6-luna in
/v1/chat/completions. To use function tools, use /v1/responses or set
reasoning_effort to 'none'." This was never a Lumina code branch (no
tools-vs-reasoning conditional existed anywhere in core/agent.py or this
backend) -- it was a provider-side rejection this backend had no way to
avoid while pinned to Chat Completions. /v1/responses accepts reasoning +
tools + vision together without restriction (live-verified: single image,
multi-image, and both together with tool_choice="auto"/"required").

Design: translate at THIS boundary only. chat()/chat_stream() build a
Responses-shaped request from Lumina's ordinary Chat-Completions-shaped
`messages` list (exactly what every other backend already receives from
core/context.py's build_messages()) and normalize the Responses reply back
into the same Chat-Completions-shaped dict BaseLLMBackend's
extract_message()/is_tool_call()/get_tool_calls()/parse_tool_call()/
extract_termination() and LMStudioBackend's extract_reasoning() already
know how to read. core/agent.py, the completion-control state machine, and
every other backend are unmodified by this change.

Statelessness (live-verified): every request sends `store: false` and
carries no `previous_response_id`. Reasoning items are never captured,
stored, or replayed across rounds -- only the model's own OPTIONAL,
human-readable `summary` text (when present) is surfaced, exactly like the
existing reasoning_content telemetry path, via the synthesized message's
"reasoning_content" field. Opaque/encrypted reasoning state
(`encrypted_content`) is never read, stored, or threaded into a later
request. A live continuation test (2026-09-05) confirmed dropping a prior
turn's reasoning item entirely produces a correct final answer with no
error -- OpenAI's documented "should replay reasoning items" guidance is a
quality recommendation, not an enforced requirement for this model, so
Lumina's existing "no cross-turn provider state" law (every WORK round
already resends the agent's own full local history, matching every other
backend) is preserved without adding any opaque per-turn buffer.
"""

import json
from typing import Optional, Generator

import requests

import config
from .lmstudio import (
    LMStudioBackend,
    discover_openai_compatible_models,
    join_endpoint,
    classify_provider_error,
    format_provider_error,
    _iter_lines_safe,
)
from .base import ModelDiscoveryOutcome, ModelDiscoveryResult, ToolChoiceMode
from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL


# Patch 3A.4 Part 2A -- per-model reasoning-effort capability data.
#
# Deliberately declared here on OpenAIBackend, NOT on LMStudioBackend --
# GroqBackend and every other LMStudioBackend-derived backend must keep
# reporting NO_REASONING_CONTROL unless it has its own real data (see
# reasoning_capabilities() below and tests/test_reasoning_translation.py's
# sibling-leakage checks).
#
# This is intentionally NOT cross-referenced against KNOWN_MODELS below.
# That catalog is offline suggestion data, while reasoning_capabilities()
# takes an explicit model string and remains independent of discovery/list
# membership. Unknown models safely fall through to NO_REASONING_CONTROL.
#
# OPENAI-RESPONSES-01 -- live-verified 2026-09-05: /v1/responses accepts
# "max" for this family (HTTP 200, real reasoning_tokens usage); the old
# /chat/completions transport actually REJECTED "max" with HTTP 400
# ("Unsupported value... Supported values are: none, low, medium, high,
# xhigh") even though it was already listed here -- a live bug this
# migration incidentally fixes, not one this table introduces.
_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_GPT_5_6_CAPS = ReasoningCapabilities(efforts=_REASONING_EFFORTS, default_effort="medium")

_REASONING_MODELS = {
    "gpt-5.6": _GPT_5_6_CAPS,
    "gpt-5.6-sol": _GPT_5_6_CAPS,
    "gpt-5.6-terra": _GPT_5_6_CAPS,
    "gpt-5.6-luna": _GPT_5_6_CAPS,
}

# VISION-TOOL-INTEROP-01 -- OPENAI-RESPONSES-01: live-verified 2026-09-05
# against the real API -- a single image, two images, tools, and
# reasoning.effort="high" all combined in one /v1/responses request
# returned HTTP 200 with a correct function_call output item, for every
# model in this set. Scoped to exactly the models with that live evidence
# (the same gpt-5.6 family reasoning_capabilities() already covers) --
# every other/unknown OpenAI model keeps the safe base-class default
# (False) until it gets its own live verification, matching
# OpenRouterBackend's existing per-model-evidence pattern for this same
# method.
_VISION_TOOL_CAPABLE_MODELS = frozenset(_REASONING_MODELS.keys())


def _translate_user_content(content):
    """Lumina's vision content-part shape (a list of
    {"type": "text", "text": ...} / {"type": "image_url", "image_url":
    {"url": ...}} parts, built by ui/main_window.py) -> Responses `input`
    content parts. A plain string passes through unchanged -- Responses
    accepts a bare string for a text-only message's `content`, live-
    verified 2026-09-05. Unrecognized part types are dropped rather than
    guessed at; that only ever happens for a future attachment shape this
    translator has not been taught yet, and dropping is the same fail-safe
    posture the rest of this file uses for unknown data."""
    if not isinstance(content, list):
        return content or ""
    parts = []
    for part in content:
        ptype = part.get("type")
        if ptype == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts


def _translate_messages_to_input(messages: list) -> list:
    """Lumina's ordinary Chat-Completions-shaped `messages` (exactly what
    core/context.py's build_messages() hands every backend) -> a Responses
    `input` items array.

    Live-verified 2026-09-05 as a full round trip: system + user ->
    function_call emitted -> the SAME shape ctx.add_tool_call()/
    add_tool_result() persist (assistant message with "tool_calls", tool
    message with "tool_call_id") translated back in on the next round ->
    correct final answer -> a later turn's plain prior assistant reply
    replayed as ordinary history -> a second correct tool call. No
    Responses-specific state is threaded between calls: every round
    reconstructs `input` fresh from whatever `messages` this call received,
    exactly like every other backend's stateless chat().
    """
    input_items = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            input_items.append({"role": "system", "content": m.get("content") or ""})
        elif role == "user":
            input_items.append({"role": "user", "content": _translate_user_content(m.get("content"))})
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments") or "{}",
                })
            content = m.get("content")
            if content:
                input_items.append({"role": "assistant", "content": [{"type": "output_text", "text": content}]})
        elif role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id"),
                "output": m.get("content") or "",
            })
    return input_items


def _translate_tools(tools: list) -> list:
    """Lumina's registry tool schemas are Chat-Completions-shaped
    ({"type": "function", "function": {"name", "description",
    "parameters"}}) -- Responses wants the same fields flattened one level,
    with no "strict" flag. `strict` is deliberately never set: it requires
    every schema to declare "additionalProperties": false and list every
    property as required, and live grep of tools/*.py confirms only a
    handful of Lumina's ~88 registered tool schemas do that -- forcing
    strict mode here would reject the rest. Live-verified 2026-09-05 that a
    non-strict schema (no additionalProperties at all, matching most real
    registered tools) is accepted normally."""
    translated = []
    for t in tools:
        fn = t.get("function", t)
        translated.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return translated


def _normalize_responses_body(body: dict) -> dict:
    """A raw /v1/responses response body -> the same Chat-Completions-
    shaped dict this backend's Chat Completions transport used to return,
    so BaseLLMBackend.extract_message()/is_tool_call()/get_tool_calls()/
    parse_tool_call()/extract_termination() and LMStudioBackend.
    extract_reasoning() (all inherited here unmodified) keep working
    without any changes to core/agent.py or base.py.

    Reasoning provenance (Lumina law: reasoning is evidence, never Final):
    only `reasoning` output items' OPTIONAL, human-readable `summary` text
    is ever surfaced here, into the synthesized message's
    "reasoning_content" field -- the exact field LMStudioBackend.
    extract_reasoning() already knows to read. `encrypted_content` is never
    read from `body` at all, by construction -- there is no code path in
    this file capable of extracting it, so it can never be persisted,
    replayed, or shown as Think/Final/transcript.

    finish_reason is synthesized (never taken verbatim from the provider)
    into exactly the vocabulary BaseLLMBackend._OAI_COMPLETE_FINISH_REASONS
    / _OAI_INCOMPLETE_FINISH_REASONS already classify: "tool_calls"/"stop"
    for a positively completed response, "length" for status="incomplete"
    (live-verified reason: "max_output_tokens") -- anything else (a
    provider-side "failed" status, a malformed/missing body) is left as
    None, which extract_termination() already treats as UNKNOWN, never as
    proof of completion.
    """
    output = body.get("output") or []
    tool_calls = []
    content_parts = []
    reasoning_parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id"),
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "{}",
                },
            })
        elif itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                    content_parts.append(part["text"])
        elif itype == "reasoning":
            for part in item.get("summary") or []:
                if isinstance(part, dict) and part.get("type") == "summary_text" and part.get("text"):
                    reasoning_parts.append(part["text"])

    message = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_parts:
        message["reasoning_content"] = "\n\n".join(reasoning_parts)

    status = body.get("status")
    if status == "incomplete":
        finish_reason = "length"
    elif tool_calls:
        finish_reason = "tool_calls"
    elif status == "completed":
        finish_reason = "stop"
    else:
        finish_reason = None

    usage = body.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    normalized_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens_details": {"cached_tokens": input_details.get("cached_tokens", 0)},
        "completion_tokens_details": {"reasoning_tokens": output_details.get("reasoning_tokens", 0)},
    }

    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": normalized_usage,
    }


class OpenAIBackend(LMStudioBackend):

    name = "openai"
    display_name = "OpenAI"
    default_url = "https://api.openai.com/v1"
    endpoint_configurable = False

    # AGENT-CONTINUATION-01B -- live-verified 2026-08-28 against Chat
    # Completions, RE-verified 2026-09-05 against /v1/responses: a real
    # request with tool_choice="required" returns HTTP 200 with a genuine
    # forced tool call. Both transports support this identically.
    supports_required_tool_choice = True

    # Offline suggestions only; never evidence of a successful refresh.
    KNOWN_MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
    ]

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "OPENAI_API_KEY", "") if api_key is None else api_key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        """Compatibility output: live IDs, else explicit offline suggestions."""
        result = self.discover_models()
        if result.outcome is ModelDiscoveryOutcome.SUCCESS:
            return list(result.models)
        return list(result.offline_suggestions)

    def discover_models(self) -> ModelDiscoveryResult:
        """GET /v1/models is a shared, transport-independent listing
        endpoint -- unaffected by the Chat Completions -> Responses
        migration, so this stays unchanged."""
        return discover_openai_compatible_models(
            self, offline_suggestions=self.KNOWN_MODELS
        )

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "OPENAI_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"

    def _reasoning_capable(self, model: Optional[str]) -> bool:
        """True only for a model this backend has real reasoning capability
        data for -- reuses reasoning_capabilities() rather than a second,
        parallel table, so this can never drift out of sync with the
        capability data apply_reasoning() already validates against."""
        return self.reasoning_capabilities(model) is not NO_REASONING_CONTROL

    def supports_vision_with_tools(self, model: Optional[str] = None) -> bool:
        return model in _VISION_TOOL_CAPABLE_MODELS

    def _apply_output_token_limit(self, payload: dict, max_tokens: int,
                                  model: Optional[str] = None) -> None:
        """Responses' output-budget field. Renamed from the Chat Completions
        translation this hook used to own (max_completion_tokens) -- same
        seam, same call sites (chat()/chat_stream() below), new wire name."""
        payload["max_output_tokens"] = max_tokens

    def _apply_disable_thinking(self, payload: dict) -> None:
        """No local thinking-disable wire fields on Responses either.

        UTILITY-RUNTIME-01's original concern (OpenAI-compatible cloud
        transports reject LM Studio's local `thinking`/
        `chat_template_kwargs` fields outright) still applies, and
        Responses has no equivalent field to send in the first place --
        complete_utility()'s disable_thinking=True already reaches this
        backend as reasoning_effort=None via
        BaseLLMBackend._effective_reasoning_effort() (see chat() below),
        which is sufficient on its own: no `reasoning` object is added to
        the payload at all, so there's nothing further to suppress here.
        """
        return None

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 2A -- reasoning-effort capability + wire translation
    # ------------------------------------------------------------------

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        """
        Implemented directly here, NOT on LMStudioBackend -- see the module
        docstring above _REASONING_MODELS for why. `model=None` (no model
        specified) always falls through to NO_REASONING_CONTROL rather than
        guessing this backend's currently-configured model, matching the
        base class's documented contract (get_model() is not consulted).
        """
        if model is None:
            return NO_REASONING_CONTROL
        return _REASONING_MODELS.get(model, NO_REASONING_CONTROL)

    def _apply_reasoning_override(self, payload: dict, effort: str,
                                   model: Optional[str] = None) -> None:
        """
        OPENAI-RESPONSES-01: Responses' reasoning wire shape is a nested
        object, not Chat Completions' flat `reasoning_effort` string field.
        `summary: "auto"` is always requested alongside a real effort so
        Lumina's existing Think/reasoning-telemetry path
        (_collect_tool_round_reasoning() in core/agent.py, via
        extract_reasoning() -> message["reasoning_content"], populated by
        _normalize_responses_body() above) has something to surface when
        the model produces one -- live-verified 2026-09-05: a `reasoning`
        output item's `summary` is genuinely optional per-response (an
        adaptive decision by the model, not a per-request toggle beyond
        this), so "no summary this round" is an expected, ordinary case,
        not a missing-field bug.
        """
        payload["reasoning"] = {"effort": effort, "summary": "auto"}

    # ------------------------------------------------------------------
    # OPENAI-RESPONSES-01 -- native Responses transport
    # ------------------------------------------------------------------

    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.7, max_tokens: int = 1024,
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None,
             tool_choice_mode: Optional[ToolChoiceMode] = None) -> dict:
        model = self.get_model()
        payload = {
            "model": model,
            "input": _translate_messages_to_input(messages),
            "store": False,
        }
        self._apply_output_token_limit(payload, max_tokens, model=model)

        # VISION-TOOL-INTEROP-01, ported from LMStudioBackend.chat(): keep
        # tools off a request that carries an image UNLESS this model has
        # confirmed vision+tools support (see supports_vision_with_tools()
        # above). This backend no longer calls LMStudioBackend.chat() at
        # all (full override, below), so this guard is reimplemented here
        # rather than inherited.
        has_vision = any(isinstance(m.get("content"), list) for m in messages)
        if tools and (not has_vision or self.supports_vision_with_tools(model)):
            payload["tools"] = _translate_tools(tools)
            resolved = self._resolve_tool_choice_mode(tool_choice_mode)
            payload["tool_choice"] = "required" if resolved == ToolChoiceMode.REQUIRED else "auto"

        # UTILITY-OPENAI-PARAMETER-CAPABILITY-01 -- live-verified
        # 2026-09-05: gpt-5.6-luna returns HTTP 400 ("Unsupported value:
        # 'temperature' does not support 0.3 with this model. Only the
        # default (1) value is supported.") for ANY non-default temperature
        # value, on both /chat/completions and /v1/responses. Every real
        # complete_utility()/complete_utility_content_only() call site
        # (auto-name, dream-sweep, My Human curation, compaction, the
        # continuity compiler) passes an explicit temperature -- this
        # backend must never forward it for a reasoning-capable model,
        # rather than trying to substitute a "legal" value. Non-reasoning
        # models (gpt-4o, gpt-4o-mini, ...) keep receiving temperature
        # unchanged -- live-verified 2026-09-05 that gpt-4o-mini accepts a
        # non-default temperature normally via Responses.
        if not self._reasoning_capable(model):
            payload["temperature"] = temperature

        effective_effort = self._effective_reasoning_effort(reasoning_effort, disable_thinking)
        self.apply_reasoning(payload, effective_effort, model=model)

        resp = None  # BACKEND-ERROR-01: bound-checkable for the HTTPError handler
        try:
            resp = requests.post(
                join_endpoint(self.base_url, "responses"),
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
            )
            resp.raise_for_status()
            return _normalize_responses_body(resp.json())
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"{self.display_name} not reachable at {self.base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError(f"{self.display_name} request timed out.")
        except requests.exceptions.HTTPError as e:
            if resp is None:
                raise RuntimeError(format_provider_error(self.display_name, None, "", str(e)))
            print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(format_provider_error(self.display_name, resp.status_code, resp.text, str(e)))

    def chat_stream(self, messages: list, max_tokens: int = 4096,
                    temperature: float = 0.7,
                    reasoning_effort: Optional[str] = None) -> Generator[str, None, None]:
        """Streams the same '__THINK_START__'/token/'__THINK_END__' contract
        every other backend's chat_stream() uses. This signature never
        carries `tools` (no abstract chat_stream() implementation in this
        codebase does -- streaming is only ever used for the tool-free
        final answer, per core/agent.py's _stream_final()), so
        response.function_call_arguments.* events are structurally
        unreachable here and are not handled."""
        model = self.get_model()
        payload = {
            "model": model,
            "input": _translate_messages_to_input(messages),
            "store": False,
            "stream": True,
        }
        self._apply_output_token_limit(payload, max_tokens, model=model)
        if not self._reasoning_capable(model):
            payload["temperature"] = temperature
        self.apply_reasoning(payload, reasoning_effort, model=model)

        resp = None  # BACKEND-ERROR-01: bound-checkable for the HTTPError handler
        try:
            resp = requests.post(
                join_endpoint(self.base_url, "responses"),
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"{self.display_name} not reachable at {self.base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError(f"{self.display_name} request timed out.")
        except requests.exceptions.HTTPError as e:
            if resp is None:
                raise RuntimeError(format_provider_error(self.display_name, None, "", str(e)))
            raise RuntimeError(format_provider_error(self.display_name, resp.status_code, resp.text, str(e)))

        # Live-verified 2026-09-05 event vocabulary for a tool-free stream:
        # response.created -> response.in_progress -> response.output_item.
        # added -> [response.reasoning_summary_part.added ->
        # response.reasoning_summary_text.delta (repeated) ->
        # response.reasoning_summary_text.done ->
        # response.reasoning_summary_part.done] -> response.content_part.
        # added -> response.output_text.delta (repeated) -> response.
        # output_text.done -> response.content_part.done -> response.
        # output_item.done -> response.completed. Every event type not
        # explicitly handled below is safely ignored, matching lmstudio.py
        # chat_stream()'s existing permissive-skip posture.
        in_think = False
        for line in _iter_lines_safe(resp):
            if not line:
                continue
            line = line.decode("utf-8")
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as e:
                print(f"[{self.display_name} STREAM] skipped malformed "
                      f"frame ({type(e).__name__}), len={len(data)}", flush=True)
                continue

            ctype = chunk.get("type")
            if ctype == "response.reasoning_summary_text.delta":
                if not in_think:
                    in_think = True
                    yield "__THINK_START__"
                token = chunk.get("delta")
                if isinstance(token, str) and token:
                    yield token
            elif ctype == "response.output_text.delta":
                if in_think:
                    in_think = False
                    yield "__THINK_END__"
                token = chunk.get("delta")
                if isinstance(token, str) and token:
                    yield token
            elif ctype in ("response.completed", "response.incomplete", "response.failed"):
                if in_think:
                    in_think = False
                    yield "__THINK_END__"
                break
            elif ctype == "error":
                if in_think:
                    in_think = False
                    yield "__THINK_END__"
                err = chunk.get("message") or (chunk.get("error") or {}).get("message") or "stream error"
                raise RuntimeError(f"{self.display_name} error: {err}")
