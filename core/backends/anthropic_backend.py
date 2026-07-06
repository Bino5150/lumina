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

import config
from core.backends.base import BaseLLMBackend

ANTHROPIC_VERSION = "2023-06-01"  # required header, independent of model version
API_BASE = "https://api.anthropic.com/v1/messages"


class AnthropicBackend(BaseLLMBackend):
    """Native Claude backend — x-api-key auth, /v1/messages endpoint."""

    name = "anthropic"
    display_name = "Anthropic (Claude)"
    default_url = API_BASE  # not user-editable; kept for UI consistency with other backends

    def __init__(self, base_url: str = None):
        # base_url is accepted for interface parity with other backends but ignored —
        # Anthropic's endpoint is fixed, unlike self-hosted/custom backends.
        self.api_key = getattr(config, "ANTHROPIC_API_KEY", "").strip()
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
        # Static list — Anthropic's /v1/models endpoint exists but pinning to known-good
        # model strings avoids surfacing deprecated/preview names Lumina can't actually use.
        return [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]

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
             disable_thinking: bool = False):
        # disable_thinking accepted for interface consistency with
        # complete_utility() but not acted on here — this backend doesn't
        # enable Anthropic's extended-thinking mode by default (see
        # _build_payload), so the prefill-vs-thinking conflict this param
        # exists for doesn't apply. Worth a real look if extended thinking
        # ever gets wired in here — Anthropic's API has a similar
        # constraint around prefill and extended thinking.
        payload = self._build_payload(messages, tools, max_tokens, temperature, stream=False)
        try:
            resp = requests.post(API_BASE, headers=self.headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Anthropic API not reachable.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Anthropic API request timed out.")
        except requests.exceptions.HTTPError as e:
            print(f"[HTTP ERROR BODY] {resp.text}", flush=True)
            raise RuntimeError(f"Anthropic API HTTP error: {e}")

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

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7):
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

        try:
            resp = requests.post(
                API_BASE, headers=self.headers, json=payload, timeout=self.timeout, stream=True
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Anthropic API not reachable.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Anthropic API request timed out.")

        thinking_open = False

        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
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
