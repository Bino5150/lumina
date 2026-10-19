"""TELEGRAM-BOT-TOKEN-LOG-01: the Telegram bot token must not reach log output
or tool results, and Telegram errors must stay visible without it.

The Bot API carries the token in the URL path
(https://api.telegram.org/bot<TOKEN>/getUpdates). Before this fix:
  - httpx's INFO "HTTP Request: POST <url>" line printed the token to stderr
    on every getUpdates poll, because comms/telegram_bridge.py's
    basicConfig(level=INFO) sends INFO to stderr for the whole process;
  - python-telegram-bot's InvalidToken ("The token `<TOKEN>` was rejected by
    the server.") reached stderr as an uncaught traceback;
  - tools/telegram_send.py returned requests' exception text, which embeds
    the URL, as the tool result (model context, chat history, Flight
    Recorder result_summary).

The end-to-end tests run the real bridge module in a child process, so the
logging configuration under test is the module's own basicConfig, not
pytest's capture handlers. Transport is mocked (httpx.MockTransport,
requests adapter, failing getaddrinfo); nothing touches the network and the
token is fake.
"""
import logging
import os
import socket
import subprocess
import sys
import textwrap

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.models import Response

from comms import telegram_log_safety
from core import flight_recorder
from core.redaction import redact_secret_shapes, redact_telegram_bot_tokens
import tools.telegram_send as telegram_send

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Real Bot API token shape: numeric bot id, ":", 35 chars of [A-Za-z0-9_-].
FAKE_TOKEN = "7123456789:AAHfakeTOKENforTESTSonly_0123456-xy"
assert len(FAKE_TOKEN.split(":")[1]) == 35
BOT_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}"


# ── redaction primitives ──

@pytest.mark.parametrize("text", [
    f'HTTP Request: POST {BOT_URL}/getUpdates "HTTP/1.1 200 OK"',
    f"401 Client Error: Unauthorized for url: {BOT_URL}/sendMessage",
    "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
    f"with url: /bot{FAKE_TOKEN}/sendMessage (Caused by NameResolutionError(...))",
    f"The token `{FAKE_TOKEN}` was rejected by the server.",
    f"https://api.telegram.org/file/bot{FAKE_TOKEN}/photos/file_1.jpg",
    "https://api.telegram.org/bot" + FAKE_TOKEN.replace(":", "%3A") + "/getMe",
])
def test_redaction_removes_token_and_keeps_error_text(text):
    out = redact_telegram_bot_tokens(text)
    assert FAKE_TOKEN not in out
    assert FAKE_TOKEN.split(":")[1] not in out
    assert "[REDACTED]" in out
    # The diagnostic around the token survives.
    assert out.replace("[REDACTED]", "") == text.replace(FAKE_TOKEN, "").replace(
        FAKE_TOKEN.replace(":", "%3A"), "")


def test_percent_encoded_token_outside_a_bot_api_url_is_redacted():
    encoded = FAKE_TOKEN.replace(":", "%3A")
    out = redact_telegram_bot_tokens(f"retrying /proxy?u={encoded}&n=2")
    assert encoded not in out
    assert out == "retrying /proxy?u=[REDACTED]&n=2"


def test_url_path_backstop_catches_a_nonstandard_token():
    odd = "12:short"
    out = redact_telegram_bot_tokens(f"POST https://api.telegram.org/bot{odd}/getMe")
    assert odd not in out
    assert out == "POST https://api.telegram.org/bot[REDACTED]/getMe"


def test_redaction_leaves_ordinary_text_alone():
    text = ("HTTP Request: POST https://api.openai.com/v1/responses \"HTTP/1.1 200 OK\" "
            "chat 424242 at 12:30:45, robot/bottle")
    assert redact_telegram_bot_tokens(text) == text


def test_shared_secret_shapes_cover_telegram_tokens():
    """Flight Recorder (and every other redact_secret_shapes consumer) now
    knows the Telegram token shape too, as a backstop to the source fixes."""
    assert FAKE_TOKEN not in redact_secret_shapes(f"token is {FAKE_TOKEN}")
    assert FAKE_TOKEN not in flight_recorder.bounded_repr(f"[x {BOT_URL}/getMe]")


# ── logging filters ──

def _record(name, level, msg, *args, exc_info=None):
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def test_httpx_filter_drops_bot_api_request_lines():
    f = telegram_log_safety.TelegramHttpxRequestFilter()
    rec = _record("httpx", logging.INFO, 'HTTP Request: %s %s "%s %d %s"',
                  "POST", f"{BOT_URL}/getUpdates", "HTTP/1.1", 200, "OK")
    assert f.filter(rec) is False


def test_httpx_filter_keeps_other_hosts_untouched():
    f = telegram_log_safety.TelegramHttpxRequestFilter()
    rec = _record("httpx", logging.INFO, 'HTTP Request: %s %s "%s %d %s"',
                  "POST", "https://api.anthropic.com/v1/messages", "HTTP/1.1", 200, "OK")
    assert f.filter(rec) is True
    assert rec.getMessage() == ('HTTP Request: POST https://api.anthropic.com/v1/messages '
                                '"HTTP/1.1 200 OK"')


def test_httpx_filter_keeps_warnings_but_redacts_them():
    f = telegram_log_safety.TelegramHttpxRequestFilter()
    rec = _record("httpx", logging.WARNING, "trouble with %s", f"{BOT_URL}/getUpdates")
    assert f.filter(rec) is True
    assert FAKE_TOKEN not in rec.getMessage()
    assert "trouble with https://api.telegram.org/bot[REDACTED]/getUpdates" == rec.getMessage()


def test_root_handler_filter_redacts_message_and_traceback():
    try:
        raise RuntimeError(f"The token `{FAKE_TOKEN}` was rejected by the server.")
    except RuntimeError:
        exc_info = sys.exc_info()
    rec = _record("telegram.ext.Updater", logging.ERROR, "polling failed for %s",
                  f"{BOT_URL}/getUpdates", exc_info=exc_info)
    assert telegram_log_safety.TelegramTokenRedactingFilter().filter(rec) is True
    rendered = logging.Formatter("%(levelname)s:%(name)s:%(message)s").format(rec)
    assert FAKE_TOKEN not in rendered
    assert "ERROR:telegram.ext.Updater:polling failed" in rendered
    assert "RuntimeError: The token `[REDACTED]` was rejected by the server." in rendered


def test_install_is_idempotent():
    telegram_log_safety.install()
    telegram_log_safety.install()
    httpx_filters = [f for f in logging.getLogger("httpx").filters
                     if isinstance(f, telegram_log_safety.TelegramHttpxRequestFilter)]
    assert len(httpx_filters) == 1


def test_redacted_exception_text_covers_the_cause_chain():
    try:
        try:
            raise ValueError(f"inner {BOT_URL}/getMe")
        except ValueError as inner:
            raise RuntimeError(f"outer `{FAKE_TOKEN}`") from inner
    except RuntimeError as exc:
        text = telegram_log_safety.redacted_exception_text(exc)
    assert FAKE_TOKEN not in text
    assert "ValueError: inner https://api.telegram.org/bot[REDACTED]/getMe" in text
    assert "RuntimeError: outer `[REDACTED]`" in text


# ── real bridge, real module logging config, child process ──

_CHILD = textwrap.dedent('''
    import asyncio, logging, runpy, sys, threading, time
    import httpx

    mode, scenario = sys.argv[1], sys.argv[2]

    async def handler(request):
        path = request.url.path
        if path.endswith("/getMe"):
            if scenario == "invalid":
                return httpx.Response(401, json={"ok": False, "error_code": 401,
                                                 "description": "Unauthorized"})
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 7123456789, "is_bot": True, "first_name": "Fake", "username": "fake_bot"}})
        if path.endswith("/getUpdates"):
            if scenario == "neterr":
                raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution",
                                         request=request)
            await asyncio.sleep(0.2)
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    _orig_init = httpx.AsyncClient.__init__
    def _init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        _orig_init(self, *a, **kw)
    httpx.AsyncClient.__init__ = _init

    import config
    config.TELEGRAM_OWNER_CHAT_ID = 424242

    if mode == "log":
        # A third-party record carrying the token (message and traceback),
        # emitted after the bridge module configured logging: the root
        # handler's filter must redact it.
        import comms.telegram_bridge  # noqa: F401
        token = sys.argv[3]
        try:
            raise RuntimeError("The token `%s` was rejected by the server." % token)
        except RuntimeError:
            logging.getLogger("telegram.ext.Updater").error(
                "Exception happened while polling https://api.telegram.org/bot%s/getUpdates",
                token, exc_info=True)
        sys.exit(0)

    if mode == "standalone":
        runpy.run_module("comms.telegram_bridge", run_name="__main__", alter_sys=True)
        sys.exit(0)

    import comms.telegram_bridge as tb
    # Another httpx user in the same process (a model provider): its
    # request line must still print.
    logging.getLogger("httpx").info('HTTP Request: POST https://api.openai.com/v1/responses "HTTP/1.1 200 OK"')
    print("start:", tb.start_bridge(), flush=True)
    time.sleep(1.5)
    print("stop:", tb.stop_bridge(), flush=True)
''')


def _run_child(tmp_path, mode, scenario="", timeout=30):
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    creds = tmp_path / "credentials.json"
    creds.write_text('{"telegram_bot_token": "%s"}' % FAKE_TOKEN)
    os.chmod(creds, 0o600)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # TELEGRAM-MASTER-SWITCH-01: the owner has remote access ON in this
    # child's own prefs (it defaults to OFF, which refuses every start).
    (data_dir / "memory").mkdir()
    (data_dir / "memory" / "prefs.json").write_text('{"telegram_remote_access": true}')
    env = dict(os.environ)
    # Own data dir and credentials: no emergency-stop latch, prefs or chat
    # state from other tests reaches the child.
    env["LUMINA_DATA_DIR"] = str(data_dir)
    env["LUMINA_SECRETS_PATH"] = str(creds)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-u", str(script), mode, scenario, FAKE_TOKEN],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout,
    )
    return proc, proc.stdout + proc.stderr


def _assert_no_token(output):
    assert FAKE_TOKEN not in output
    assert FAKE_TOKEN.split(":")[1] not in output


def test_embedded_bridge_polling_never_prints_the_token(tmp_path):
    proc, out = _run_child(tmp_path, "embedded", "ok")
    assert proc.returncode == 0, out
    assert "start: (True, 'Bridge started.')" in out
    assert "[TELEGRAM] Bridge started (embedded)." in out
    assert "stop: (True, 'Bridge stopped.')" in out
    _assert_no_token(out)
    assert "api.telegram.org/bot" not in out
    # Narrow: other hosts' httpx request lines are unaffected.
    assert 'INFO:httpx:HTTP Request: POST https://api.openai.com/v1/responses "HTTP/1.1 200 OK"' in out


def test_embedded_bridge_invalid_token_is_visible_without_the_token(tmp_path):
    proc, out = _run_child(tmp_path, "embedded", "invalid")
    assert proc.returncode == 0, out
    _assert_no_token(out)
    assert "ERROR:telegram_bridge:[TELEGRAM] Bridge stopped on an error:" in out
    assert "telegram.error.InvalidToken: The token `[REDACTED]` was rejected by the server." in out
    assert "stop: (False, 'Bridge not running.')" in out


def test_embedded_bridge_network_errors_stay_visible(tmp_path):
    proc, out = _run_child(tmp_path, "embedded", "neterr")
    assert proc.returncode == 0, out
    _assert_no_token(out)
    assert "ERROR:telegram.ext.Updater:Exception happened while polling for updates." in out
    assert "telegram.error.NetworkError: httpx.ConnectError: [Errno -3] Temporary failure in name resolution" in out


def test_standalone_bridge_invalid_token_exits_nonzero_without_the_token(tmp_path):
    proc, out = _run_child(tmp_path, "standalone", "invalid")
    assert proc.returncode == 1, out
    _assert_no_token(out)
    assert "telegram.error.InvalidToken: The token `[REDACTED]` was rejected by the server." in out


def test_bridge_logging_config_redacts_third_party_records(tmp_path):
    proc, out = _run_child(tmp_path, "log")
    assert proc.returncode == 0, out
    _assert_no_token(out)
    assert ("ERROR:telegram.ext.Updater:Exception happened while polling "
            "https://api.telegram.org/bot[REDACTED]/getUpdates") in out
    assert "RuntimeError: The token `[REDACTED]` was rejected by the server." in out


# ── tools/telegram_send.py (requests) ──

@pytest.fixture
def send_env(monkeypatch):
    monkeypatch.setattr(telegram_send, "get_secret", lambda k: FAKE_TOKEN)
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: 424242)
    # TELEGRAM-MASTER-SWITCH-01: remote access ON, a listener running (so no
    # reply-path note is appended); the outbound auto-start helper is gone.
    from comms import telegram_access
    monkeypatch.setattr(telegram_access, "remote_access_enabled", lambda: True)
    monkeypatch.setattr(telegram_send, "_listener_running", lambda: True)
    monkeypatch.setattr(telegram_send, "check", lambda rid: None)
    monkeypatch.setattr(telegram_send, "record", lambda rid, result: None)


def _http_401(self, request, **kwargs):
    resp = Response()
    resp.status_code = 401
    resp.reason = "Unauthorized"
    resp.url = request.url
    resp.request = request
    resp._content = b'{"ok":false,"error_code":401,"description":"Unauthorized"}'
    return resp


def test_send_message_http_error_result_has_no_token(send_env, monkeypatch, tmp_path):
    monkeypatch.setattr(HTTPAdapter, "send", _http_401)
    result = telegram_send.send_telegram_message("hello")
    _assert_no_token(result)
    assert result == ("[Telegram send error: 401 Client Error: Unauthorized for url: "
                      "https://api.telegram.org/bot[REDACTED]/sendMessage]")

    doc = tmp_path / "a.txt"
    doc.write_text("x")
    result = telegram_send.send_telegram_file(str(doc))
    _assert_no_token(result)
    assert "401 Client Error: Unauthorized for url: https://api.telegram.org/bot[REDACTED]/sendDocument" in result


def test_send_message_connection_error_result_has_no_token(send_env, monkeypatch):
    def _no_dns(*a, **k):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    result = telegram_send.send_telegram_message("hello")
    _assert_no_token(result)
    assert "Max retries exceeded with url: /bot[REDACTED]/sendMessage" in result
    assert "Temporary failure in name resolution" in result
    # The tool result is what the Flight Recorder summarizes.
    _assert_no_token(flight_recorder.bounded_repr(result))
