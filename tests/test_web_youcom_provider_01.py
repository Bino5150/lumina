"""WEB-YOUCOM-PROVIDER-01 -- optional You.com step in the web_search chain.

Locks in the three behavior contracts of the YDC_API_KEY-gated You.com
provider in tools/web.py:

  1. OFF by default: with no YDC_API_KEY, _youcom_search() makes no API
     call, returns None, and web_search resolves through the existing
     keyless DuckDuckGo chain exactly as before.
  2. ON with a good response: results.web[] entries (url / title /
     description | snippets) are formatted by the shared _format_results.
  3. Fail-open: any request or response error returns None so web_search
     falls back to the keyless chain instead of surfacing a search error.

All network I/O is mocked -- nothing here talks to ydc-index.io or
DuckDuckGo. The keyless chain is "poisoned" per-test (sys.modules entry
None forces ImportError on the ddgs/duckduckgo_search imports, and
_ddg_html_search is replaced with a sentinel) so a stray live call fails
loudly instead of silently hitting the network.
"""

import sys

import tools.web as web


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _poison_keyless_chain(monkeypatch):
    """Make any real DuckDuckGo path both unreachable and loudly wrong."""
    monkeypatch.setitem(sys.modules, "ddgs", None)
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)
    monkeypatch.setattr(web, "_ddg_html_search", lambda q, m: "DDG-CHAIN-SENTINEL")


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("YDC_API_KEY", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("You.com API must not be called without YDC_API_KEY")

    monkeypatch.setattr(web.requests, "post", _boom)
    assert web._youcom_search("test query", 5) is None


def test_formats_results_when_keyed(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "test-key-123")
    payload = {
        "results": {
            "web": [
                {"url": "https://example.com/a", "title": "Result A", "description": "Desc A"},
                {"url": "https://example.com/b", "title": "Result B", "snippets": ["Snippet B"]},
                {"title": "No URL, skipped", "description": "no url"},
            ]
        }
    }
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(payload)

    monkeypatch.setattr(web.requests, "post", _fake_post)
    out = web._youcom_search("hello world", 5)

    assert out is not None
    assert "Result A" in out and "https://example.com/a" in out and "Desc A" in out
    assert "Result B" in out and "Snippet B" in out
    assert "No URL, skipped" not in out
    assert captured["url"] == "https://ydc-index.io/v1/search"
    assert captured["headers"]["X-API-Key"] == "test-key-123"
    assert captured["json"]["query"] == "hello world"
    assert captured["json"]["count"] == 5


def test_empty_web_results_return_none(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "test-key-123")
    monkeypatch.setattr(
        web.requests, "post", lambda *a, **k: _FakeResponse({"results": {"web": []}})
    )
    assert web._youcom_search("nothing", 5) is None


def test_fail_open_on_request_error(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "test-key-123")

    def _raise(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(web.requests, "post", _raise)
    assert web._youcom_search("query", 5) is None


def test_web_search_prefers_youcom_when_keyed(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "test-key-123")
    _poison_keyless_chain(monkeypatch)
    payload = {
        "results": {
            "web": [
                {"url": "https://example.com/x", "title": "Youcom Result", "description": "d"}
            ]
        }
    }
    monkeypatch.setattr(web.requests, "post", lambda *a, **k: _FakeResponse(payload))
    out = web.web_search("anything")
    assert "Youcom Result" in out
    assert "DDG-CHAIN-SENTINEL" not in out


def test_web_search_falls_through_without_key(monkeypatch):
    monkeypatch.delenv("YDC_API_KEY", raising=False)
    _poison_keyless_chain(monkeypatch)
    # No key set: web_search must reach the keyless chain untouched.
    assert web.web_search("anything") == "DDG-CHAIN-SENTINEL"
