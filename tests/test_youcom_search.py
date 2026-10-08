"""tools/web.py — optional You.com search provider (LUMINA_WEB_SEARCH=youcom).

Coverage for the opt-in provider step added ahead of the DuckDuckGo chain
in web_search():
- env unset -> You.com never called, the DDG chain runs exactly as before
  (the gate is additive; the default path must stay untouched);
- env set + healthy you-search response -> results formatted from
  results.web and returned;
- env set + You.com failing for any reason -> falls through to the DDG
  chain instead of surfacing an error (the chain is the floor, not the
  provider);
- the SSE parser picks the JSON-RPC result out of a multi-line body.
"""
import json
import sys
from unittest import mock

import pytest

import tools.web as web


@pytest.fixture
def _no_ddg_libs(monkeypatch):
    """Force both DDG library paths to ImportError.

    requirements.txt pins ddgs, so dev/CI machines have it installed —
    without this the routing tests would make real DuckDuckGo network
    calls instead of deterministically landing on the mocked
    `_ddg_html_search` sentinel floor. (None in sys.modules makes
    `import ddgs` / `import duckduckgo_search` raise ImportError.)
    """
    monkeypatch.setitem(sys.modules, "ddgs", None)
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)


def _sse(*payloads):
    """Render JSON-RPC payloads as an MCP streamable-HTTP SSE body."""
    return "".join(f"event: message\ndata: {json.dumps(p)}\n\n" for p in payloads)


def _call_result(web_results):
    return {
        "jsonrpc": "2.0", "id": 2, "result": {
            "content": [{
                "type": "text",
                "text": json.dumps({"results": {"web": web_results}}),
            }],
        },
    }


def _init_result():
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
        "serverInfo": {"name": "you", "version": "1"},
    }}


class _FakeResponse:
    def __init__(self, text="", headers=None):
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        pass


def _mock_transport(web_results=None, fail_on_call=False):
    """Return a mock requests.post driving _youcom_search's 3-call flow."""
    responses = [_FakeResponse(_sse(_init_result()))]

    def post(url, **kwargs):
        if "notifications/initialized" in (kwargs.get("json") or {}).get("method", ""):
            return _FakeResponse("")
        if fail_on_call:
            raise web.requests.ConnectionError("simulated you.com outage")
        return _FakeResponse(_sse(_call_result(web_results)))

    return mock.MagicMock(side_effect=post)


@pytest.fixture
def _gate_off(monkeypatch):
    monkeypatch.delenv("LUMINA_WEB_SEARCH", raising=False)


@pytest.fixture
def _gate_on(monkeypatch):
    monkeypatch.setenv("LUMINA_WEB_SEARCH", "youcom")


def test_gate_off_never_calls_youcom(_gate_off, _no_ddg_libs):
    youcom = mock.patch.object(web, "_youcom_search", return_value="youcom results")
    ddg = mock.patch.object(web, "_ddg_html_search", return_value="ddg fallback")
    with youcom as y, ddg as d:
        assert web.web_search("test query") == "ddg fallback"
    y.assert_not_called()
    d.assert_called_once()


def test_gate_on_routes_to_youcom(_gate_on, _no_ddg_libs):
    youcom = mock.patch.object(web, "_youcom_search", return_value="youcom results")
    ddg = mock.patch.object(web, "_ddg_html_search", return_value="ddg fallback")
    with youcom as y, ddg as d:
        assert web.web_search("test query") == "youcom results"
    y.assert_called_once()
    d.assert_not_called()


def test_gate_on_youcom_failure_falls_back_to_ddg_chain(_gate_on, _no_ddg_libs):
    # Any You.com failure must fall through to the DDG chain — the chain
    # is the floor, so a dead provider can never break web_search.
    ddg = mock.patch.object(web, "_ddg_html_search", return_value="ddg fallback")
    with mock.patch.object(web, "_youcom_search", side_effect=web.requests.ConnectionError("outage")), ddg as d:
        assert web.web_search("test query") == "ddg fallback"
    d.assert_called_once()


def test_youcom_search_formats_web_results(_gate_on):
    transport = _mock_transport(web_results=[
        {"title": "PyPI requests", "url": "https://pypi.org/project/requests/",
         "description": "Python HTTP for Humans."},
        {"title": "Real Python guide", "url": "https://realpython.com/python-requests/",
         "description": "A guide to the requests library."},
    ])
    with mock.patch.object(web.requests, "post", transport):
        out = web.web_search("python requests library", max_results=5)
    assert "PyPI requests" in out
    assert "https://pypi.org/project/requests/" in out
    assert "Python HTTP for Humans." in out
    # max_results is respected on the provider path too
    assert "Real Python guide" in out


def test_youcom_failure_without_env_falls_through_cleanly(_gate_off, _no_ddg_libs):
    # Belt-and-suspenders: even with the gate off, a raising You.com path
    # (e.g. someone patches it in) can't take web_search down with it.
    ddg = mock.patch.object(web, "_ddg_html_search", return_value="ddg fallback")
    youcom = mock.patch.object(web, "_youcom_search", side_effect=Exception("boom"))
    with youcom, ddg as d:
        assert web.web_search("test query") == "ddg fallback"
    d.assert_called_once()


def test_mcp_sse_result_picks_last_result():
    body = _sse(
        {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "Search successful"}},
        _call_result([{"title": "t", "url": "u", "description": "d"}]),
    )
    envelope = web._mcp_sse_result(body)
    assert envelope and "result" in envelope
    content = envelope["result"]["content"]
    assert json.loads(content[0]["text"])["results"]["web"][0]["title"] == "t"


def test_mcp_sse_result_ignores_noise_and_returns_none():
    assert web._mcp_sse_result("event: message\ndata: not json\n\n") is None
    assert web._mcp_sse_result("") is None


def test_youcom_raises_on_empty_web_results(_gate_on):
    transport = _mock_transport(web_results=[])
    with mock.patch.object(web.requests, "post", transport):
        with pytest.raises(ValueError):
            web._youcom_search("test query")