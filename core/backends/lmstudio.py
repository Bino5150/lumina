"""
LM Studio backend — OpenAI-compatible local inference server.
Default port: 1234
"""

import json
import requests
from typing import Optional, Generator
from .base import BaseLLMBackend, ModelDiscoveryOutcome, ModelDiscoveryResult, ToolChoiceMode
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


def discover_openai_compatible_models(
    backend: BaseLLMBackend,
    *,
    timeout: int = 10,
    offline_suggestions=(),
) -> ModelDiscoveryResult:
    """Enumerate a genuine OpenAI-compatible ``GET /models`` surface.

    The response must be HTTP-successful and shaped as ``{"data": list}``.
    Raw bodies and exception messages are deliberately excluded from the
    diagnostic because this result is rendered directly in Settings.
    """
    suggestions = tuple(offline_suggestions)
    try:
        base_url = validate_base_url(backend.base_url, backend.display_name)
        resp = requests.get(
            join_endpoint(base_url, "models"),
            headers=backend.headers,
            timeout=timeout,
        )
        raise_for_status = getattr(resp, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        elif getattr(resp, "status_code", 200) >= 400:
            raise requests.exceptions.HTTPError()
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
            offline_suggestions=suggestions,
            diagnostic=(
                f"{backend.display_name} model discovery failed{suffix} "
                f"({type(exc).__name__})."
            ),
        )

    if not models:
        return ModelDiscoveryResult(
            ModelDiscoveryOutcome.EMPTY,
            offline_suggestions=suggestions,
            diagnostic=f"{backend.display_name} returned no usable models.",
        )
    return ModelDiscoveryResult(
        ModelDiscoveryOutcome.SUCCESS,
        models=models,
        offline_suggestions=suggestions,
        diagnostic=f"{backend.display_name} returned {len(models)} model(s).",
    )


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


# AGENT-GLM-THINK-TOOL-TRANSITION-01 -- one shared alias-priority list for
# every OpenAI-compatible reasoning field name Lumina has live-verified
# across providers, in priority order, first non-empty string wins:
#   "reasoning_content" -- Qwen3/DeepSeek-R1-style local/self-hosted servers.
#   "reasoning" -- OpenRouter's own unified field. LIVE-VERIFIED 2026-08-28
#     (non-streaming) and again 2026-08-29 (STREAMING deltas, this ticket)
#     against the actual configured production route (openrouter /
#     z-ai/glm-5.3-flash): a real tool_calls-bearing response returns
#     message == {"role", "content": null, "refusal": null,
#     "reasoning": "<plain text>", "tool_calls": [...],
#     "reasoning_details": [...]} -- "reasoning" is a plain string sibling
#     of "tool_calls"/"content" in the SAME message object (non-streaming)
#     or the SAME delta object (streaming, alongside an empty "content").
#     "reasoning_details" (OpenRouter's structured/newer sibling field) is
#     deliberately NOT parsed here -- out of scope for this slice's
#     "surface only what's already a plain string" law.
#     Also confirmed live: reasoning is genuinely ABSENT (null) on some
#     otherwise-valid tool-calls-bearing turns from the same route (the
#     control-gate-shaped probe) -- callers must treat that as an
#     ordinary, expected "no reasoning this round" case, not an error.
#   "thinking" -- alternate name kept for the same symmetry.
_REASONING_FIELD_PRIORITY = ("reasoning_content", "reasoning", "thinking")


def _first_reasoning_field(source: dict, *, strip: bool = True) -> Optional[str]:
    """AGENT-GLM-THINK-TOOL-TRANSITION-01 -- shared alias-priority lookup
    used by both extract_openai_compatible_reasoning() below (a full
    non-streaming message dict, whole-text, stripped) and chat_stream()'s
    own per-frame delta lookup (a single streaming delta dict, one
    incremental token, never stripped).

    strip=False exists because a legitimate individual reasoning TOKEN can
    be pure whitespace (e.g. the single space between two words in the
    model's reasoning text, live-observed 2026-08-29) -- stripping a
    per-token fragment would silently eat that whitespace from the
    reconstructed Think text. Only the non-streaming whole-blob case wants
    a strip. This is the ONE difference from a bare shared constant: same
    alias set, same priority order, same isinstance/non-empty gate,
    different collapse-boundary semantics for the two call shapes.

    Every candidate is type-checked (isinstance str) before being trusted
    -- a non-string value (malformed/unexpected shape) is skipped, never
    coerced via str()/repr(). Returns the first candidate's text (stripped
    or not, per `strip`), or None if `source` isn't a dict or no candidate
    field is a non-empty string."""
    if not isinstance(source, dict):
        return None
    for key in _REASONING_FIELD_PRIORITY:
        value = source.get(key)
        if not isinstance(value, str):
            continue
        if strip:
            # Whole-blob shape: a whitespace-only value must be treated as
            # absent (fall through to the next alias), matching the
            # original non-streaming behavior exactly.
            stripped = value.strip()
            if stripped:
                return stripped
        elif value:
            # Per-token shape: only a truly empty string ("") is absent --
            # a whitespace-only token (e.g. a single " " between two words)
            # is real content and must be returned as-is, never stripped.
            return value
    return None


def extract_openai_compatible_reasoning(response: dict) -> Optional[str]:
    """AGENT-TOOL-THINK-TELEMETRY-01A1 -- passive reasoning extraction for
    every OpenAI-compatible-shaped non-streaming response: LMStudioBackend
    and every one of its descendants (LM Studio, DeepSeek, Groq, Kimi,
    llama.cpp, OmniRoute, OpenAI, OpenRouter, Qwen, vLLM, Custom) plus
    OllamaBackend, which shares this exact response shape but does not
    subclass LMStudioBackend (see ollama.py's own extract_reasoning()).

    See _first_reasoning_field()'s own docstring for the field priority
    and live-verification history; this wrapper just locates the message
    dict and requests the stripped (whole-blob) variant. Returns None if
    the response doesn't even parse to a choices[0].message shape, or no
    candidate field is a non-empty string -- both collapse to the same
    "nothing to surface" result.
    """
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    return _first_reasoning_field(message)


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
        """Compatibility output: live IDs on success, otherwise an empty list."""
        return list(self.discover_models().models)

    def discover_models(self) -> ModelDiscoveryResult:
        return discover_openai_compatible_models(self, timeout=5)

    def health_check(self) -> tuple[bool, str]:
        try:
            model = self.get_model()
            return True, f"Connected — {model}"
        except Exception as e:
            return False, str(e)

    def _apply_output_token_limit(self, payload: dict, max_tokens: int,
                                  model: Optional[str] = None) -> None:
        """Translate Lumina's provider-neutral output budget to the wire.

        OpenAI-compatible servers traditionally accept ``max_tokens``, so
        that remains the shared-family default. Concrete providers whose
        native Chat Completions contract uses a different field override
        this hook without leaking provider rules into LuminaAgent or sibling
        backends.
        """
        payload["max_tokens"] = max_tokens

    def _apply_disable_thinking(self, payload: dict) -> None:
        """
        LM Studio / local-server wire translation for the disable_thinking
        intent (UTILITY-RUNTIME-01). Both fields are load-bearing here: the
        server 400s on a prefilled assistant turn while thinking is enabled
        (see the S41 correction comment in chat() above). Inherited unchanged
        by LlamaCppBackend and VLLMBackend -- llama.cpp-server and vLLM both
        natively support chat_template_kwargs and tolerate the extra field,
        and the same prefill-vs-thinking conflict applies. Cloud subclasses
        (OpenAI, OpenRouter, DeepSeek, Groq, Kimi, Qwen, OmniRoute, Custom)
        override this with a no-op -- their transports reject these fields.
        """
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.7, max_tokens: int = 4096,
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None,
             tool_choice_mode: Optional[ToolChoiceMode] = None) -> dict:
        model = self.get_model()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        self._apply_output_token_limit(payload, max_tokens, model=model)
        # VISION-TOOL-INTEROP-01 -- has_vision has existed unmodified since
        # the very first commit (6c0b78d, 2026-06-12): a blanket, backend-
        # wide default that drops tools/tool_choice the moment ANY message
        # in the request (not just the current turn -- the whole
        # accumulated history) carries multipart content. That default is
        # still the right fallback for a backend/model with no confirmed
        # capability data (never force tools past a transport that might
        # reject or silently mishandle the combination). But it is no
        # longer unconditional: supports_vision_with_tools(model) lets a
        # backend/model that has ACTUALLY been confirmed to handle vision
        # + tools together (live-verified for OpenRouter/z-ai/glm-5.3-flash
        # via real HTTP 200s with correct tool_calls decisions across
        # auto/required tool_choice, both with and without a tool actually
        # warranted -- see the campaign report) keep offering tools on a
        # vision turn, including a later text-only turn whose history
        # merely contains an old image. This never changes WHICH tools are
        # offered (self.registry's already-enabled set, decided entirely
        # above this call) -- only whether any are offered when vision is
        # present. See core/agent.py's WORK-round construction for the
        # honest capability notice surfaced when this stays False instead
        # of the old silent drop.
        has_vision = any(isinstance(m.get("content"), list) for m in messages)
        if tools and (not has_vision or self.supports_vision_with_tools(model)):
            payload["tools"] = tools
            # AGENT-CONTINUATION-01B -- "required" only ever reaches the
            # wire for a subclass with live-verified support (see
            # supports_required_tool_choice overrides); every other
            # OpenAI-compatible descendant (local servers included) keeps
            # emitting exactly "auto", byte-identical to pre-01B payloads.
            resolved = self._resolve_tool_choice_mode(tool_choice_mode)
            payload["tool_choice"] = "required" if resolved == ToolChoiceMode.REQUIRED else "auto"

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
        effective_effort = self._effective_reasoning_effort(reasoning_effort, disable_thinking, model=model)
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
        #
        # UTILITY-RUNTIME-01: the wire translation now lives in the
        # _apply_disable_thinking() provider-boundary hook instead of being
        # inlined here. This class's implementation below emits LM Studio's
        # local fields (correct for this server); cloud OpenAI-compatible
        # subclasses that inherit this chat() override the hook to a no-op
        # so they never transmit fields their provider rejects.
        if disable_thinking:
            self._apply_disable_thinking(payload)

        base_url = validate_base_url(self.base_url, self.display_name)
        resp = None  # BACKEND-ERROR-01: bound-checkable for the HTTPError handler
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
            if resp is None:
                # HTTPError escaped the transport call itself (custom adapter,
                # session hook, or strict-provider test double) — never
                # dereference an unbound response; diagnose from the
                # exception, which stays preserved as __context__.
                raise RuntimeError(format_provider_error(self.display_name, None, "", str(e)))
            print(f"[HTTP ERROR BODY] {resp.text[:500]}", flush=True)
            raise RuntimeError(format_provider_error(self.display_name, resp.status_code, resp.text, str(e)))

    def extract_reasoning(self, response: dict) -> Optional[str]:
        """AGENT-TOOL-THINK-TELEMETRY-01A1 -- shared across every
        LMStudioBackend descendant for free (DeepSeek/Groq/Kimi/
        llama.cpp/OmniRoute/OpenAI/OpenRouter/Qwen/vLLM/Custom all inherit
        this unmodified, same pattern as extract_message()/
        extract_termination() above). See
        extract_openai_compatible_reasoning()'s own docstring for the
        live-verified field priority and shape."""
        return extract_openai_compatible_reasoning(response)

    def chat_stream(self, messages: list, max_tokens: int = 4096,
                    temperature: float = 0.7,
                    reasoning_effort: Optional[str] = None) -> Generator[str, None, None]:
        model = self.get_model()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        self._apply_output_token_limit(payload, max_tokens, model=model)
        # No disable_thinking on this signature (never had one) -- forward
        # reasoning_effort straight through, no precedence guard needed.
        self.apply_reasoning(payload, reasoning_effort, model=model)
        base_url = validate_base_url(self.base_url, self.display_name)
        resp = None  # BACKEND-ERROR-01: bound-checkable for the HTTPError handler
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
            if resp is None:
                # See chat(): HTTPError from the transport call itself must
                # never dereference an unbound response.
                raise RuntimeError(format_provider_error(self.display_name, None, "", str(e)))
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

                    # AGENT-GLM-THINK-TOOL-TRANSITION-01 -- same
                    # reasoning_content/reasoning/thinking alias priority
                    # as extract_openai_compatible_reasoning()'s
                    # non-streaming lookup, via the shared
                    # _first_reasoning_field() helper. Previously this
                    # only checked reasoning_content/thinking here,
                    # silently dropping OpenRouter/GLM's "reasoning" delta
                    # field entirely (never shown as Think, never falls
                    # through to content either) -- live-proven 2026-08-29
                    # against z-ai/glm-5.3-flash: 74-147 of ~90-449 frames
                    # per final-answer stream carried non-empty
                    # delta["reasoning"] alongside an empty delta["content"]
                    # in the same frame. strip=False preserves the
                    # original never-strip per-token semantics exactly.
                    reasoning = _first_reasoning_field(delta, strip=False) or ""
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
