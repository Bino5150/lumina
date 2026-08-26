"""
LM Studio backend — OpenAI-compatible local inference server.
Default port: 1234
"""

import json
import requests
from typing import Optional, Generator
from .base import BaseLLMBackend
import config


def validate_base_url(base_url: str, provider: str) -> str:
    """Normalize and validate a configured OpenAI-compatible base URL.

    Without this, an empty or schemeless base_url (e.g. a 'custom' backend
    selected before its URL field has ever been saved) surfaces as requests'
    own cryptic MissingSchema error — "Invalid URL '/models': No scheme
    supplied. Perhaps you meant https:///models?" — with no indication of
    which backend or setting is actually at fault. Raising here, before any
    network call, gives a clear message naming the actual provider instead.
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError(f"{provider}: no endpoint URL configured.")
    if "://" not in base_url:
        raise ValueError(
            f"{provider}: endpoint URL '{base_url}' is missing a scheme "
            f"(expected e.g. https://... or http://...)."
        )
    return base_url


def join_endpoint(base_url: str, path: str) -> str:
    """Join an already-validated base_url with a path, without duplicating
    the path if base_url already ends with it (e.g. someone pastes the full
    '.../chat/completions' URL into a base-url field meant to hold just the
    '.../v1' root)."""
    path = path.lstrip("/")
    if base_url.endswith("/" + path):
        return base_url
    return f"{base_url}/{path}"


def classify_provider_error(status_code, raw_body: str) -> dict:
    """Best-effort classification of an OpenAI-compatible provider's error
    response, so billing/quota/auth failures don't all collapse into one
    indistinguishable 'HTTP error' (see
    LUMINA_PROVIDER_BRIDGE_ADDENDUM_2026-08-01.md — OpenCode Zen's 401
    CreditsError is a payment-method requirement, not an invalid key).
    Returns {"kind", "message"}; falls back to "generic" / "" on anything
    that doesn't parse or match a known shape. Never raises.
    """
    kind = "generic"
    message = ""
    try:
        body = json.loads(raw_body) if raw_body else {}
        err = body.get("error", body) if isinstance(body, dict) else {}
        if isinstance(err, dict):
            err_type = str(err.get("type") or "")
            message = str(err.get("message") or "")
        else:
            err_type = ""
        haystack = f"{err_type} {message}".lower()
        # Message-based signals first: providers reuse the same HTTP status
        # (e.g. 402) for genuinely different problems, so an explicit
        # "quota" or "payment"/CreditsError signal in the body should win
        # over a generic status-code guess, not lose to one.
        if err_type == "CreditsError" or "payment" in haystack:
            kind = "billing_required"
        elif "quota" in haystack:
            kind = "insufficient_quota"
        elif status_code == 402:
            kind = "billing_required"
        elif status_code == 401:
            kind = "authentication"
        elif status_code == 429:
            kind = "rate_limited"
    except Exception:
        pass
    return {"kind": kind, "message": message}


def format_provider_error(provider: str, status_code, raw_body: str, fallback: str) -> str:
    """Build the single error string chat()/chat_stream() raise on an HTTP
    error — names the actual provider (not a hardcoded backend name),
    includes the normalized classification, and preserves the provider's
    own message (or a bounded raw-body preview if the body didn't parse as
    the expected {"error": {...}} shape) instead of only the generic
    requests status line."""
    info = classify_provider_error(status_code, raw_body)
    detail = info["message"] or (raw_body[:500] if raw_body else fallback)
    return f"{provider} error ({info['kind']}, HTTP {status_code}): {detail}"


def _iter_lines_safe(resp):
    """FE-15: requests.exceptions.RequestException (e.g. ChunkedEncodingError
    from a mid-stream disconnect) raises from *inside* iter_lines(), outside
    the connect-time try/except in chat_stream(). It's also not a subclass
    of the builtin ConnectionError/TimeoutError that core/agent.py's
    _stream_final() catches, so it used to escape both layers -- the turn's
    partial content got dropped from history and a raw exception surfaced
    instead of the graceful "[Stream error: ...]" path. Converting here
    keeps chat_stream()'s main loop untouched.
    """
    try:
        yield from resp.iter_lines()
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"LM Studio stream interrupted: {e}")


class LMStudioBackend(BaseLLMBackend):

    name = "lmstudio"
    display_name = "LM Studio"
    default_url = "http://localhost:1234/v1"
    endpoint_configurable = True

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url
        self.api_key = "lm-studio"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = config.DEFAULT_MODEL

    def get_model(self) -> str:
        if self._model:
            return self._model
        base_url = validate_base_url(self.base_url, self.display_name)
        try:
            resp = requests.get(join_endpoint(base_url, "models"), headers=self.headers, timeout=5)
            models = resp.json().get("data", [])
            if models:
                self._model = models[0]["id"]
                return self._model
        except Exception as e:
            raise ConnectionError(f"Cannot reach {self.display_name} at {base_url}: {e}")
        raise ConnectionError(f"No models loaded in {self.display_name}.")

    def list_models(self) -> list[str]:
        try:
            base_url = validate_base_url(self.base_url, self.display_name)
            resp = requests.get(join_endpoint(base_url, "models"), headers=self.headers, timeout=5)
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
             temperature: float = 0.7, max_tokens: int = 4096,
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None) -> dict:
        model = self.get_model()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        has_vision = any(isinstance(m.get("content"), list) for m in messages)
        if tools and not has_vision:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # Patch 3A.4 Part 3 -- generic polymorphic reasoning translation.
        # This does NOT give LMStudioBackend itself reasoning semantics --
        # its own reasoning_capabilities() stays NO_REASONING_CONTROL
        # (untouched), so apply_reasoning() is a no-op here. But self.
        # reasoning_capabilities(model) is an instance-method call, so on
        # OpenAIBackend/GroqBackend/OpenRouterBackend/QwenBackend (all of
        # which inherit this chat() unmodified and override
        # reasoning_capabilities()/_apply_reasoning_override() themselves)
        # it resolves to each subclass's own real capability data and wire
        # translation -- zero provider conditionals added here.
        effective_effort = self._effective_reasoning_effort(reasoning_effort, disable_thinking)
        self.apply_reasoning(payload, effective_effort, model=model)

        # S41 correction: complete_utility() briefly dropped these when it
        # replaced dreaming.py/auto-naming's old bespoke requests.post()
        # calls, on the assumption that assistant-prefill alone (the S23
        # anti-bleed fix) was sufficient on its own. It wasn't — this
        # server explicitly 400s ("Assistant response prefill is
        # incompatible with enable_thinking") when a prefilled assistant
        # turn is sent while thinking is still enabled. The prefill and
        # this flag were never redundant; they were always working
        # together. Restored as an explicit opt-in param instead of
        # silently baked into every call, so normal tool-calling turns
        # (which never prefill) are unaffected.
        if disable_thinking:
            payload["thinking"] = {"type": "disabled"}
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        base_url = validate_base_url(self.base_url, self.display_name)
        try:
            resp = requests.post(
                join_endpoint(base_url, "chat/completions"),
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"{self.display_name} not reachable at {base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError(f"{self.display_name} request timed out.")
        except requests.exceptions.HTTPError as e:
            print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(format_provider_error(self.display_name, resp.status_code, resp.text, str(e)))

    def chat_stream(self, messages: list, max_tokens: int = 4096,
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
        # No disable_thinking on this signature (never had one) -- forward
        # reasoning_effort straight through, no precedence guard needed.
        self.apply_reasoning(payload, reasoning_effort, model=model)
        base_url = validate_base_url(self.base_url, self.display_name)
        try:
            resp = requests.post(
                join_endpoint(base_url, "chat/completions"),
                headers=self.headers, json=payload,
                timeout=config.TOOL_CALL_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"{self.display_name} not reachable at {base_url}.")
        except requests.exceptions.Timeout:
            raise TimeoutError(f"{self.display_name} request timed out.")
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(format_provider_error(self.display_name, resp.status_code, resp.text, str(e)))

        buffer = ""
        in_think = False

        for line in _iter_lines_safe(resp):
            if not line:
                continue
            line = line.decode("utf-8")
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choices = chunk.get("choices") or []
                    if not choices:
                        # Some providers (e.g. OpenCode Zen) send a trailing
                        # metadata frame with empty choices and top-level
                        # usage/cost fields — expected, not an error.
                        continue
                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason")
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

                    # Guard against a non-string content shape rather than
                    # assuming delta["content"] is always str|None|missing —
                    # some providers use structured content parts here.
                    # `not token` alone would let a truthy non-string value
                    # (e.g. a list) reach `buffer += token` below and crash.
                    token = delta.get("content")
                    if not isinstance(token, str) or not token:
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

                except (json.JSONDecodeError, KeyError, IndexError) as e:
                    # Structure only — never the frame's own text — so this
                    # stays useful for debugging a stream-shape mismatch
                    # without logging assistant output or private content.
                    print(f"[{self.display_name} STREAM] skipped malformed "
                          f"frame ({type(e).__name__}), len={len(data)}", flush=True)
                    continue

        if buffer.strip():
            if in_think:
                yield buffer
                yield "__THINK_END__"
            else:
                yield buffer
