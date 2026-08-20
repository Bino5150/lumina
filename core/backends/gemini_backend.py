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
import re
import requests
from typing import Optional

import config
from core.backends.base import BaseLLMBackend
from core.backends.reasoning import ReasoningCapabilities, NO_REASONING_CONTROL

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Patch 3A.4 Part 2A -- per-model reasoning ("thinking level") capability
# data. Model-specific matrix, not a provider-wide list -- Gemini's own
# docs show thinking-level support varies per model, and the task spec
# gave an explicit required matrix (reproduced here) rather than one
# uniform set. Unknown/unlisted Gemini model -> NO_REASONING_CONTROL.
_REASONING_MODELS = {
    "gemini-3.7-flash": ReasoningCapabilities(
        efforts=("low", "medium", "high"), default_effort="medium"),
    "gemini-3.6-flash": ReasoningCapabilities(
        efforts=("minimal", "low", "medium", "high"), default_effort="medium"),
    "gemini-3.5-flash": ReasoningCapabilities(
        efforts=("minimal", "low", "medium", "high"), default_effort="medium"),
    "gemini-3.5-flash-lite": ReasoningCapabilities(
        efforts=("minimal", "low", "medium", "high"), default_effort="minimal"),
    "gemini-3.1-pro-preview": ReasoningCapabilities(
        efforts=("low", "medium", "high"), default_effort="high"),
    "gemini-3.1-flash-lite-image": ReasoningCapabilities(
        efforts=("minimal", "high"), default_effort="minimal"),
    "gemini-3-flash-preview": ReasoningCapabilities(
        efforts=("minimal", "low", "medium", "high"), default_effort="high"),
    "gemini-3-pro-preview": ReasoningCapabilities(
        efforts=("low", "high"), default_effort="high"),
    "gemini-2.5-pro": ReasoningCapabilities(
        efforts=("low", "medium", "high")),
    "gemini-2.5-flash": ReasoningCapabilities(
        efforts=("low", "medium", "high")),
    "gemini-2.5-flash-lite": ReasoningCapabilities(
        efforts=("low", "medium", "high")),
}


def normalize_gemini_tool_result(value):
    """
    Normalize a tool result into the dict shape Gemini's functionResponse.response
    field requires -- Gemini rejects a bare string/list/None/number/bool there,
    it always wants an object. dict passes through as-is (already the right
    shape); everything else gets wrapped under a "result" key.
    """
    if isinstance(value, dict):
        return value
    if value is None or isinstance(value, (list, str, int, float, bool)):
        return {"result": value}
    return {"result": str(value)}


_THOUGHT_SIG_RE = re.compile(r'("thoughtSignature"\s*:\s*")[^"]*(")')


def _redact_thought_signatures(text: str) -> str:
    """
    Scrub opaque thoughtSignature values out of raw Gemini response text before
    any log/print -- same treatment as an API key. Never appears in routine
    logs; it's an opaque token Gemini requires we round-trip, not diagnostic
    content, and has no reason to ever be human-visible.
    """
    if not text:
        return text
    return _THOUGHT_SIG_RE.sub(r'\1[REDACTED]\2', text)


def classify_gemini_error(status_code, raw_body: str) -> dict:
    """
    Best-effort classification of a Gemini API error response into the same
    normalized "kind" vocabulary core/backends/lmstudio.py's
    classify_provider_error() already uses for the OpenAI-compatible backend
    family (generic/billing_required/insufficient_quota/authentication/
    rate_limited) -- so a caller can treat any backend's failure the same
    way regardless of which provider produced it. Never raises.

    Gemini's error body is Google's standard envelope:
        {"error": {"code": <int>, "message": <str>, "status": <ENUM_STRING>}}
    -- a different shape from OpenAI's {"error": {"type", "message"}}: the
    classifying field is "status" (upper-snake-case, e.g. RESOURCE_EXHAUSTED,
    PERMISSION_DENIED, UNAUTHENTICATED), not "type", and there's no separate
    "type" key at all.

    RESOURCE_EXHAUSTED is Gemini's status for BOTH true quota exhaustion
    (the free-tier daily/per-minute request cap this backend actually hit
    during testing) and plain rate limiting -- Google doesn't split these
    into separate statuses. The message text is the only signal that tells
    them apart ("quota"/"free_tier_requests" wording vs a bare "please retry
    in Ns" throttle), so -- same message-first-then-status-code precedence
    classify_provider_error() already uses, for the same reason -- the
    message is checked before falling back to the bare status/HTTP code.
    """
    kind = "generic"
    message = ""
    try:
        body = json.loads(raw_body) if raw_body else {}
        err = body.get("error", body) if isinstance(body, dict) else {}
        status = str(err.get("status") or "")
        message = str(err.get("message") or "")
        haystack = f"{status} {message}".lower()
        if "quota" in haystack:
            kind = "insufficient_quota"
        elif "billing" in haystack:
            kind = "billing_required"
        elif status == "RESOURCE_EXHAUSTED" or status_code == 429:
            kind = "rate_limited"
        elif status == "UNAUTHENTICATED" or status_code == 401:
            kind = "authentication"
        elif status == "PERMISSION_DENIED" or status_code == 403:
            kind = "authentication"
    except Exception:
        pass
    return {"kind": kind, "message": message}


def format_gemini_error(status_code, raw_body: str, fallback: str) -> str:
    """Build the single error string chat()/chat_stream() raise on an HTTP
    error -- same output shape as lmstudio.py's format_provider_error(), so
    a caller doesn't need to special-case which backend produced it:
    "Gemini error (<kind>, HTTP <status>): <detail>". raw_body should
    already be thoughtSignature-redacted (see _redact_thought_signatures)
    before it reaches this function -- this only classifies/formats, it
    never touches redaction itself."""
    info = classify_gemini_error(status_code, raw_body)
    detail = info["message"] or (raw_body[:500] if raw_body else fallback)
    return f"Gemini error ({info['kind']}, HTTP {status_code}): {detail}"


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

    def configured_model(self) -> Optional[str]:
        """
        Patch 3A.4 Part 4 override -- GeminiBackend has NO self._model
        attribute at all (it uses self.default_model exclusively, same as
        AnthropicBackend). See AnthropicBackend.configured_model()'s
        docstring for the full rationale -- the base BaseLLMBackend
        implementation reading self._model would silently return None here
        even though a real configured model exists.
        """
        return self.default_model or None

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 2A -- reasoning-effort capability + wire translation
    # ------------------------------------------------------------------

    def reasoning_capabilities(self, model=None) -> ReasoningCapabilities:
        """
        `model=None` always falls through to NO_REASONING_CONTROL, matching
        the base class's documented contract (get_model() is not consulted
        even though it would be side-effect-free here today).
        """
        if model is None:
            return NO_REASONING_CONTROL
        return _REASONING_MODELS.get(model, NO_REASONING_CONTROL)

    def _apply_reasoning_override(self, payload: dict, effort: str, model=None) -> None:
        """
        POSITIVELY VERIFIED wire shape (not left inert) -- confirmed
        2026-08-20 against Google's own authoritative Gemini API REST
        reference docs at ai.google.dev/gemini-api/docs/generate-content/
        thinking and .../gemini-3 (the generateContent endpoint page, not
        the Interactions API and not a Python/JS SDK's snake_case
        convenience naming):

          - The REST JSON field is nested as
            generationConfig.thinkingConfig.thinkingLevel (camelCase) --
            confirmed via that doc's own REST request example:
                {"generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}}
          - Valid thinkingLevel values per that doc: minimal / low / medium
            / high, with support varying by model -- already encoded
            per-model in _REASONING_MODELS above, so `effort` reaching this
            method has already been validated against the right set for
            this specific model.
          - The legacy sibling field is
            generationConfig.thinkingConfig.thinkingBudget. The gemini-3
            doc states explicitly: "You cannot use both thinking_level and
            the legacy thinking_budget parameter in the same request.
            Doing so will return a 400 error." Lumina's own _build_payload()
            never sets thinkingBudget today (confirmed: no thinking field
            exists anywhere in current payload construction before this
            change), but thinkingBudget is popped defensively below anyway
            so the two fields can never coexist in the payload regardless
            of future changes elsewhere -- per the Part 2A spec, these must
            never be mixed under any circumstance.

        Merges defensively into any pre-existing generationConfig /
        thinkingConfig dict (payload already carries maxOutputTokens /
        temperature in generationConfig from _build_payload()) rather than
        overwriting either object wholesale.
        """
        generation_config = payload.setdefault("generationConfig", {})
        thinking_config = generation_config.setdefault("thinkingConfig", {})
        thinking_config.pop("thinkingBudget", None)
        thinking_config["thinkingLevel"] = effort

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

          - role="tool" -> role="user" content with functionResponse part(s).
            context.py's add_tool_result() shape is {"role": "tool", "tool_call_id",
            "name", "content"} — Gemini's functionResponse wants {"name", "response"}
            where response is a dict, so the (already-stringified, by context.py)
            content gets wrapped via normalize_gemini_tool_result(). NOTE: Gemini's
            functionResponse has no id/tool_call_id field at all — matching is
            implicitly by name + turn order, not an explicit call id. This is a
            real structural difference from both OpenAI and Anthropic, which both
            thread an explicit id through. If a single assistant turn ever issues
            two parallel calls to the *same* tool name, Gemini has no built-in way
            to disambiguate which result pairs with which call beyond sequence —
            noting this since it's a real (if narrow) limitation of the wire
            format itself, not a gap in this translation layer.
            Consecutive "tool" messages (i.e. every result from one assistant
            turn's parallel tool calls) are merged into ONE user Content with
            multiple functionResponse parts — Gemini expects parallel function
            responses batched together in a single turn (FC1,FC2 -> one turn with
            FR1+FR2), not interleaved as separate back-to-back user turns
            (FC1,FR1,FC2,FR2), which is what naive one-message-at-a-time
            translation would otherwise produce.
          - role="assistant" with tool_calls -> role="model" content.
            THOUGHT-SIGNATURE PRESERVATION (Bug B): if this message carries
            provider_metadata.gemini_content (stashed by extract_message() from
            Gemini's own response), that exact Content object — parts, ordering,
            and any opaque thoughtSignature — is re-sent unmodified. Gemini
            requires the thoughtSignature it attached to a functionCall part to
            come back byte-for-byte on the next request, or it 400s with
            "Function call is missing a thought_signature in functionCall parts."
            Reconstructing a fresh functionCall part here (the old behavior) can
            never carry that opaque value, since it never exists anywhere in the
            OpenAI-neutral tool_calls record agent.py/context.py operate on —
            those are shared by every backend and have no room for a
            Gemini-specific opaque field, so the raw Content object is smuggled
            through as an extra provider_metadata key instead (see
            extract_message()). Falls back to reconstructing from tool_calls only
            if provider_metadata is absent (history that predates this fix, or a
            non-Gemini origin) — not expected to fire in normal operation, kept
            only so an odd history shape degrades instead of crashing.
          - role="user"/"assistant" plain text -> role="user"/"model" with a single
            text part.
        """
        out = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            role = m.get("role")

            if role == "tool":
                parts = []
                while i < n and messages[i].get("role") == "tool":
                    tm = messages[i]
                    parts.append({
                        "functionResponse": {
                            "name": tm.get("name"),
                            "response": normalize_gemini_tool_result(tm.get("content", "")),
                        }
                    })
                    i += 1
                out.append({"role": "user", "parts": parts})
                continue

            if role == "assistant" and m.get("tool_calls"):
                gemini_content = (m.get("provider_metadata") or {}).get("gemini_content")
                if gemini_content is not None:
                    out.append(gemini_content)
                    i += 1
                    continue

                # Fallback reconstruction — loses thoughtSignature. See docstring.
                parts = []
                text = m.get("content")
                if text:
                    parts.append({"text": text})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", tc)
                    raw_args = fn.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            args = {}
                    else:
                        # Already a dict (some clients emit parsed args) — do not
                        # swallow them; the old `except TypeError: args = {}` path
                        # silently executed every tool with empty arguments.
                        args = raw_args or {}
                    parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
                out.append({"role": "model", "parts": parts})
                i += 1
                continue

            gemini_role = "model" if role == "assistant" else "user"
            out.append({"role": gemini_role, "parts": cls._parts_from_content(m.get("content"))})
            i += 1

        return out

    @staticmethod
    def _parts_from_content(content):
        # Convert an OpenAI-shaped content value into Gemini Part dicts.
        # Plain string -> [{"text": ...}].
        # Multipart list (vision turn) -> text blocks become {"text": ...},
        # image_url blocks become {"inline_data": {mime_type, data}} — Gemini's
        # inline image format. Only data: URLs are handled; remote http(s) URLs
        # are skipped (fetching them would need extra machinery + a MIME guess).
        if not isinstance(content, list):
            return [{"text": content or ""}]
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append({"text": text})
            elif btype == "image_url":
                iu = block.get("image_url")
                url = iu.get("url", "") if isinstance(iu, dict) else ""
                if url and url.startswith("data:"):
                    try:
                        meta, _, b64 = url.partition(",")
                        mime = meta[len("data:"):].split(";")[0] or "image/png"
                        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                    except Exception:
                        continue
        if not parts:
            parts.append({"text": ""})
        return parts

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
             disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None):
        # disable_thinking accepted for interface consistency with
        # complete_utility() but not acted on here for THINKING itself —
        # same reasoning as AnthropicBackend: this backend doesn't enable
        # Gemini's thinking mode by default, so the conflict doesn't apply
        # today. Still routed through _effective_reasoning_effort() below
        # so the disable_thinking-wins precedence contract holds regardless.
        payload = self._build_payload(messages, tools, max_tokens, temperature)
        # Patch 3A.4 Part 3 -- apply the already-verified native translation
        # (Part 2A's thinkingConfig.thinkingLevel) after the payload is
        # fully built, before the HTTP call. self.default_model is a stable
        # instance attribute, reused consistently for both the URL below
        # and this capability lookup.
        effective_effort = self._effective_reasoning_effort(reasoning_effort, disable_thinking)
        self.apply_reasoning(payload, effective_effort, model=self.default_model)
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
            # Redact before truncating for the log line (truncating first
            # could cut a thoughtSignature in half and leave a partial value
            # visible) -- then classify off the full redacted body so a
            # quota/rate-limit/auth message longer than the 500-char log
            # preview still gets read correctly.
            redacted = _redact_thought_signatures(resp.text)
            print(f"[HTTP ERROR BODY] model={self.default_model} status={resp.status_code} body={redacted[:500]}", flush=True)
            raise RuntimeError(format_gemini_error(resp.status_code, redacted, str(e)))

    def extract_message(self, response):
        """
        Normalize a Gemini generateContent response back into the OpenAI-shaped
        dict the rest of agent.py expects: {"role", "content", "tool_calls": [...]
        or omitted} — same target shape anthropic_backend.py produces.
        """
        try:
            candidate = response["candidates"][0]
            parts = candidate["content"]["parts"]
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
            # Stash Gemini's own Content object (all parts, in original order,
            # including any opaque thoughtSignature) so _translate_messages can
            # re-send it unmodified on the next turn instead of reconstructing a
            # fresh functionCall part that would be missing the signature —
            # Bug B. Carried as a whole unit, not per-tool-call, because Gemini
            # may attach the signature to only the first part of a parallel
            # response; splitting it up here and reattaching later per-call
            # would risk copying that signature onto parts it was never issued
            # for. See _translate_messages' docstring for the read side.
            message["provider_metadata"] = {"gemini_content": candidate["content"]}
        return message

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7,
                     reasoning_effort: Optional[str] = None):
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
        # Patch 3A.4 Part 3 -- same native translation as chat() above, no
        # disable_thinking on this signature so no precedence guard needed.
        self.apply_reasoning(payload, reasoning_effort, model=self.default_model)
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
        except requests.exceptions.HTTPError as e:
            # This branch didn't exist before -- an HTTP error on the
            # streaming path (quota exhausted, rate limited, auth failure --
            # the exact class of failure chat()'s non-streaming path already
            # handles above) used to leak as a raw requests.exceptions.
            # HTTPError instead of the RuntimeError chat() raises. core/
            # agent.py's _stream_final() only catches (ConnectionError,
            # TimeoutError, RuntimeError, ValueError) for its graceful
            # "[Stream error: ...]" handling -- an uncaught HTTPError skipped
            # that path entirely and fell through to a cruder top-level
            # handler instead. Same shape as chat()'s handling, just also
            # applied here.
            redacted = _redact_thought_signatures(resp.text)
            print(f"[HTTP ERROR BODY] model={self.default_model} status={resp.status_code} body={redacted[:500]}", flush=True)
            raise RuntimeError(format_gemini_error(resp.status_code, redacted, str(e)))

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            # Gemini's SSE stream has no charset param on its Content-Type
            # (text/event-stream), so requests.Response.encoding defaults to
            # ISO-8859-1 (see requests.utils.get_encoding_from_headers) --
            # decode_unicode=True would silently mangle every multi-byte
            # UTF-8 character (em-dashes, curly quotes, accents, emoji) into
            # mojibake. The API always sends UTF-8, so decode explicitly.
            raw_line = raw_line.decode("utf-8")
            if not raw_line.startswith("data:"):
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
