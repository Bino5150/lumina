"""Foreground ChatGPT Plan Responses lane.

The account's OAuth session is the sole credential source. This transport
never reads an API key, changes billing lanes, retries a request, or accepts
a stream without a positively completed terminal event.
"""

import copy
import json
import math
import re
import time
from typing import Optional

import requests

import config
from core import lane_preferences, persistence
from core.chatgpt_auth import ChatGPTAuthError, PlanPermission, SessionState, get_session_manager
from core.chatgpt_plan_chat_binding import ChatBindingError, check_or_bind_chat
from .base import BackendStreamTelemetry, ModelDiscoveryOutcome, ModelDiscoveryResult
from .lmstudio import join_endpoint, _iter_lines_safe
from .openai_backend import (
    OpenAIBackend, _ResponsesTiming, _normalize_responses_body,
    _normalize_responses_usage, _translate_messages_to_input,
)
from .reasoning import NO_REASONING_CONTROL


_TOOL_NAMESPACE = "lumina"
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_MAX_CALLS_PER_RESPONSE = 32
_MAX_STREAM_ITEMS = 128
_MAX_STREAM_ARGUMENT_CHARS = 262144


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate function argument key.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonstandard function argument number.")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nonfinite function argument number.")
    return parsed


def _plan_tools(schemas: list) -> tuple[list, dict]:
    """Translate only explicitly offered registry schemas, refusing collisions."""
    names = {}
    functions = []
    for schema in schemas:
        if not isinstance(schema, dict) or schema.get("type") != "function":
            raise ValueError("Invalid ChatGPT Plan tool schema.")
        fn = schema.get("function")
        if not isinstance(fn, dict):
            raise ValueError("Invalid ChatGPT Plan tool schema.")
        name, description, parameters = (fn.get(k) for k in ("name", "description", "parameters"))
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in names:
            raise ValueError("Invalid or colliding ChatGPT Plan tool name.")
        if not isinstance(description, str) or not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("Invalid ChatGPT Plan function definition.")
        names[name] = name
        functions.append({"type": "function", "name": name,
                          "description": description, "parameters": parameters})
    return ([{"type": "namespace", "name": _TOOL_NAMESPACE,
              "description": "Lumina's enabled local tools", "tools": functions}] if functions else []), names


def _plan_response(body: dict, offered: dict, previous_ids: set,
                   telemetry=None) -> tuple[dict, list]:
    """Validate an entire completed response before any call can reach dispatch."""
    output = body.get("output")
    if body.get("status") != "completed" or not isinstance(output, list):
        raise ValueError("ChatGPT Plan response was not complete or well formed.")
    seen = set(previous_ids)
    call_count = 0
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Malformed ChatGPT Plan output item.")
        kind = item.get("type")
        if kind == "function_call":
            call_count += 1
            if call_count > _MAX_CALLS_PER_RESPONSE:
                raise ValueError("ChatGPT Plan function-call batch exceeds the safe limit.")
            call_id, name, args = (item.get(k) for k in ("call_id", "name", "arguments"))
            if (not isinstance(call_id, str) or not call_id or len(call_id) > 64
                    or call_id in seen or not isinstance(name, str)
                    or item.get("namespace") != _TOOL_NAMESPACE or name not in offered
                    or not isinstance(args, str) or len(args) > _MAX_STREAM_ARGUMENT_CHARS
                    or item.get("status") not in (None, "completed")
                    or item.get("async") is True
                    or (item.get("caller") not in (None, {"type": "direct"}))):
                raise ValueError("Unknown or malformed ChatGPT Plan function call.")
            try:
                parsed = json.loads(args, object_pairs_hook=_unique_object,
                                    parse_constant=_reject_constant, parse_float=_finite_float)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValueError("Invalid ChatGPT Plan function arguments.") from exc
            if not isinstance(parsed, dict):
                raise ValueError("ChatGPT Plan function arguments must be an object.")
            seen.add(call_id)
        elif kind == "reasoning":
            if item.get("status") not in (None, "completed"):
                raise ValueError("Incomplete ChatGPT Plan reasoning item.")
            if item.get("encrypted_content") is not None:
                if not isinstance(item["encrypted_content"], str):
                    raise ValueError("Malformed ChatGPT Plan reasoning item.")
        elif kind == "message":
            if (item.get("status") not in (None, "completed")
                    or item.get("role") not in (None, "assistant")
                    or not isinstance(item.get("content"), list)):
                raise ValueError("Malformed ChatGPT Plan message item.")
            if any(not isinstance(p, dict) or p.get("type") != "output_text" or not isinstance(p.get("text"), str)
                   for p in item["content"]):
                raise ValueError("Unsupported ChatGPT Plan message content.")
        else:
            raise ValueError("Unsupported ChatGPT Plan output item.")
    normalized = _normalize_responses_body(body, telemetry=telemetry)
    for call in normalized["choices"][0]["message"].get("tool_calls", []):
        call["function"]["name"] = offered[call["function"]["name"]]
    # Responses output items are replayable input items. Keep their exact
    # sequence and provider identities outside the normalized conversation:
    # reasoning may occur between two calls in the same completed response.
    return normalized, copy.deepcopy(output)


class _CompletedStreamItems:
    """Reconstruct only completed items; deltas cannot authorize a tool call."""

    def __init__(self):
        self.items: dict[int, dict] = {}
        self.open: dict[int, dict] = {}
        self.argument_fragments: dict[int, list[str]] = {}
        self.argument_lengths: dict[int, int] = {}
        self.argument_done: dict[int, str] = {}
        self.text_fragments: dict[tuple[int, int], list[str]] = {}
        self.text_lengths: dict[tuple[int, int], int] = {}
        self.text_done: dict[tuple[int, int], str] = {}

    @staticmethod
    def _index(event):
        index = event.get("output_index")
        if type(index) is not int or index < 0 or index >= _MAX_STREAM_ITEMS:
            raise ValueError("Invalid ChatGPT Plan stream output index.")
        return index

    def _identity(self, event):
        index = self._index(event)
        item = self.open.get(index)
        if item is None or event.get("item_id") != item.get("id"):
            raise ValueError("ChatGPT Plan stream item identity changed.")
        return index

    def observe(self, event):
        kind = event.get("type")
        if kind == "response.output_item.added":
            index = self._index(event)
            item = event.get("item")
            if (index != len(self.open) or not isinstance(item, dict)
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(item.get("type"), str)
                    or any(previous.get("id") == item["id"] for previous in self.open.values())):
                raise ValueError("Malformed or duplicate ChatGPT Plan output item addition.")
            self.open[index] = item
            return
        if kind == "response.output_item.done":
            index = self._index(event)
            original = self.open.get(index)
            item = event.get("item")
            if (original is None or index in self.items or not isinstance(item, dict)
                    or item.get("id") != original.get("id")
                    or item.get("type") != original.get("type")):
                raise ValueError("Malformed or mismatched ChatGPT Plan output item completion.")
            if item.get("type") == "function_call":
                for field in ("call_id", "name", "namespace"):
                    if field in original and field in item and original[field] != item[field]:
                        raise ValueError("ChatGPT Plan function-call identity changed.")
                arguments = item.get("arguments")
                if not isinstance(arguments, str):
                    raise ValueError("ChatGPT Plan function arguments were not completed.")
                if index in self.argument_done and arguments != self.argument_done[index]:
                    raise ValueError("ChatGPT Plan function arguments changed after completion.")
                fragments = self.argument_fragments.get(index)
                if fragments and index not in self.argument_done:
                    raise ValueError("ChatGPT Plan function argument stream did not complete.")
                if fragments and arguments != "".join(fragments):
                    raise ValueError("ChatGPT Plan function argument deltas disagree with completed item.")
            if item.get("type") == "message":
                if (original.get("role") is not None and item.get("role") is not None
                        and original["role"] != item["role"]):
                    raise ValueError("ChatGPT Plan message role changed.")
                content = item.get("content")
                if not isinstance(content, list):
                    raise ValueError("Malformed ChatGPT Plan completed message.")
                for (output_index, content_index), fragments in self.text_fragments.items():
                    if output_index == index:
                        if (content_index >= len(content) or not isinstance(content[content_index], dict)
                                or content[content_index].get("type") != "output_text"
                                or content[content_index].get("text") != "".join(fragments)):
                            raise ValueError("ChatGPT Plan text deltas disagree with completed item.")
                for (output_index, content_index), done_text in self.text_done.items():
                    if output_index == index and (content_index >= len(content)
                            or content[content_index].get("text") != done_text):
                        raise ValueError("ChatGPT Plan completed text changed.")
            self.items[index] = item
            return
        if kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            index = self._identity(event)
            if self.open[index].get("type") != "function_call" or index in self.items:
                raise ValueError("ChatGPT Plan argument event was outside an open function call.")
            if kind.endswith(".delta"):
                delta = event.get("delta")
                if not isinstance(delta, str) or index in self.argument_done:
                    raise ValueError("Malformed ChatGPT Plan function argument delta.")
                fragments = self.argument_fragments.setdefault(index, [])
                length = self.argument_lengths.get(index, 0) + len(delta)
                if length > _MAX_STREAM_ARGUMENT_CHARS:
                    raise ValueError("ChatGPT Plan streamed arguments exceeded the safe limit.")
                fragments.append(delta)
                self.argument_lengths[index] = length
            else:
                arguments = event.get("arguments")
                if not isinstance(arguments, str) or index in self.argument_done:
                    raise ValueError("Duplicate or malformed ChatGPT Plan argument completion.")
                fragments = self.argument_fragments.get(index)
                if fragments and arguments != "".join(fragments):
                    raise ValueError("ChatGPT Plan argument deltas disagree with completion.")
                self.argument_done[index] = arguments
            return
        if kind in ("response.output_text.delta", "response.output_text.done"):
            # Legacy completed-body fixtures omit item metadata. Real item
            # streams carry it, and must bind every text event to its item.
            if not self.open:
                return
            index = self._identity(event)
            content_index = event.get("content_index")
            if (self.open[index].get("type") != "message" or index in self.items
                    or type(content_index) is not int or content_index < 0):
                raise ValueError("Invalid ChatGPT Plan text event identity.")
            key = (index, content_index)
            if kind.endswith(".delta"):
                delta = event.get("delta")
                if not isinstance(delta, str) or key in self.text_done:
                    raise ValueError("Malformed ChatGPT Plan text delta.")
                length = self.text_lengths.get(key, 0) + len(delta)
                if length > 1048576:
                    raise ValueError("ChatGPT Plan streamed text exceeded the safe limit.")
                self.text_fragments.setdefault(key, []).append(delta)
                self.text_lengths[key] = length
            else:
                value = event.get("text")
                if not isinstance(value, str) or key in self.text_done:
                    raise ValueError("Duplicate or malformed ChatGPT Plan text completion.")
                fragments = self.text_fragments.get(key)
                if fragments and value != "".join(fragments):
                    raise ValueError("ChatGPT Plan text deltas disagree with completion.")
                self.text_done[key] = value
            return
        if kind in ("response.content_part.added", "response.content_part.done",
                    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                    "response.refusal.delta", "response.refusal.done") and self.open:
            index = self._identity(event)
            if index in self.items:
                raise ValueError("ChatGPT Plan item event arrived after item completion.")

    def completed_body(self, body):
        output = body.get("output")
        if not isinstance(output, list):
            raise ValueError("ChatGPT Plan completed output was not a list.")
        if self.open:
            if len(self.items) != len(self.open):
                raise ValueError("ChatGPT Plan stream ended with an unfinished output item.")
            ordered = [self.items[index] for index in range(len(self.open))]
            if output and output != ordered:
                raise ValueError("ChatGPT Plan terminal output disagreed with completed items.")
            return dict(body, output=copy.deepcopy(ordered))
        return body


class PlanStreamError(RuntimeError):
    """A non-success terminal state, safe for the agent's final recorder."""

    def __init__(self, message: str, state: str = "none", *,
                 http_status=None, provider_request_id=None):
        super().__init__(message)
        self.stream_completion_state = state
        self.http_status = http_status
        self.provider_request_id = provider_request_id


class ChatGPTPlanBackend(OpenAIBackend):
    name = "openai_chatgpt_plan"
    display_name = "ChatGPT Plan"
    default_url = "https://api.openai.com/v1"
    endpoint_configurable = False
    supports_required_tool_choice = False
    KNOWN_MODELS = ()

    def __init__(self, base_url: Optional[str] = None, manager=None):
        # Deliberately do not call OpenAIBackend.__init__: it installs
        # paid-API credentials in self.headers.
        self.base_url = base_url or self.default_url
        self._manager = manager
        self._model = lane_preferences.get_lane_model(persistence.load(), self.name) or ""
        self.model_labels: dict[str, str] = {}
        self.catalog_profile_id: Optional[str] = None
        self._response_replay: dict[str, dict] = {}
        self._turn_binding: Optional[tuple] = None
        self._turn_invalid: Optional[str] = None
        self._chat_id: Optional[int] = None
        self._headless_history_binding: Optional[tuple] = None

    def supports_tool_work(self) -> bool:
        return True

    def clear_tool_turn_state(self) -> None:
        """Retire opaque stateless response items at every foreground turn exit."""
        self._response_replay.clear()
        self._turn_binding = None
        self._turn_invalid = None
        self._chat_id = None

    def set_chat_context(self, chat_id: Optional[int]) -> None:
        """Receive the agent's current durable chat ID before provider work."""
        self._chat_id = chat_id

    def supports_vision(self, model: Optional[str] = None) -> bool:
        return False

    def supports_vision_with_tools(self, model: Optional[str] = None) -> bool:
        return False

    def reasoning_capabilities(self, model: Optional[str] = None):
        return NO_REASONING_CONTROL

    def _accepts_temperature(self, model: Optional[str]) -> bool:
        return False

    def _apply_output_token_limit(self, payload: dict, max_tokens: int,
                                  model: Optional[str] = None) -> None:
        return None

    def _session(self):
        if self._manager is None:
            self._manager = get_session_manager()
        return self._manager

    def _grant(self):
        try:
            return self._session().get_valid_access_token()
        except ChatGPTAuthError as exc:
            raise RuntimeError(exc.user_message) from exc

    def _check_turn_binding(self, grant, messages: list) -> None:
        """Refuse a changed OAuth identity, profile, lane or model before I/O.

        A token generation is deliberately excluded: ordinary renewal of
        the same verified account must remain usable. A detected mismatch
        poisons the remainder of this turn even if Settings switches back.
        """
        if self._turn_invalid is not None:
            raise RuntimeError(self._turn_invalid)
        session = self._session()
        try:
            active = session.get_active_profile()
        except ChatGPTAuthError as exc:
            self._turn_invalid = "ChatGPT account changed during tool work. Start a new turn."
            self._response_replay.clear()
            raise RuntimeError(self._turn_invalid) from exc
        identity = getattr(grant, "account_identity", None)
        profile_id = getattr(grant, "profile_id", None)
        if (not isinstance(identity, tuple) or len(identity) != 2
                or not all(isinstance(part, str) and part for part in identity)
                or not isinstance(profile_id, str) or not profile_id
                or active is None or active.profile_id != profile_id
                or not session.is_grant_current(grant)):
            reason = "ChatGPT account changed during tool work. Start a new turn."
        elif self.name != "openai_chatgpt_plan" or (
                self._turn_binding is not None and self.name != self._turn_binding[2]):
            reason = "ChatGPT backend lane changed during tool work. Start a new turn."
        elif self._turn_binding is not None and self.get_model() != self._turn_binding[3]:
            reason = "ChatGPT model changed during tool work. Start a new turn."
        elif self._turn_binding is not None and (profile_id, identity) != self._turn_binding[:2]:
            reason = "ChatGPT account changed during tool work. Start a new turn."
        else:
            reason = None
        if reason is not None:
            self._turn_invalid = reason
            self._response_replay.clear()
            raise RuntimeError(reason)
        binding = (profile_id, identity, self.name, self.get_model())
        if self._chat_id is not None:
            try:
                check_or_bind_chat(self._chat_id, *binding)
            except ChatBindingError as exc:
                self._turn_invalid = str(exc)
                self._response_replay.clear()
                raise
        else:
            # Headless/direct callers have no durable chat ID. Keep the
            # destination for the backend lifetime; a truly fresh one-user
            # request may establish a new conversation after turn cleanup.
            ordinary = [m for m in messages if m.get("role") != "system"]
            fresh = len(ordinary) == 1 and ordinary[0].get("role") == "user"
            if self._headless_history_binding is None and not fresh:
                self._turn_invalid = "ChatGPT conversation has unbound previous history. Start a new chat."
                self._response_replay.clear()
                raise RuntimeError(self._turn_invalid)
            if (self._headless_history_binding is not None
                    and binding != self._headless_history_binding and not fresh):
                if binding[:2] != self._headless_history_binding[:2]:
                    self._turn_invalid = "ChatGPT account changed for this conversation. Start a new chat."
                elif binding[2] != self._headless_history_binding[2]:
                    self._turn_invalid = "ChatGPT backend lane changed for this conversation. Start a new chat."
                else:
                    self._turn_invalid = "ChatGPT model changed for this conversation. Start a new chat."
                self._response_replay.clear()
                raise RuntimeError(self._turn_invalid)
            self._headless_history_binding = binding
        if self._turn_binding is None:
            self._turn_binding = binding

    @staticmethod
    def _auth_headers(grant) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {grant.access_token}"}

    @staticmethod
    def _safe_error(response) -> str:
        """Keep diagnostic identifiers without echoing arbitrary provider text."""
        status = getattr(response, "status_code", None)
        request_id = (getattr(response, "headers", {}) or {}).get("x-request-id", "")
        code = param = detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    code = error.get("code") if isinstance(error.get("code"), str) else ""
                    param = error.get("param") if isinstance(error.get("param"), str) else ""
                    detail = error.get("message") if isinstance(error.get("message"), str) else ""
                elif isinstance(body.get("detail"), str):
                    detail = body["detail"]
        except (ValueError, TypeError):
            pass
        from core.redaction import redact_secret_shapes
        fields = [f"HTTP {status}"]
        if code:
            fields.append(f"code={redact_secret_shapes(code[:120])}")
        if param:
            fields.append(f"param={redact_secret_shapes(param[:120])}")
        if request_id:
            fields.append(f"request_id={redact_secret_shapes(request_id[:120])}")
        if detail:
            fields.append(redact_secret_shapes(detail[:300]))
        return "ChatGPT Plan error (" + ", ".join(fields) + ")"

    def _catalog(self, grant) -> ModelDiscoveryResult:
        if not self._session().is_grant_current(grant):
            return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                        diagnostic="ChatGPT account changed. Refresh models.")
        try:
            response = requests.get(
                join_endpoint(self.base_url, "models"),
                headers=self._auth_headers(grant), timeout=config.TOOL_CALL_TIMEOUT,
            )
            if not response.ok:
                return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                            diagnostic=self._safe_error(response))
            body = response.json()
        except requests.exceptions.RequestException as exc:
            return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                        diagnostic=f"ChatGPT model catalog unavailable ({type(exc).__name__}).")
        except (ValueError, TypeError):
            return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                        diagnostic="ChatGPT model catalog returned invalid JSON.")
        if not isinstance(body, dict) or not isinstance(body.get("models"), list):
            return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                        diagnostic="ChatGPT model catalog has an unsupported shape.")
        ids = []
        labels = {}
        for item in body["models"]:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug, label = item.get("slug"), item.get("display_name")
            if isinstance(slug, str) and slug and isinstance(label, str) and label and slug not in labels:
                ids.append(slug)
                labels[slug] = label
        self.model_labels = labels
        self.catalog_profile_id = grant.profile_id
        if not ids:
            return ModelDiscoveryResult(ModelDiscoveryOutcome.EMPTY,
                                        diagnostic="No selectable models are listed for this ChatGPT account.")
        return ModelDiscoveryResult(ModelDiscoveryOutcome.SUCCESS, models=tuple(ids),
                                    diagnostic="ChatGPT account models refreshed.")

    def discover_models(self) -> ModelDiscoveryResult:
        try:
            return self._catalog(self._grant())
        except RuntimeError as exc:
            return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED, diagnostic=str(exc))

    def list_models(self) -> list[str]:
        result = self.discover_models()
        return list(result.models) if result.outcome is ModelDiscoveryOutcome.SUCCESS else []

    def health_check(self) -> tuple[bool, str]:
        try:
            status = self._session().get_session_state()
            permission = self._session().get_plan_permission_state()
        except ChatGPTAuthError as exc:
            return False, exc.user_message
        if status.state is not SessionState.READY or permission is not PlanPermission.GRANTED:
            return False, status.message or "ChatGPT Plan connection or permission is not ready."
        return True, "ChatGPT Plan connected"

    @staticmethod
    def _translate_messages_to_input(messages: list) -> tuple[str, list]:
        instructions = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                if any(not isinstance(part, dict) or part.get("type") != "text" for part in content):
                    raise ValueError("ChatGPT Plan supports text-only foreground chat in this version.")
            if message.get("role") == "system":
                instructions.append(content or "")
        ordinary = [m for m in messages if m.get("role") != "system"]
        return "\n\n".join(instructions), _translate_messages_to_input(ordinary)

    def _payload(self, messages: list, grant, tools=None, *, final=False) -> tuple[dict, dict]:
        model = self.get_model()
        if not model:
            raise ValueError("Choose a ChatGPT Plan model before sending a message.")
        result = self._catalog(grant)
        if result.outcome is not ModelDiscoveryOutcome.SUCCESS:
            raise RuntimeError(result.diagnostic)
        if model not in result.models:
            raise ValueError("Selected model is not available to this ChatGPT account. Reselect a model.")
        instructions, _ = self._translate_messages_to_input(messages)
        # A new WORK turn cannot inherit opaque output from an earlier turn.
        # The final streamer may append an internal user prompt after work;
        # that request still needs this turn's stateless continuity.
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        current_ids = {tc.get("id") for m in messages[last_user + 1:]
                       for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)}
        if not final and not current_ids.intersection(self._response_replay):
            self._response_replay.clear()
        expanded = []
        for message in messages:
            if message.get("role") == "system":
                continue
            calls = message.get("tool_calls") or []
            first_id = calls[0].get("id") if calls and isinstance(calls[0], dict) else None
            batch = self._response_replay.get(first_id) if first_id else None
            if message.get("role") == "assistant" and batch is not None:
                actual_calls = tuple((tc.get("id"), (tc.get("function") or {}).get("name"),
                                      (tc.get("function") or {}).get("arguments"))
                                     for tc in calls if isinstance(tc, dict))
                if actual_calls != batch["calls"] or len(actual_calls) != len(calls):
                    raise ValueError("ChatGPT Plan cached function-call history changed.")
                expanded.extend(copy.deepcopy(batch["output"]))
                continue
            for item in _translate_messages_to_input([message]):
                if item.get("type") == "function_call":
                    item = dict(item, namespace=_TOOL_NAMESPACE)
                expanded.append(item)
        translated, offered = _plan_tools(tools or [])
        payload = {"model": model, "instructions": instructions, "input": expanded,
                   "store": False, "stream": True}
        if translated:
            payload["tools"] = translated
        return payload, offered

    def _events(self, messages: list, tools=None, *, final=False):
        if self._turn_invalid is not None:
            raise RuntimeError(self._turn_invalid)
        grant = self._grant()
        self._check_turn_binding(grant, messages)
        payload, offered = self._payload(messages, grant, tools, final=final)
        self._check_turn_binding(grant, messages)
        try:
            response = requests.post(
                join_endpoint(self.base_url, "responses"),
                headers=self._auth_headers(grant), json=payload,
                timeout=config.TOOL_CALL_TIMEOUT, stream=True,
            )
            if not response.ok:
                raise PlanStreamError(
                    self._safe_error(response), http_status=response.status_code,
                    provider_request_id=response.headers.get("x-request-id"),
                )
            terminal_event = None
            completed_items = _CompletedStreamItems()
            for line in _iter_lines_safe(response):
                if not line or not line.startswith(b"data: "):
                    continue
                raw = line[6:]
                if raw == b"[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    raise RuntimeError("ChatGPT Plan returned a malformed stream event.")
                if not isinstance(event, dict):
                    raise RuntimeError("ChatGPT Plan returned a malformed stream event.")
                kind = event.get("type")
                if terminal_event is not None:
                    raise PlanStreamError("ChatGPT Plan stream contained events after completion.",
                                          state="ambiguous", http_status=response.status_code,
                                          provider_request_id=response.headers.get("x-request-id"))
                if kind == "response.completed":
                    body = event.get("response")
                    if not isinstance(body, dict) or body.get("status") != "completed":
                        raise PlanStreamError(
                            "ChatGPT Plan completion event lacked a completed response.",
                            http_status=response.status_code,
                            provider_request_id=response.headers.get("x-request-id"),
                        )
                    event["response"] = completed_items.completed_body(body)
                    event["_lumina_http_status"] = response.status_code
                    event["_lumina_request_id"] = response.headers.get("x-request-id")
                    terminal_event = event
                    continue
                if kind in ("response.failed", "response.incomplete", "error"):
                    body = event.get("response") if isinstance(event.get("response"), dict) else {}
                    error = body.get("error") if isinstance(body.get("error"), dict) else {}
                    incomplete = body.get("incomplete_details") if isinstance(body.get("incomplete_details"), dict) else {}
                    from core.redaction import redact_secret_shapes
                    details = [kind, f"HTTP {response.status_code}"]
                    for label, value in (("code", error.get("code")),
                                         ("param", error.get("param")),
                                         ("reason", incomplete.get("reason")),
                                         ("request_id", response.headers.get("x-request-id"))):
                        if isinstance(value, str) and value:
                            details.append(f"{label}={redact_secret_shapes(value[:120])}")
                    state = {"response.failed": "failed", "response.incomplete": "incomplete"}.get(kind, "none")
                    raise PlanStreamError(
                        "ChatGPT Plan stream (" + ", ".join(details) + ")", state,
                        http_status=response.status_code,
                        provider_request_id=response.headers.get("x-request-id"),
                    )
                completed_items.observe(event)
                yield event, offered
            if terminal_event is None:
                raise PlanStreamError(
                    "ChatGPT Plan stream ended before response.completed.",
                    http_status=response.status_code,
                    provider_request_id=response.headers.get("x-request-id"),
                )
            yield terminal_event, offered
        except ConnectionError as exc:
            raise ConnectionError("ChatGPT Plan stream interrupted.") from exc
        except requests.exceptions.Timeout as exc:
            raise TimeoutError("ChatGPT Plan request timed out.") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError("ChatGPT Plan connection interrupted.") from exc
        finally:
            if "response" in locals():
                response.close()

    def chat(self, messages: list, tools=None, temperature: float = 0.7,
             max_tokens: int = 1024, disable_thinking: bool = False,
             reasoning_effort: Optional[str] = None, tool_choice_mode=None,
             capture_telemetry: bool = False) -> dict:
        timing = _ResponsesTiming(time.monotonic())
        previous_ids = {tc.get("id") for m in messages for tc in (m.get("tool_calls") or [])
                        if isinstance(tc, dict)}
        for event, offered in self._events(messages, tools):
            now = time.monotonic()
            timing.observe(event, now)
            if event.get("type") == "response.completed":
                body = event["response"]
                telemetry = timing.snapshot(body, now) if capture_telemetry else None
                normalized, output_items = _plan_response(body, offered, previous_ids, telemetry)
                calls = normalized["choices"][0]["message"].get("tool_calls") or []
                if calls:
                    self._response_replay[calls[0]["id"]] = {
                        "calls": tuple((call["id"], call["function"]["name"],
                                        call["function"]["arguments"]) for call in calls),
                        "output": output_items,
                    }
                    if len(self._response_replay) > 32:
                        self._response_replay.pop(next(iter(self._response_replay)))
                return normalized
        raise RuntimeError("ChatGPT Plan stream ended before response.completed.")

    def chat_stream(self, messages: list, max_tokens: int = 4096,
                    temperature: float = 0.7, reasoning_effort: Optional[str] = None):
        in_think = False
        visible_parts = []
        for event, _offered in self._events(messages, final=True):
            kind = event.get("type")
            if kind == "response.reasoning_summary_text.delta":
                if not in_think:
                    in_think = True
                    yield "__THINK_START__"
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    yield delta
            elif kind == "response.output_text.delta":
                if in_think:
                    in_think = False
                    yield "__THINK_END__"
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    visible_parts.append(delta)
                    yield delta
            elif kind == "response.completed":
                if in_think:
                    yield "__THINK_END__"
                body = event["response"]
                normalized, _ = _plan_response(body, {}, set())
                final_text = normalized["choices"][0]["message"]["content"]
                if not isinstance(final_text, str) or not final_text.strip():
                    raise PlanStreamError("ChatGPT Plan final response contained no completed text.",
                                          state="ambiguous")
                if visible_parts and "".join(visible_parts) != final_text:
                    raise PlanStreamError("ChatGPT Plan streamed text did not match its completed response.",
                                          state="ambiguous")
                if not visible_parts:
                    yield final_text
                fields = {"request_streamed": True, "stream_completion_state": "completed"}
                fields["http_status"] = event.get("_lumina_http_status")
                fields["provider_request_id"] = event.get("_lumina_request_id")
                if isinstance(body.get("usage"), dict):
                    fields["usage"] = _normalize_responses_usage(body)
                self._response_replay.clear()
                yield BackendStreamTelemetry(fields)
                return
        raise RuntimeError("ChatGPT Plan stream ended before response.completed.")
