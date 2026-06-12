"""
LM Studio backend — OpenAI-compatible local inference server.
Default port: 1234
"""

import json
import requests
from typing import Optional, Generator
from .base import BaseLLMBackend
import config


class LMStudioBackend(BaseLLMBackend):

    name = "lmstudio"
    display_name = "LM Studio"
    default_url = "http://localhost:1234/v1"

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or config.LLM_BACKEND_URL or self.default_url).rstrip("/")
        self.api_key = "lm-studio"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
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

    def list_models(self) -> list[str]:
        try:
            resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=5)
            return [m["id"] for m in resp.json().get("data", [])]
        except Exception:
            return []

    def health_check(self) -> tuple[bool, str]:
        try:
            model = self.get_model()
            return True, f"Connected — {model}"
        except Exception as e:
            return False, str(e)

    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.7, max_tokens: int = 4096) -> dict:
        payload = {
            "model": self.get_model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        has_vision = any(isinstance(m.get("content"), list) for m in messages)
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

    def chat_stream(self, messages: list, max_tokens: int = 4096,
                    temperature: float = 0.7) -> Generator[str, None, None]:
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
                    finish_reason = chunk["choices"][0].get("finish_reason")
                    if finish_reason in ("stop", "length", "eos"):
                        if buffer:
                            yield buffer
                            buffer = ""
                        if in_think:
                            yield "__THINK_END__"
                        break

                    # reasoning_content field (Qwen3, DeepSeek-R1 style)
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

                    while True:
                        if not in_think:
                            think_start = buffer.find("<think>")
                            if think_start == -1:
                                safe = buffer[:-8] if len(buffer) > 8 else ""
                                if safe:
                                    yield safe
                                    buffer = buffer[len(safe):]
                                break
                            else:
                                if think_start > 0:
                                    yield buffer[:think_start]
                                buffer = buffer[think_start + 7:]
                                in_think = True
                                yield "__THINK_START__"
                        else:
                            think_end = buffer.find("</think>")
                            if think_end == -1:
                                safe = buffer[:-9] if len(buffer) > 9 else ""
                                if safe:
                                    yield safe
                                    buffer = buffer[len(safe):]
                                break
                            else:
                                if think_end > 0:
                                    yield buffer[:think_end]
                                buffer = buffer[think_end + 8:]
                                in_think = False
                                yield "__THINK_END__"

                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        if buffer.strip():
            if in_think:
                yield buffer
                yield "__THINK_END__"
            else:
                yield buffer