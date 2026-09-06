"""
Ollama backend — hits Ollama's OpenAI-compatible endpoint (/v1/).
Default port: 11434
Tool calls and streaming use the same OpenAI-compat surface,
so this adapter is nearly identical to LM Studio with different
defaults and no api_key requirement.
"""

import json
import requests
from typing import Optional, Generator
from .base import BaseLLMBackend, ModelDiscoveryResult, ToolChoiceMode
from .lmstudio import discover_openai_compatible_models, extract_openai_compatible_reasoning
import config


class OllamaBackend(BaseLLMBackend):

    name = "ollama"
    display_name = "Ollama"
    default_url = "http://localhost:11434/v1"
    endpoint_configurable = True

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url
        self.headers = {"Content-Type": "application/json"}
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
            raise ConnectionError(f"Cannot reach Ollama at {self.base_url}: {e}")
        raise ConnectionError("No models found in Ollama.")

    def list_models(self) -> list[str]:
        """Compatibility output: live IDs on success, otherwise empty."""
        return list(self.discover_models().models)

    def discover_models(self) -> ModelDiscoveryResult:
        return discover_openai_compatible_models(self, timeout=5)

    def health_check(self) -> tuple[bool, str]:
        try:
            model = self.get_model()
            return True, f"Connected — {model}"
        except Exception as e:
            return False, str(e)

    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.7, max_tokens: int = 1024,
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None,
             tool_choice_mode: Optional[ToolChoiceMode] = None) -> dict:
        model = self.get_model()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # AGENT-CONTINUATION-01B -- no local Ollama server has been
            # live-verified to accept "required" (supports_required_tool_
            # choice stays the base-class False), so this always resolves
            # to "auto" -- byte-identical to pre-01B behavior.
            resolved = self._resolve_tool_choice_mode(tool_choice_mode)
            payload["tool_choice"] = "required" if resolved == ToolChoiceMode.REQUIRED else "auto"
        # disable_thinking accepted for interface consistency with
        # complete_utility() but not yet acted on here — Ollama has its own
        # newer "think": false option for supporting models, but this
        # hasn't been verified against Lumina's actual Ollama usage. Not a
        # live bug today since llamacpp is the active local backend; worth
        # revisiting if/when Ollama becomes the daily driver and utility
        # calls start hitting the same prefill-vs-thinking conflict there.

        # Patch 3A.4 Part 3 -- reasoning_effort accepted for interface
        # consistency with every other backend's chat() (mechanical
        # signature widening) and routed through apply_reasoning() for
        # uniformity with LMStudio/Anthropic/Gemini's treatment -- but this
        # is always a no-op today: OllamaBackend.reasoning_capabilities()
        # is never overridden, so it stays NO_REASONING_CONTROL via the
        # base class default and apply_reasoning() never mutates payload.
        # No real Ollama reasoning-effort capability data exists yet (out
        # of scope for this slice, same as every other local backend) --
        # wiring the call through now costs nothing behaviorally and means
        # Ollama isn't the one asymmetric exception among the backends that
        # accept disable_thinking, but it does NOT give Ollama real
        # reasoning-effort semantics.
        effective_effort = self._effective_reasoning_effort(reasoning_effort, disable_thinking, model=model)
        self.apply_reasoning(payload, effective_effort, model=model)

        resp = None  # BACKEND-ERROR-01: bound-checkable for the HTTPError handler
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"Ollama not reachable at {self.base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Ollama request timed out.")
        except requests.exceptions.HTTPError as e:
            # Bounded -- was an unbounded raw-body print, same hygiene gap
            # anthropic_backend.py's equivalent had before this pass.
            # BACKEND-ERROR-01: an HTTPError that escaped the transport call
            # itself (custom adapter/hook/test double) has no response to
            # print -- str(e) below still carries the original failure.
            if resp is not None:
                print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(f"Ollama HTTP error: {e}")

    def extract_reasoning(self, response: dict) -> Optional[str]:
        """AGENT-TOOL-THINK-TELEMETRY-01A1 -- Ollama's /v1/ endpoint is the
        same OpenAI-compatible response shape LMStudioBackend's own
        override reads (module docstring: "nearly identical to LM Studio
        with different defaults"), but this class doesn't subclass
        LMStudioBackend, so it needs its own one-line delegation to the
        shared parser rather than inheriting it -- exactly the same
        relationship this file already has with discover_openai_
        compatible_models() above."""
        return extract_openai_compatible_reasoning(response)

    def chat_stream(self, messages: list, max_tokens: int = 1024,
                    temperature: float = 0.7,
                    reasoning_effort: Optional[str] = None) -> Generator[str, None, None]:
        model = self.get_model()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        # See chat()'s comment above -- always a no-op today (no disable_thinking
        # on this signature, matching the abstract contract, so no precedence guard needed).
        self.apply_reasoning(payload, reasoning_effort, model=model)
        resp = None  # BACKEND-ERROR-01: bound-checkable for the HTTPError handler
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"Ollama not reachable at {self.base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError("Ollama request timed out.")
        except requests.exceptions.HTTPError as e:
            # This branch didn't exist before -- same gap as gemini_backend.py
            # and anthropic_backend.py had: an HTTP error on the streaming
            # path (e.g. model not pulled, OOM) used to leak as a raw
            # requests.exceptions.HTTPError instead of the RuntimeError
            # chat() already raises for the identical failure. core/agent.py's
            # _stream_final() only catches (ConnectionError, TimeoutError,
            # RuntimeError, ValueError) for its graceful "[Stream error: ...]"
            # handling -- an uncaught HTTPError skipped that path entirely.
            # No quota/billing concept for a local backend, so unlike the two
            # cloud backends this stays unclassified, matching chat()'s own
            # existing style here.
            if resp is not None:
                print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(f"Ollama HTTP error: {e}")

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
                    token = delta.get("content", "")
                    if not token:
                        continue

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
