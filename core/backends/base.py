"""
BaseLLMBackend — abstract interface all LLM backends must implement.
"""

import re
from abc import ABC, abstractmethod
from typing import Optional, Generator


class BaseLLMBackend(ABC):

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
             disable_thinking: bool = False) -> dict:
        """
        Non-streaming chat. Used for tool call turns.
        Returns raw response dict with OpenAI-compatible shape.
        disable_thinking: best-effort hint for backends whose server
        rejects (or silently mishandles) an assistant-prefill turn while
        thinking/reasoning mode is active — see complete_utility() below,
        which is the only caller that sets this True. Backends that don't
        have this failure mode may ignore it.
        """
        ...

    @abstractmethod
    def chat_stream(self, messages: list, max_tokens: int = 1024,
                    temperature: float = 0.7) -> Generator[str, None, None]:
        """
        Streaming chat. Yields text chunks + think markers.
        Special yields: '__THINK_START__', '__THINK_END__'
        """
        ...

    # --- Helpers (concrete, shared across all backends) ---

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