"""tts/elevenlabs_bridge.py -- ElevenLabsBridge (MB-07).
Mocks requests.get/post throughout; no real network calls, no real API key
required for the suite to pass.
"""
import requests
import pytest
from tts.elevenlabs_bridge import ElevenLabsBridge


class _FakeResp:
    """Minimal stand-in for a requests.Response."""
    def __init__(self, status_code=200, content=b"", json_data=None, text=""):
        self.status_code = status_code
        self.content = content
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("response has no JSON body")
        return self._json


def _make_bridge(api_key="test-key", voice_id="default-voice-id",
                  model_id="eleven_turbo_v2_5", output_format="wav_22050"):
    """Build an ElevenLabsBridge with controlled attributes, bypassing
    __init__ (and therefore the real config module) entirely -- same
    pattern test_lmstudio_backend.py uses for its backend fixtures."""
    b = ElevenLabsBridge.__new__(ElevenLabsBridge)
    b.enabled = True
    b.api_key = api_key
    b.voice_id = voice_id
    b.model_id = model_id
    b.output_format = output_format
    b._voice_cache = {}
    return b


# ── speak() / _speak_worker() ───────────────────────────────────────────

def test_speak_posts_correct_url_header_params_and_payload(monkeypatch):
    bridge = _make_bridge(voice_id="voice123", model_id="eleven_turbo_v2_5",
                           output_format="wav_22050")
    captured = {}

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, params=params, json=json, timeout=timeout)
        return _FakeResp(status_code=200, content=b"AUDIOBYTES")
    monkeypatch.setattr(requests, "post", fake_post)

    played = {}
    bridge._play_audio = lambda audio_bytes: played.setdefault("bytes", audio_bytes)

    bridge.speak("Hello world", blocking=True)

    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voice123"
    assert captured["headers"] == {"xi-api-key": "test-key"}
    assert captured["params"] == {"output_format": "wav_22050"}
    assert captured["json"] == {"text": "Hello world", "model_id": "eleven_turbo_v2_5"}
    assert played["bytes"] == b"AUDIOBYTES"


def test_speak_calls_on_done_after_successful_play(monkeypatch):
    bridge = _make_bridge()
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResp(status_code=200, content=b"x"))
    bridge._play_audio = lambda b: None
    called = {"done": False}

    bridge.speak("hi", blocking=True, on_done=lambda: called.__setitem__("done", True))

    assert called["done"] is True


def test_speak_applies_phonetic_map_before_sending(monkeypatch):
    bridge = _make_bridge()
    captured = {}
    monkeypatch.setattr(requests, "post", lambda url, headers=None, params=None, json=None, timeout=None:
                         captured.update(json=json) or _FakeResp(status_code=200, content=b"x"))
    bridge._play_audio = lambda b: None

    bridge.speak("Bino asked Lumina a question.", blocking=True)

    assert captured["json"]["text"] == "Beeno asked Loo-mina a question."


def test_speak_resolves_voice_display_name_via_cache(monkeypatch):
    bridge = _make_bridge(voice_id="fallback-id")
    bridge._voice_cache = {"Rachel": "abc123"}
    captured = {}
    monkeypatch.setattr(requests, "post", lambda url, headers=None, params=None, json=None, timeout=None:
                         captured.update(url=url) or _FakeResp(status_code=200, content=b"x"))
    bridge._play_audio = lambda b: None

    bridge.speak("hi", blocking=True, voice_id="Rachel")

    assert captured["url"].endswith("/abc123")


def test_speak_resolves_stored_voice_name_set_via_set_voice(monkeypatch):
    """Regression: the Personas tab's voice dropdown calls set_voice(name)
    with a display name (e.g. "Rachel"), which lands on self.voice_id.
    A later speak() call with no per-call override used to send that name
    straight through as the URL path segment instead of resolving it via
    the cache -- this pins _resolve_voice_id() folding self.voice_id into
    the same resolution path."""
    bridge = _make_bridge(voice_id="placeholder")
    bridge._voice_cache = {"Rachel": "abc123"}
    bridge.set_voice("Rachel")
    captured = {}
    monkeypatch.setattr(requests, "post", lambda url, headers=None, params=None, json=None, timeout=None:
                         captured.update(url=url) or _FakeResp(status_code=200, content=b"x"))
    bridge._play_audio = lambda b: None

    bridge.speak("hi", blocking=True)  # no voice_id override -- must use self.voice_id

    assert captured["url"].endswith("/abc123")
    assert "/Rachel" not in captured["url"]


def test_speak_non_200_does_not_play_audio_and_logs_detail_message(monkeypatch, capsys):
    bridge = _make_bridge()
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResp(
        status_code=401,
        json_data={"detail": {"status": "invalid_api_key", "message": "Invalid API key"}},
        text="ignored",
    ))
    played = {"called": False}
    bridge._play_audio = lambda b: played.__setitem__("called", True)

    bridge.speak("hi", blocking=True)

    assert played["called"] is False
    assert "Invalid API key" in capsys.readouterr().err


def test_speak_non_200_falls_back_to_raw_text_if_body_not_json(monkeypatch, capsys):
    bridge = _make_bridge()
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResp(
        status_code=500, json_data=None, text="upstream exploded",
    ))
    bridge._play_audio = lambda b: pytest.fail("must not play audio on non-200")

    bridge.speak("hi", blocking=True)

    assert "upstream exploded" in capsys.readouterr().err


def test_speak_without_api_key_makes_no_request(monkeypatch):
    bridge = _make_bridge(api_key=None)
    called = {"post": False}
    monkeypatch.setattr(requests, "post", lambda *a, **kw: called.__setitem__("post", True))

    bridge.speak("hi", blocking=True)

    assert called["post"] is False


def test_speak_without_voice_id_configured_makes_no_request(monkeypatch):
    bridge = _make_bridge(voice_id=None)
    called = {"post": False}
    monkeypatch.setattr(requests, "post", lambda *a, **kw: called.__setitem__("post", True))

    bridge.speak("hi", blocking=True)

    assert called["post"] is False


# ── list_voices() ────────────────────────────────────────────────────────

def test_list_voices_parses_response_into_name_to_id_cache(monkeypatch):
    bridge = _make_bridge()
    resp_json = {"voices": [
        {"voice_id": "abc123", "name": "Rachel"},
        {"voice_id": "def456", "name": "Adam"},
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResp(status_code=200, json_data=resp_json))

    names = bridge.list_voices()

    assert names == ["Rachel", "Adam"]
    assert bridge._voice_cache == {"Rachel": "abc123", "Adam": "def456"}


def test_list_voices_non_200_returns_empty_list(monkeypatch):
    bridge = _make_bridge()
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResp(status_code=500))

    assert bridge.list_voices() == []


def test_list_voices_without_api_key_returns_empty_no_request(monkeypatch):
    bridge = _make_bridge(api_key=None)
    called = {"get": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("get", True))

    assert bridge.list_voices() == []
    assert called["get"] is False


# ── test() ───────────────────────────────────────────────────────────────

def test_test_returns_true_on_200(monkeypatch):
    bridge = _make_bridge()
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResp(status_code=200))

    assert bridge.test() is True


def test_test_returns_false_on_non_200(monkeypatch):
    bridge = _make_bridge()
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResp(status_code=401))

    assert bridge.test() is False


def test_test_returns_false_on_timeout(monkeypatch):
    bridge = _make_bridge()

    def raise_timeout(*a, **kw):
        raise requests.exceptions.Timeout("timed out")
    monkeypatch.setattr(requests, "get", raise_timeout)

    assert bridge.test() is False


def test_test_returns_false_on_connection_error(monkeypatch):
    bridge = _make_bridge()

    def raise_conn(*a, **kw):
        raise requests.exceptions.ConnectionError("unreachable")
    monkeypatch.setattr(requests, "get", raise_conn)

    assert bridge.test() is False


def test_test_missing_api_key_returns_false_cleanly_no_request(monkeypatch):
    bridge = _make_bridge(api_key=None)
    called = {"get": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("get", True))

    assert bridge.test() is False
    assert called["get"] is False


def test_test_empty_string_api_key_returns_false(monkeypatch):
    bridge = _make_bridge(api_key="")

    assert bridge.test() is False


# ── __init__ / config wiring ─────────────────────────────────────────────

def test_init_missing_config_attrs_fails_soft_no_crash(monkeypatch):
    """Simulates config.py before Part B landed -- getattr(..., None) must
    keep construction from raising, and test() must report not-reachable
    rather than crash."""
    import config as cfg
    monkeypatch.delattr(cfg, "ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delattr(cfg, "ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.delattr(cfg, "ELEVENLABS_MODEL", raising=False)
    monkeypatch.delattr(cfg, "ELEVENLABS_OUTPUT_FORMAT", raising=False)

    bridge = ElevenLabsBridge()

    assert bridge.api_key is None
    assert bridge.voice_id is None
    assert bridge.model_id is None
    assert bridge.output_format is None
    assert bridge.test() is False
