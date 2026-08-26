"""
core/backends/anthropic_backend.py

Native Anthropic (Claude) backend for Lumina.

Unlike openrouter.py / deepseek.py / groq.py / openai_backend.py, this does NOT
subclass LMStudioBackend. The wire format is fundamentally different:

  - Auth header is `x-api-key` + `anthropic-version`, not `Authorization: Bearer`
  - Endpoint is `/v1/messages`, not `/v1/chat/completions`
  - `system` is a top-level request field, not a message with role="system"
  - Tool schemas use `input_schema`, not `parameters`
  - Tool calls arrive as `tool_use` content blocks, not a `tool_calls` array
  - Tool results go back as `tool_result` content blocks inside a user message,
    not a message with role="tool" (matches context.py's add_tool_result() shape:
    {"role": "tool", "tool_call_id", "name", "content"})
  - Streaming is typed SSE events (message_start/content_block_start/
    content_block_delta/content_block_stop/message_delta/message_stop),
    not flat OpenAI-style delta chunks
  - Extended thinking arrives as a `thinking` content block with incremental
    `thinking_delta` events — maps naturally to the __THINK_START__/__THINK_END__
    sentinel convention lmstudio.py already uses for reasoning_content/<think> tags.

Confirmed against the real base.py: chat_stream() takes no `tools` param — only
chat() (non-streaming) ever carries tools, so chat_stream() never needs to handle
tool_use content blocks at all. This eliminated an earlier draft's invented
tool-call sentinel mechanism for streaming, which was solving a problem that
doesn't exist in this codebase's actual contract.

Inherits BaseLLMBackend directly and implements the contract natively.
"""

import json
import requests
from typing import Optional

import config
from core.backends.base import (
    BaseLLMBackend,
    ModelDiscoveryOutcome,
    ModelDiscoveryResult,
)
from core.backends.reasoning import ReasoningCapabilities, NO_REASONING_CONTROL

ANTHROPIC_VERSION = "2023-06-01"  # required header, independent of model version
API_ROOT = "https://api.anthropic.com/v1"
API_BASE = f"{API_ROOT}/messages"

# Offline suggestions only; never evidence of a successful refresh.
KNOWN_MODELS = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)

# Patch 3A.4 Part 2A -- per-model reasoning-effort capability data.
#
# Part 2A's task spec named the required model "Claude Sonnet 5" and
# suggested (but did not confirm) a "claude-sonnet-5" id. At the time, a
# repo-wide grep (config.py, config.example.py, anthropic_backend.py's own
# default/list_models(), every test file) for "claude-sonnet-5" /
# "sonnet 5" / "sonnet5" / "SONNET_5" found ZERO matches, so the
# capability matrix was keyed only to "claude-sonnet-4-6" -- the one
# ANTHROPIC_DEFAULT_MODEL actually resolved to at the time.
#
# CORRECTED (Part 2A correction pass): "claude-sonnet-5" is now confirmed
# to exist, and per Anthropic's own documentation it does NOT share an
# effort matrix with "claude-sonnet-4-6" -- claude-sonnet-4-6 does not
# support "xhigh". The two ids are therefore mapped as separate, distinct
# entries below, each with its own capability constant; do not collapse
# them back into one shared object even though both currently use
# default_effort="high", since their `efforts` tuples differ.
# ANTHROPIC_DEFAULT_MODEL in config.py is unaffected by this correction
# and still resolves to "claude-sonnet-4-6" -- this map is capability
# data only, not a change of Lumina's default model.
_SONNET_5_CAPS = ReasoningCapabilities(
    efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="high",
)

_SONNET_4_6_CAPS = ReasoningCapabilities(
    efforts=("low", "medium", "high", "max"),
    default_effort="high",
)

_REASONING_MODELS = {
    "claude-sonnet-5": _SONNET_5_CAPS,
    "claude-sonnet-4-6": _SONNET_4_6_CAPS,
}


def classify_anthropic_error(status_code, raw_body: str) -> dict:
    """
    Best-effort classification of an Anthropic API error response into the
    same normalized "kind" vocabulary core/backends/lmstudio.py's
    classify_provider_error() already uses for the OpenAI-compatible backend
    family (generic/billing_required/insufficient_quota/authentication/
    rate_limited). Never raises.

    Anthropic's error body is a documented, stable envelope:
        {"type": "error", "error": {"type": <error_type>, "message": <str>},
         "request_id": <str>}
    with a fixed, published set of error.type values, each already pinned to
    one HTTP status (per docs.claude.com/en/api/errors): invalid_request_error
    (400), authentication_error (401), billing_error (402), permission_error
    (403), not_found_error (404), rate_limit_error (429), api_error (500),
    overloaded_error (529). Unlike Gemini/OpenAI-compatible providers, there's
    no ambiguity to resolve via message text here -- error.type alone is
    authoritative, so this is a direct lookup rather than a haystack scan.
    billing_error and permission_error are kept distinct in Anthropic's own
    vocabulary (billing = payment/plan problem, permission = this key can't
    use this resource) but map to different normalized kinds here since
    they're actionable differently: billing_error -> billing_required,
    permission_error -> authentication (same bucket as an invalid key --
    both mean "this credential can't do this," just for different reasons).
    """
    kind = "generic"
    message = ""
    try:
        body = json.loads(raw_body) if raw_body else {}
        err = body.get("error", body) if isinstance(body, dict) else {}
        err_type = str(err.get("type") or "")
        message = str(err.get("message") or "")
        if err_type == "rate_limit_error" or status_code == 429:
            kind = "rate_limited"
        elif err_type == "billing_error" or status_code == 402:
            kind = "billing_required"
        elif err_type in ("authentication_error", "permission_error") or status_code in (401, 403):
            kind = "authentication"
    except Exception:
        pass
    return {"kind": kind, "message": message}


def format_anthropic_error(status_code, raw_body: str, fallback: str) -> str:
    """Build the single error string chat()/chat_stream() raise on an HTTP
    error -- same output shape as lmstudio.py's format_provider_error() /
    gemini_backend.py's format_gemini_error(), so a caller doesn't need to
    special-case which backend produced it: "Anthropic error (<kind>, HTTP
    <status>): <detail>"."""
    info = classify_anthropic_error(status_code, raw_body)
    detail = info["message"] or (raw_body[:500] if raw_body else fallback)
    return f"Anthropic error ({info['kind']}, HTTP {status_code}): {detail}"


class AnthropicBackend(BaseLLMBackend):
    """Native Claude backend — x-api-key auth, /v1/messages endpoint."""

    name = "anthropic"
    display_name = "Anthropic (Claude)"
    default_url = API_BASE  # not user-editable; kept for UI consistency with other backends

    def __init__(self, base_url: str = None, api_key: Optional[str] = None):
        # base_url is accepted for interface parity with other backends but ignored —
        # Anthropic's endpoint is fixed, unlike self-hosted/custom backends.
        configured_key = getattr(config, "ANTHROPIC_API_KEY", "") if api_key is None else api_key
        self.api_key = configured_key.strip()
        self.default_model = getattr(config, "ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-4-6")
        self.headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        self.timeout = getattr(config, "TOOL_CALL_TIMEOUT", 600)

    # ------------------------------------------------------------------
    # Health / model listing
    # ------------------------------------------------------------------

    def health_check(self):
        # Matches groq.py / openrouter.py convention exactly: cloud backends report
        # "Configured" on key presence rather than burning a live API call just to
        # populate a settings-panel status line.
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY not set in config.py"
        return True, f"Configured — {self.default_model}"

    def get_model(self):
        return self.default_model

    def list_models(self):
        """Compatibility output: live IDs, else explicit offline suggestions."""
        result = self.discover_models()
        if result.outcome is ModelDiscoveryOutcome.SUCCESS:
            return list(result.models)
        return list(result.offline_suggestions)

    def discover_models(self) -> ModelDiscoveryResult:
        try:
            resp = requests.get(f"{API_ROOT}/models", headers=self.headers, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("invalid model-list response structure")
            models = tuple(
                entry["id"]
                for entry in payload["data"]
                if isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"].strip()
            )
        except Exception as exc:
            status = getattr(locals().get("resp"), "status_code", None)
            suffix = f" (HTTP {status})" if isinstance(status, int) else ""
            return ModelDiscoveryResult(
                ModelDiscoveryOutcome.FAILED,
                offline_suggestions=KNOWN_MODELS,
                diagnostic=(
                    f"Anthropic model discovery failed{suffix} "
                    f"({type(exc).__name__})."
                ),
            )
        if not models:
            return ModelDiscoveryResult(
                ModelDiscoveryOutcome.EMPTY,
                offline_suggestions=KNOWN_MODELS,
                diagnostic="Anthropic returned no usable models.",
            )
        return ModelDiscoveryResult(
            ModelDiscoveryOutcome.SUCCESS,
            models=models,
            offline_suggestions=KNOWN_MODELS,
            diagnostic=f"Anthropic returned {len(models)} model(s).",
        )

    def configured_model(self) -> Optional[str]:
        """
        Patch 3A.4 Part 4 override -- AnthropicBackend has NO self._model
        attribute at all (it uses self.default_model exclusively, unlike
        every LMStudioBackend-derived backend). The BaseLLMBackend default
        implementation reads self._model, which would silently return None
        here even though a real configured model exists -- that would
        silently break reasoning-preference restoration for Anthropic
        specifically. `or None` keeps the same falsy-safe contract as the
        base implementation even though default_model is never actually
        falsy in practice (always defaulted via getattr in __init__).
        """
        return self.default_model or None

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 2A -- reasoning-effort capability + wire translation
    # ------------------------------------------------------------------

    def reasoning_capabilities(self, model=None) -> ReasoningCapabilities:
        """
        See the _REASONING_MODELS module-level comment above: both
        "claude-sonnet-5" and "claude-sonnet-4-6" are confirmed, distinct
        entries with different effort matrices (claude-sonnet-4-6 does not
        support "xhigh"). `model=None` always falls through to
        NO_REASONING_CONTROL, matching the base class's documented
        contract (get_model() is not consulted here even though it would
        be side-effect-free for this backend, to keep the contract
        uniform across backends where it isn't).
        """
        if model is None:
            return NO_REASONING_CONTROL
        return _REASONING_MODELS.get(model, NO_REASONING_CONTROL)

    def _apply_reasoning_override(self, payload: dict, effort: str, model=None) -> None:
        """
        payload["output_config"]["effort"] = effort -- merged defensively:
        if `payload` already has an "output_config" dict (current
        _build_payload() never puts one there, but this must not assume
        that holds forever), any existing keys in it are preserved
        alongside the new "effort" key via setdefault() + in-place mutation
        rather than overwriting the whole object.

        Deliberately does NOT touch the separate Anthropic `thinking` field
        (extended thinking) -- effort is independent of that per the Part
        2A spec, and this backend doesn't enable extended thinking by
        default today anyway (see chat()'s disable_thinking comment).
        "adaptive" is never a member of either Sonnet capability's efforts, so it can
        never reach here via the validated `effort` param.
        """
        output_config = payload.setdefault("output_config", {})
        output_config["effort"] = effort

    # ------------------------------------------------------------------
    # Request translation: OpenAI-shaped tool registry -> Anthropic shape
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_tools(openai_tools):
        """
        ToolRegistry.get_schemas() hands every backend OpenAI-style tool defs:
            {"type": "function", "function": {"name", "description", "parameters"}}
        Anthropic wants:
            {"name", "description", "input_schema"}
        """
        if not openai_tools:
            return None
        translated = []
        for t in openai_tools:
            fn = t.get("function", t)  # tolerate already-flat input defensively
            translated.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return translated

    @staticmethod
    def _split_system(messages):
        """
        Anthropic takes `system` as a top-level string. Pull any role="system"
        messages out of the list and concatenate them (there's normally exactly one,
        but ctx.build_messages() / ephemeral injection could in theory produce more).
        Returns (system_str_or_None, remaining_messages).
        """
        system_parts = []
        remaining = []
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content", "")
                if isinstance(content, list):
                    # defensive: in case content is already block-structured
                    content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                system_parts.append(content)
            else:
                remaining.append(m)
        system_str = "\n\n".join(p for p in system_parts if p) or None
        return system_str, remaining

    @classmethod
    def _translate_messages(cls, messages):
        """
        Convert OpenAI-shaped conversation history (as built by context.py) into
        Anthropic's message format:

          - role="tool" messages -> role="user" message containing a tool_result block
          - assistant messages with tool_calls -> assistant message with tool_use blocks
          - plain text messages -> pass through with content as a string (Anthropic
            accepts both string and block-array content; string is fine for plain turns)

        Confirmed against context.py's add_tool_result(): tool messages are shaped
        {"role": "tool", "tool_call_id", "name", "content"}, and assistant tool_calls
        follow the OpenAI {"id", "type": "function", "function": {"name", "arguments"}}
        shape (same as what extract_message() below produces) — so the lookups here
        match the actual contract, not a guess.
        """
        out = []
        for m in messages:
            role = m.get("role")

            if role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id"),
                        "content": str(m.get("content", "")),
                    }],
                })
                continue

            if role == "assistant" and m.get("tool_calls"):
                blocks = []
                text = m.get("content")
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", tc)
                    try:
                        tool_input = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        tool_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id"),
                        "name": fn.get("name"),
                        "input": tool_input,
                    })
                out.append({"role": "assistant", "content": blocks})
                continue

            # plain user/assistant text turn
            out.append({"role": role, "content": m.get("content", "")})

        return out

    def _build_payload(self, messages, tools=None, max_tokens=4096, temperature=0.7, stream=False):
        system_str, convo = self._split_system(messages)
        payload = {
            "model": self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self._translate_messages(convo),
            "stream": stream,
        }
        if system_str:
            payload["system"] = system_str
        translated_tools = self._translate_tools(tools)
        if translated_tools:
            payload["tools"] = translated_tools
        return payload

    # ------------------------------------------------------------------
    # Non-streaming chat
    # ------------------------------------------------------------------

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None):
        # disable_thinking accepted for interface consistency with
        # complete_utility() but not acted on here for THINKING itself —
        # this backend doesn't enable Anthropic's extended-thinking mode by
        # default (see _build_payload), so the prefill-vs-thinking conflict
        # this param exists for doesn't apply. Worth a real look if
        # extended thinking ever gets wired in here — Anthropic's API has a
        # similar constraint around prefill and extended thinking. Still
        # routed through _effective_reasoning_effort() below so the
        # disable_thinking-wins precedence contract holds regardless.
        payload = self._build_payload(messages, tools, max_tokens, temperature, stream=False)
        # Patch 3A.4 Part 3 -- apply the already-verified native translation
        # (Part 2A's output_config.effort) after the payload is fully built,
        # before the HTTP call. self.default_model is a stable instance
        # attribute (not a method call) already reused consistently here.
        effective_effort = self._effective_reasoning_effort(reasoning_effort, disable_thinking)
        self.apply_reasoning(payload, effective_effort, model=self.default_model)
        try:
            resp = requests.post(API_BASE, headers=self.headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Anthropic API not reachable.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Anthropic API request timed out.")
        except requests.exceptions.HTTPError as e:
            # Was an unbounded, unredacted print of the raw body -- same
            # hygiene gap gemini_backend.py's equivalent print already
            # avoided (bounded to 500 chars there). Anthropic's error body
            # carries no thoughtSignature-class secret to redact, but an
            # arbitrarily large body still shouldn't dump in full.
            print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(format_anthropic_error(resp.status_code, resp.text, str(e)))

    def extract_message(self, response):
        """
        Normalize an Anthropic /v1/messages response back into the OpenAI-shaped
        dict the rest of agent.py expects (mirrors what LMStudioBackend.extract_message
        hands back): {"role", "content", "tool_calls": [...] or omitted}.
        """
        content_blocks = response.get("content", [])
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            # "thinking" blocks deliberately excluded from extract_message's content —
            # they're surfaced via chat_stream's think sentinels instead, not here.

        message = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7,
                     reasoning_effort: Optional[str] = None):
        """
        Yields plain text chunks, wrapping extended-thinking content in
        __THINK_START__ / __THINK_END__ sentinels — same convention lmstudio.py
        uses for reasoning_content/<think> tags.

        NOTE: base.py's chat_stream signature carries no `tools` param — confirmed
        against the live abstract method. Tool-calling only ever happens through
        the non-streaming chat() path; agent.py's tool loop presumably calls chat()
        when tools are in play and only reaches for chat_stream() on the final,
        tool-free turn. That matches how OpenAI-shaped streaming works too (tool
        call deltas exist in principle, but this codebase's contract doesn't route
        through chat_stream for them), so there is no tool_use handling needed
        here at all — content_block_start/delta/stop for tool_use blocks simply
        won't occur on a request built without `tools` in the payload.
        """
        payload = {
            "model": self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        system_str, convo = self._split_system(messages)
        payload["messages"] = self._translate_messages(convo)
        if system_str:
            payload["system"] = system_str

        # Patch 3A.4 Part 3 -- same native translation as chat() above, no
        # disable_thinking on this signature so no precedence guard needed.
        self.apply_reasoning(payload, reasoning_effort, model=self.default_model)

        try:
            resp = requests.post(
                API_BASE, headers=self.headers, json=payload, timeout=self.timeout, stream=True
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Anthropic API not reachable.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Anthropic API request timed out.")
        except requests.exceptions.HTTPError as e:
            # This branch didn't exist before -- an HTTP error on the
            # streaming path (rate limited, overloaded, billing/permission
            # failure -- the exact class of failure chat()'s non-streaming
            # path already handles above) used to leak as a raw requests.
            # exceptions.HTTPError instead of the RuntimeError chat() raises.
            # core/agent.py's _stream_final() only catches (ConnectionError,
            # TimeoutError, RuntimeError, ValueError) for its graceful
            # "[Stream error: ...]" handling -- an uncaught HTTPError skipped
            # that path entirely and fell through to a cruder top-level
            # handler instead. Same shape as chat()'s handling, just also
            # applied here.
            print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(format_anthropic_error(resp.status_code, resp.text, str(e)))

        thinking_open = False

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            # Same fix as gemini_backend.py's chat_stream(): Anthropic's SSE
            # stream also has no charset param on its Content-Type
            # (text/event-stream), so requests.Response.encoding defaults to
            # ISO-8859-1 and decode_unicode=True would mangle multi-byte
            # UTF-8 text. Decode explicitly instead.
            raw_line = raw_line.decode("utf-8")
            if not raw_line.startswith("data:"):
                continue
            data_str = raw_line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "content_block_delta":
                delta = event["delta"]
                dtype = delta.get("type")

                if dtype == "text_delta":
                    yield delta.get("text", "")

                elif dtype == "thinking_delta":
                    if not thinking_open:
                        yield "__THINK_START__"
                        thinking_open = True
                    yield delta.get("thinking", "")

            elif etype == "content_block_stop":
                if thinking_open:
                    yield "__THINK_END__"
                    thinking_open = False

            elif etype == "message_stop":
                break

        # safety net: close an unterminated thinking block if the stream ended mid-block
        if thinking_open:
            yield "__THINK_END__"
