"""Foreground, text-only ChatGPT Plan Responses lane (01D-B).

The account's OAuth session is the sole credential source. This transport
never reads an API key, changes billing lanes, retries a request, or accepts
a stream without a positively completed terminal event.
"""

import json
import time
from typing import Optional

import requests

import config
from core import lane_preferences, persistence
from core.chatgpt_auth import ChatGPTAuthError, PlanPermission, SessionState, get_session_manager
from .base import BackendStreamTelemetry, ModelDiscoveryOutcome, ModelDiscoveryResult
from .lmstudio import join_endpoint, _iter_lines_safe
from .openai_backend import (
    OpenAIBackend, _ResponsesTiming, _normalize_responses_body,
    _normalize_responses_usage, _translate_messages_to_input,
)
from .reasoning import NO_REASONING_CONTROL


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

    def supports_tool_work(self) -> bool:
        return False

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

    def _payload(self, messages: list, grant) -> dict:
        model = self.get_model()
        if not model:
            raise ValueError("Choose a ChatGPT Plan model before sending a message.")
        result = self._catalog(grant)
        if result.outcome is not ModelDiscoveryOutcome.SUCCESS:
            raise RuntimeError(result.diagnostic)
        if model not in result.models:
            raise ValueError("Selected model is not available to this ChatGPT account. Reselect a model.")
        instructions, inputs = self._translate_messages_to_input(messages)
        return {"model": model, "instructions": instructions, "input": inputs,
                "store": False, "stream": True}

    def _events(self, messages: list):
        grant = self._grant()
        payload = self._payload(messages, grant)
        if not self._session().is_grant_current(grant):
            raise RuntimeError("ChatGPT account changed before dispatch. Try again.")
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
            terminal = False
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
                if kind == "response.completed":
                    body = event.get("response")
                    if not isinstance(body, dict) or body.get("status") != "completed":
                        raise PlanStreamError(
                            "ChatGPT Plan completion event lacked a completed response.",
                            http_status=response.status_code,
                            provider_request_id=response.headers.get("x-request-id"),
                        )
                    terminal = True
                    event["_lumina_http_status"] = response.status_code
                    event["_lumina_request_id"] = response.headers.get("x-request-id")
                    yield event
                    break
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
                yield event
            if not terminal:
                raise PlanStreamError(
                    "ChatGPT Plan stream ended before response.completed.",
                    http_status=response.status_code,
                    provider_request_id=response.headers.get("x-request-id"),
                )
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
        if tools or tool_choice_mode is not None:
            raise ValueError("ChatGPT Plan does not support tool work in this version.")
        timing = _ResponsesTiming(time.monotonic())
        for event in self._events(messages):
            now = time.monotonic()
            timing.observe(event, now)
            if event.get("type") == "response.completed":
                body = event["response"]
                telemetry = timing.snapshot(body, now) if capture_telemetry else None
                return _normalize_responses_body(body, telemetry=telemetry)
        raise RuntimeError("ChatGPT Plan stream ended before response.completed.")

    def chat_stream(self, messages: list, max_tokens: int = 4096,
                    temperature: float = 0.7, reasoning_effort: Optional[str] = None):
        in_think = False
        for event in self._events(messages):
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
                    yield delta
            elif kind == "response.completed":
                if in_think:
                    yield "__THINK_END__"
                body = event["response"]
                fields = {"request_streamed": True, "stream_completion_state": "completed"}
                fields["http_status"] = event.get("_lumina_http_status")
                fields["provider_request_id"] = event.get("_lumina_request_id")
                if isinstance(body.get("usage"), dict):
                    fields["usage"] = _normalize_responses_usage(body)
                yield BackendStreamTelemetry(fields)
                return
        raise RuntimeError("ChatGPT Plan stream ended before response.completed.")
