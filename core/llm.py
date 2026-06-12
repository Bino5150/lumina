"""
LM Studio API Client
Handles all communication with LM Studio's OpenAI-compatible endpoint.
Supports both streaming and non-streaming responses.
"""

import json
import requests
from typing import Optional, Generator
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class LMStudioClient:
    def __init__(self):
        self.base_url = config.LM_STUDIO_BASE_URL
        self.api_key = config.LM_STUDIO_API_KEY
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        self._model = config.DEFAULT_MODEL

    def get_model(self) -> str:
        if self._model:
            return self._model
        try:
            resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=5)
            models = resp.json().get("data", [])
            if models:
                self._model = models[0]["id"]
                return self._model
        except Exception as e:
            raise ConnectionError(f"Cannot reach LM Studio at {self.base_url}: {e}")
        raise ConnectionError("No models loaded in LM Studio.")

    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.7, max_tokens: int = 1024) -> dict:
        """Non-streaming chat — used for tool call turns (tools require non-streaming)."""
        payload = {
            "model": self.get_model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        has_vision = any(
            isinstance(m.get("content"), list) for m in messages
        )
        if tools and not has_vision:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"  
        
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"LM Studio not reachable at {self.base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError("LM Studio request timed out.")
        except requests.exceptions.HTTPError as e:       
            print(f"[HTTP ERROR BODY] {resp.text}", flush=True)
            raise RuntimeError(f"LM Studio HTTP error: {e}")

    def chat_stream(self, messages: list, max_tokens: int = 1024,
                    temperature: float = 0.7) -> Generator[str, None, None]:
        """
        Streaming chat — yields text chunks as they arrive.
        Used for the final response turn (no tools on final turn).
        Yields special markers:
          '__THINK_START__' / '__THINK_END__' around <think> content
          text chunks for regular content
        """
        payload = {
            "model": self.get_model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"LM Studio not reachable at {self.base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError("LM Studio request timed out.")

        buffer = ""
        in_think = False

        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8")
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    reasoning = delta.get("reasoning_content", "") or delta.get("thinking", "")
                    if reasoning:
                        if not in_think:
                            in_think = True
                            yield "__THINK_START__"
                        yield reasoning
                        continue
                    
                    token = delta.get("content", "")
                    if not token:
                        continue
                    if in_think:                
                        in_think = False
                        yield "__THINK_END__"

                    buffer += token

                    # Detect and emit think block markers
                    while True:
                        if not in_think:
                            think_start = buffer.find("<think>")
                            if think_start == -1:
                                # No think block — emit everything except possible partial tag
                                safe = buffer[:-8] if len(buffer) > 8 else ""
                                if safe:
                                    yield safe
                                    buffer = buffer[len(safe):]
                                break
                            else:
                                # Emit text before think block
                                if think_start > 0:
                                    yield buffer[:think_start]
                                buffer = buffer[think_start + 7:]  # skip <think>
                                in_think = True
                                yield "__THINK_START__"
                        else:
                            think_end = buffer.find("</think>")
                            if think_end == -1:
                                # Still inside think — emit think content
                                safe = buffer[:-9] if len(buffer) > 9 else ""
                                if safe:
                                    yield safe
                                    buffer = buffer[len(safe):]
                                break
                            else:
                                # Emit remaining think content then close
                                if think_end > 0:
                                    yield buffer[:think_end]
                                buffer = buffer[think_end + 8:]  # skip </think>
                                in_think = False
                                yield "__THINK_END__"

                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        # Flush remaining buffer
        if buffer.strip():
            if in_think:
                yield buffer
                yield "__THINK_END__"
            else:
                yield buffer

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
        fn = tool_call["function"]
        name = fn["name"]
        try:
            args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
        except json.JSONDecodeError:
            args = {}
        return name, args

    def test_connection(self) -> str:
        model = self.get_model()
        return f"Connected — model: {model}"
