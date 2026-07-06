"""
core/backends/gemini_backend.py

Native Google Gemini backend for Lumina.

Like anthropic_backend.py, this does NOT subclass LMStudioBackend — Gemini's wire
format is its own thing, different from both OpenAI's shape and Anthropic's shape:

  - Auth header is `x-goog-api-key`, not `Authorization: Bearer` or `x-api-key`
  - Model is part of the URL path, not a JSON body field:
        POST /v1beta/models/{model}:generateContent
        POST /v1beta/models/{model}:streamGenerateContent?alt=sse
  - No "messages" array — uses "contents": [{"role", "parts": [...]}], where each
    Part is {"text": ...} or {"functionCall": ...} or {"functionResponse": ...}
  - Assistant role is called "model", not "assistant"
  - System prompt is "system_instruction": {"parts": [{"text": ...}]} — a top-level
    field like Anthropic's `system`, but nested one level deeper (parts, not a
    bare string)
  - Tools are "tools": [{"functionDeclarations": [...]}] — note the schema field
    is "parameters" (same key OpenAI uses), so no key-rename needed there, just a
    different wrapper shape and a different top-level container name
  - Tool calls arrive as a "functionCall": {"name", "args"} Part in the response,
    not a separate array alongside content
  - Tool results go back as a "functionResponse": {"name", "response"} Part inside
    a "user"-role content turn — note "response" wants a dict, not a raw string,
    so a plain string tool result gets wrapped as {"result": "..."} 
  - Streaming with ?alt=sse yields one full GenerateContentResponse JSON object
    per SSE "data:" line (not incremental token deltas with typed event names
    the way Anthropic's stream works) — each chunk's candidates[0].content.parts
    contains whatever text/functionCall arrived since the last chunk
  - Gemini has no equivalent of extended-thinking sentinel content in the public
    API surface used here, so __THINK_START__/__THINK_END__ are never emitted by
    this backend. (Gemini "thinking"/reasoning tokens, where supported, are not
    exposed as separate streamable content in the generateContent surface as of
    this writing — if that changes, this is the file to revisit.)

Confirmed against base.py: chat_stream() takes (messages, max_tokens, temperature)
with no tools param — same contract Anthropic's backend was built against. Tool
calls only flow through non-streaming chat().

Inherits BaseLLMBackend directly and implements the contract natively.
"""

import json
import requests

import config
from core.backends.base import BaseLLMBackend

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class GeminiBackend(BaseLLMBackend):
    """Native Gemini backend — x-goog-api-key auth, model-in-URL endpoint."""

    name = "gemini"
    display_name = "Gemini (Google)"
    default_url = API_ROOT  # fixed endpoint, not user-editable — kept for UI parity

    def __init__(self, base_url: str = None):
        # base_url accepted for interface parity with other backends but ignored,
        # same convention as AnthropicBackend.
        self.api_key = getattr(config, "GEMINI_API_KEY", "").strip()
        self.default_model = getattr(config, "GEMINI_DEFAULT_MODEL", "gemini-3.5-flash")
        self.headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        self.timeout = getattr(config, "TOOL_CALL_TIMEOUT", 600)

    # ------------------------------------------------------------------
    # Health / model listing
    # ------------------------------------------------------------------

    def health_check(self):
        # Matches groq.py / openrouter.py / anthropic_backend.py convention:
        # report configured state on key presence, no live API ping.
        if not self.api_key:
            return False, "GEMINI_API_KEY not set in config.py"
        return True, f"Configured — {self.default_model}"

    def get_model(self):
        return self.default_model

    def list_models(self):
        # Static known-good list, same rationale as anthropic_backend.py: Gemini's
        # /v1beta/models list endpoint exists but includes tuned/deprecated/preview
        # entries Lumina can't necessarily use. Verify against a live `curl
        # {API_ROOT}/models?key=...` before wiring into the Settings dropdown —
        # model naming on this surface (gemini-3.5-flash family per current docs)
        # moves faster than Anthropic's or OpenAI's.
        return [
            "gemini-3.5-pro",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
        ]

    # ------------------------------------------------------------------
    # Request translation: OpenAI-shaped tool registry -> Gemini shape
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_tools(openai_tools):
        """
        ToolRegistry.get_schemas() hands every backend OpenAI-style tool defs:
            {"type": "function", "function": {"name", "description", "parameters"}}
        Gemini wants:
            {"functionDeclarations": [{"name", "description", "parameters"}]}
        Unlike Anthropic, the schema field name itself ("parameters") is unchanged —
        only the wrapper shape differs.
        """
        if not openai_tools:
            return None
        declarations = []
        for t in openai_tools:
            fn = t.get("function", t)
            declarations.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return [{"functionDeclarations": declarations}] if declarations else None

    @staticmethod
    def _split_system(messages):
        """
        Gemini takes system instructions as a top-level "system_instruction"
        field shaped {"parts": [{"text": ...}]}. Pull role="system" messages out
        of the list (context.py's build_messages() always puts exactly one at
        index 0, but this handles the general case same as Anthropic's version).
        Returns (system_instruction_dict_or_None, remaining_messages).
        """
        system_parts = []
        remaining = []
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                system_parts.append(content)
            else:
                remaining.append(m)
        joined = "\n\n".join(p for p in system_parts if p)
        system_instruction = {"parts": [{"text": joined}]} if joined else None
        return system_instruction, remaining

    @classmethod
    def _translate_messages(cls, messages):
        """
        Convert OpenAI-shaped conversation history into Gemini's "contents" array.

          - role="tool" -> role="user" content with a functionResponse part.
            context.py's add_tool_result() shape is {"role": "tool", "tool_call_id",
            "name", "content"} — Gemini's functionResponse wants {"name", "response"}
            where response is a dict, so the plain string content gets wrapped.
            NOTE: Gemini's functionResponse has no id/tool_call_id field at all —
            matching is implicitly by name + turn order, not an explicit call id.
            This is a real structural difference from both OpenAI and Anthropic,
            which both thread an explicit id through. If a single assistant turn
            ever issues two parallel calls to the *same* tool name, Gemini has no
            built-in way to disambiguate which result pairs with which call beyond
            sequence — noting this since it's a real (if narrow) limitation of the
            wire format itself, not a gap in this translation layer.
          - role="assistant" with tool_calls -> role="model" content with one or
            more functionCall parts (plus a text part if there's also plain text).
          - role="user"/"assistant" plain text -> role="user"/"model" with a single
            text part.
        """
        out = []
        for m in messages:
            role = m.get("role")

            if role == "tool":
                out.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": m.get("name"),
                            "response": {"result": str(m.get("content", ""))},
                        }
                    }],
                })
                continue

            if role == "assistant" and m.get("tool_calls"):
                parts = []
                text = m.get("content")
                if text:
                    parts.append({"text": text})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", tc)
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
                out.append({"role": "model", "parts": parts})
                continue

            gemini_role = "model" if role == "assistant" else "user"
            out.append({"role": gemini_role, "parts": [{"text": m.get("content", "") or ""}]})

        return out

    def _build_payload(self, messages, tools=None, max_tokens=1024, temperature=0.7):
        system_instruction, convo = self._split_system(messages)
        payload = {
            "contents": self._translate_messages(convo),
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction
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
        # complete_utility() but not acted on here — same reasoning as
        # AnthropicBackend: this backend doesn't enable Gemini's thinking
        # mode by default, so the conflict doesn't apply today.
        payload = self._build_payload(messages, tools, max_tokens, temperature)
        url = f"{API_ROOT}/models/{self.default_model}:generateContent"
        try:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Gemini API not reachable.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Gemini API request timed out.")
        except requests.exceptions.HTTPError as e:
            print(f"[HTTP ERROR BODY] {resp.text}", flush=True)
            raise RuntimeError(f"Gemini API HTTP error: {e}")

    def extract_message(self, response):
        """
        Normalize a Gemini generateContent response back into the OpenAI-shaped
        dict the rest of agent.py expects: {"role", "content", "tool_calls": [...]
        or omitted} — same target shape anthropic_backend.py produces.
        """
        try:
            parts = response["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            return {"role": "assistant", "content": ""}

        text_parts = []
        tool_calls = []

        for i, part in enumerate(parts):
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    # Gemini doesn't issue an id for function calls the way OpenAI/
                    # Anthropic do. Synthesizing a stable-within-this-message id so
                    # downstream code that expects every tool_call to have one
                    # (parse_tool_call, add_tool_result threading) doesn't choke.
                    # This id only needs to round-trip within a single turn, which
                    # it does — it's never sent back to Gemini itself (see
                    # _translate_messages' functionResponse, which keys by name).
                    "id": f"gemini_call_{i}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name"),
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                })

        message = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7):
        """
        Yields plain text chunks. No tools param — confirmed against base.py,
        same contract as anthropic_backend.py's chat_stream(). Gemini's streaming
        surface (generateContent with ?alt=sse) sends one full
        GenerateContentResponse JSON per SSE data line, not incremental named
        delta events — so unlike Anthropic's typed-event parser, this just reads
        candidates[0].content.parts[].text off each chunk and yields whatever's new.

        No __THINK_START__/__THINK_END__ sentinels are emitted — see module
        docstring for why.
        """
        payload = self._build_payload(messages, tools=None, max_tokens=max_tokens, temperature=temperature)
        url = f"{API_ROOT}/models/{self.default_model}:streamGenerateContent"

        try:
            resp = requests.post(
                url,
                headers=self.headers,
                params={"alt": "sse"},
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Gemini API not reachable.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Gemini API request timed out.")

        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue
            data_str = raw_line[len("data:"):].strip()
            if not data_str:
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            try:
                parts = chunk["candidates"][0]["content"]["parts"]
            except (KeyError, IndexError):
                continue

            for part in parts:
                text = part.get("text")
                if text:
                    yield text
