"""BACKEND-ERROR-01 — backend HTTP failure-path regressions.

Defect (refined from the UTILITY-RUNTIME-01 recon): the four transports
with their own requests-based chat() implementations (lmstudio — inherited
by the cloud OpenAI-compatible subclasses — anthropic, gemini, ollama)
share one handler shape whose `except requests.exceptions.HTTPError`
branch dereferences the closure variable `resp`:

    resp = requests.post(...)          # may raise BEFORE assignment
    resp.raise_for_status()            # raises HTTPError AFTER assignment
except HTTPError as e:
    print(resp.text...)                # ← UnboundLocalError if post() raised

Source-vet refinement of the prior hypothesis: stock requests.post() can
raise ConnectionError / Timeout (caught by dedicated handlers that never
touch resp) or other RequestException subclasses (propagate raw, unmasked)
— but it cannot raise HTTPError itself. The masking is therefore reachable
exactly when an HTTPError escapes the transport call: custom transport
adapters, session hooks, or test doubles that raise HTTPError from post().
That is the case these tests pin: the original failure must survive —
never a secondary UnboundLocalError.

No live network: requests.post is monkeypatched in each backend's own
module namespace; every path is driven through the real production chat()
/ chat_stream() / complete_utility() code.
"""
import types

import pytest
import requests

import core.backends.anthropic_backend as anthropic_backend
import core.backends.gemini_backend as gemini_backend
import core.backends.lmstudio as lmstudio
import core.backends.ollama as ollama
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.ollama import OllamaBackend

# ── harness ──────────────────────────────────────────────────────────────

class FakeResponse:
    """Stands in for requests.Response on the success/HTTP-failure paths."""

    def __init__(self, status_code=200, body='{"choices": [{"message": {"content": "hi"}}]}',
                 raise_http=False):
        self.status_code = status_code
        self.text = body
        self._raise_http = raise_http
        self._parsed = {"choices": [{"message": {"content": "hi"}}]}

    def raise_for_status(self):
        if self._raise_http:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Server Error for url: fake",
                response=self,
            )

    def json(self):
        return self._parsed


def make_backend(kind):
    if kind == "lmstudio":
        return LMStudioBackend(base_url="http://local.invalid/v1"), lmstudio
    if kind == "anthropic":
        return AnthropicBackend(api_key="test-key"), anthropic_backend
    if kind == "gemini":
        return GeminiBackend(api_key="test-key"), gemini_backend
    if kind == "ollama":
        return OllamaBackend(base_url="http://local.invalid:11434/v1"), ollama
    raise ValueError(kind)


def patch_transport(monkeypatch, mod, post_behavior):
    """Stub the module's transport: `post_behavior` stands in for
    requests.post; requests.get returns a valid models list so get_model()
    resolves without network (lmstudio/ollama chat() call it first)."""
    monkeypatch.setattr(mod.requests, "post", post_behavior)

    models = FakeResponse()
    models._parsed = {"data": [{"id": "test-model"}]}
    monkeypatch.setattr(mod.requests, "get", lambda *a, **kw: models)


KINDS = ["lmstudio", "anthropic", "gemini", "ollama"]


def stream_first_chunk(gen):
    """chat_stream() is a generator — the transport failure raises on the
    first iteration, exactly as it does in production."""
    try:
        for _ in gen:
            break
    except StopIteration:
        pass


# ── A. pre-response transport failures: dedicated handlers, no masking ──

@pytest.mark.parametrize("kind", KINDS)
def test_pre_response_connection_error_normalized(kind, monkeypatch):
    """post() raising ConnectionError must surface as the backend's clean
    normalized ConnectionError — never a secondary UnboundLocalError."""
    backend, mod = make_backend(kind)

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError("connection refused")
    patch_transport(monkeypatch, mod, boom)

    with pytest.raises(ConnectionError, match="not reachable"):
        backend.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("kind", KINDS)
def test_pre_response_timeout_normalized(kind, monkeypatch):
    backend, mod = make_backend(kind)

    def boom(*a, **kw):
        raise requests.exceptions.Timeout("read timed out")
    patch_transport(monkeypatch, mod, boom)

    with pytest.raises(TimeoutError, match="timed out"):
        backend.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("kind", KINDS)
def test_pre_response_other_requestexception_propagates_unmasked(kind, monkeypatch):
    """RequestExceptions outside the dedicated handlers (e.g.
    ChunkedEncodingError from a mid-body disconnect) propagate raw —
    the original failure itself, never a handler-manufactured error."""
    backend, mod = make_backend(kind)

    def boom(*a, **kw):
        raise requests.exceptions.ChunkedEncodingError("connection broken mid-body")
    patch_transport(monkeypatch, mod, boom)

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        backend.chat([{"role": "user", "content": "hi"}])


# ── B. response-backed HTTP failure: full diagnostics preserved ─────────

@pytest.mark.parametrize("kind", KINDS)
def test_response_backed_http_error_delivers_provider_diagnostics(kind, monkeypatch):
    """The real production HTTP-4xx/5xx path: post() returns a response,
    raise_for_status() raises HTTPError with that response attached. The
    handler must keep delivering status diagnostics. Per-kind established
    contracts: lmstudio/anthropic/gemini include the provider body in the
    RuntimeError; ollama's carries the status line (str(e)) with the body
    going to the bounded terminal print — both preserve the failure."""
    backend, mod = make_backend(kind)
    resp = FakeResponse(status_code=429, body='{"error": "rate limited"}', raise_http=True)
    patch_transport(monkeypatch, mod, lambda *a, **kw: resp)

    with pytest.raises(RuntimeError) as excinfo:
        backend.chat([{"role": "user", "content": "hi"}])
    assert "429" in str(excinfo.value)
    if kind != "ollama":
        assert "rate limited" in str(excinfo.value)


# ── C. THE defect: HTTPError escaping the transport call itself ─────────

@pytest.mark.parametrize("kind", KINDS)
def test_httperror_from_transport_call_is_not_masked(kind, monkeypatch):
    """If an HTTPError escapes requests.post() directly (custom transport
    adapter, session hook, or strict-provider test double), the handler
    must convert it to the established RuntimeError carrying the ORIGINAL
    failure text — never manufacture an UnboundLocalError that erases it."""
    backend, mod = make_backend(kind)

    def boom(*a, **kw):
        raise requests.exceptions.HTTPError("502 upstream exploded at the adapter")
    patch_transport(monkeypatch, mod, boom)

    with pytest.raises(RuntimeError) as excinfo:
        backend.chat([{"role": "user", "content": "hi"}])
    assert "502 upstream exploded at the adapter" in str(excinfo.value)


@pytest.mark.parametrize("kind", KINDS)
def test_httperror_from_transport_call_not_masked_on_stream_path(kind, monkeypatch):
    """Same contract for chat_stream(): the generator raises on first
    iteration with the original failure preserved."""
    backend, mod = make_backend(kind)

    def boom(*a, **kw):
        raise requests.exceptions.HTTPError("503 adapter-level failure")
    patch_transport(monkeypatch, mod, boom)

    with pytest.raises(RuntimeError) as excinfo:
        stream_first_chunk(backend.chat_stream([{"role": "user", "content": "hi"}]))
    assert "503 adapter-level failure" in str(excinfo.value)


# ── success path unchanged ───────────────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_success_path_unchanged(kind, monkeypatch):
    backend, mod = make_backend(kind)
    patch_transport(monkeypatch, mod, lambda *a, **kw: FakeResponse())

    result = backend.chat([{"role": "user", "content": "hi"}])
    # parsed JSON dict, not raw text — the success contract is unchanged
    assert result == {"choices": [{"message": {"content": "hi"}}]}


# ── utility composition: never-raises contract holds under all failures ─

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ["connection", "httperror_direct"])
def test_complete_utility_never_raises_and_returns_none(kind, mode, monkeypatch):
    """UTILITY-RUNTIME-01's never-raises contract must hold for every
    failure mode — a network outage or adapter-level HTTPError becomes a
    None result, never an escaping exception of any kind."""
    backend, mod = make_backend(kind)

    if mode == "connection":
        def boom(*a, **kw):
            raise requests.exceptions.ConnectionError("connection refused")
    else:
        def boom(*a, **kw):
            raise requests.exceptions.HTTPError("502 upstream exploded")
    patch_transport(monkeypatch, mod, boom)

    assert backend.complete_utility(prompt="summarize", prefill="SUMMARY:") is None
