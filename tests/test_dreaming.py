"""F-62 quick fix: core/dreaming.py must resolve the actually-loaded model
instead of sending config.DEFAULT_MODEL (None by default) — the old code
was a guaranteed 400 on every dream sweep."""
import pytest
import core.dreaming as dreaming


@pytest.fixture(autouse=True)
def reset_cache():
    dreaming._resolved_model_cache["model"] = None
    yield
    dreaming._resolved_model_cache["model"] = None


def test_resolves_model_from_backend(monkeypatch):
    class FakeResponse:
        def json(self):
            return {"data": [{"id": "qwopus-3.5-v3-4b"}]}

    monkeypatch.setattr(dreaming.requests, "get", lambda *a, **k: FakeResponse())
    assert dreaming._resolve_model() == "qwopus-3.5-v3-4b"


def test_caches_after_first_resolution(monkeypatch):
    call_count = {"n": 0}

    class FakeResponse:
        def json(self):
            call_count["n"] += 1
            return {"data": [{"id": "qwopus-3.5-v3-4b"}]}

    monkeypatch.setattr(dreaming.requests, "get", lambda *a, **k: FakeResponse())
    dreaming._resolve_model()
    dreaming._resolve_model()
    dreaming._resolve_model()
    assert call_count["n"] == 1


def test_falls_back_to_default_model_when_backend_unreachable(monkeypatch):
    def broken_get(*a, **k):
        raise ConnectionError("backend down")

    monkeypatch.setattr(dreaming.requests, "get", broken_get)
    monkeypatch.setattr(dreaming.config, "DEFAULT_MODEL", "fallback-model")
    assert dreaming._resolve_model() == "fallback-model"


def test_never_sends_none_as_model_when_backend_reachable(monkeypatch):
    """The actual live bug: old code sent "model": None unconditionally."""
    class FakeResponse:
        def json(self):
            return {"data": [{"id": "real-loaded-model"}]}

    monkeypatch.setattr(dreaming.requests, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(dreaming.config, "DEFAULT_MODEL", None)
    resolved = dreaming._resolve_model()
    assert resolved is not None
    assert resolved == "real-loaded-model"
