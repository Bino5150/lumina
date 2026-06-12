"""
BaseLLMBackend — abstract interface all LLM backends must implement.
"""

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
             temperature: float = 0.7, max_tokens: int = 1024) -> dict:
        """
        Non-streaming chat. Used for tool call turns.
        Returns raw response dict with OpenAI-compatible shape.
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