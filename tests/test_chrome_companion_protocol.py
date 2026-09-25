"""BROWSER-COMPANION-01A -- wire protocol framing/validation and the Python
restricted-surface policy (the JS mirror is proven identical in
tests/test_chrome_companion_extension.py)."""
from __future__ import annotations

import io
import json
import struct

import pytest

from chrome_companion import policy, protocol
from chrome_companion.protocol import ProtocolError

CID = "a" * 32
RID = "b" * 32


def _framed(payload: bytes) -> bytes:
    return struct.pack("=I", len(payload)) + payload


def _request(**overrides):
    req = {"v": 1, "type": "request", "connection_id": CID, "request_id": RID, "op": "extract_text",
           "tab_id": 7, "deadline_ms": 1_900_000_000_000, "args": {"max_chars": 500}}
    req.update(overrides)
    return req


def _response(**overrides):
    res = {"v": 1, "type": "response", "connection_id": CID, "request_id": RID, "ok": True,
           "result": {"text": "hi", "total_chars": 2, "title": "t"}, "tab_id": 7,
           "observed": {"url": "https://example.test/", "origin": "https://example.test",
                        "document_id": "d1"},
           "truncated": False}
    res.update(overrides)
    return res


# ── Framing ──────────────────────────────────────────────────────────────

def test_valid_frame_round_trips_utf8():
    message = {"v": 1, "type": "bye", "connection_id": CID, "reason": "paused", "x": "Café ✓ 🙂"}
    frame = protocol.encode_frame(message, 4096)
    assert protocol.read_frame(io.BytesIO(frame).read, 4096) == message
    assert protocol.read_frame(io.BytesIO(b"").read, 4096) is None  # clean EOF


def test_truncated_header_and_payload_are_rejected():
    with pytest.raises(ProtocolError) as exc:
        protocol.read_frame(io.BytesIO(b"\x05\x00").read, 4096)
    assert exc.value.code == "truncated_frame"
    with pytest.raises(ProtocolError) as exc:
        protocol.read_frame(io.BytesIO(struct.pack("=I", 10) + b'{"a":').read, 4096)
    assert exc.value.code == "truncated_frame"


def test_oversized_frame_rejected_before_payload_is_read():
    reads = []

    def read(n):
        reads.append(n)
        if len(reads) > 1:
            raise AssertionError("payload must not be read after an oversized header")
        return struct.pack("=I", protocol.MAX_FROM_EXTENSION_BYTES + 1)

    with pytest.raises(ProtocolError) as exc:
        protocol.read_frame(read, protocol.MAX_FROM_EXTENSION_BYTES)
    assert exc.value.code == "frame_too_large"
    with pytest.raises(ProtocolError):
        protocol.encode_frame({"x": "y" * 100}, 50)


@pytest.mark.parametrize("payload, code", [
    (b"\xff\xfe{}", "invalid_utf8"),
    (b'{"a": ', "invalid_json"),
    (b"[1, 2]", "not_an_object"),
    (b'{"a": NaN}', "invalid_json"),
    (b'{"a": 1, "a": 2}', "invalid_json"),
    (b"[" * 100_000 + b"]" * 100_000, "invalid_json"),
])
def test_invalid_payloads_are_rejected(payload, code):
    with pytest.raises(ProtocolError) as exc:
        protocol.read_frame(io.BytesIO(_framed(payload)).read, 1_000_000)
    assert exc.value.code == code


def test_lone_surrogates_from_hostile_titles_are_scrubbed():
    decoded = protocol.decode_payload(b'{"title": "x\\ud83d"}')
    assert decoded["title"] == "x\ufffd"
    decoded["title"].encode("utf-8")  # would raise without the scrub


# ── Envelope validation ──────────────────────────────────────────────────

def test_valid_request_and_response_pass():
    protocol.validate_request(_request())
    protocol.validate_response(_response())
    protocol.validate_response(_response(ok=False, result=None,
                                         error={"code": "tab_closed", "message": "x"}))


@pytest.mark.parametrize("version", [2, 0, True, "1", None])
def test_unknown_protocol_versions_rejected(version):
    for validate, message in ((protocol.validate_request, _request(v=version)),
                              (protocol.validate_response, _response(v=version))):
        with pytest.raises(ProtocolError) as exc:
            validate(message)
        assert exc.value.code in {"unsupported_version", "bad_version"}


def test_unknown_operation_rejected():
    with pytest.raises(ProtocolError) as exc:
        protocol.validate_request(_request(op="click"))
    assert exc.value.code == "unknown_op"
    for action_op in ("type", "press_key", "navigate", "submit", "screenshot", "debugger"):
        assert action_op not in protocol.OPS


@pytest.mark.parametrize("overrides", [
    {"tab_id": True},                       # bool posing as int
    {"tab_id": -1},
    {"op": "get_tab", "tab_id": None},      # required tab missing
    {"op": "list_tabs", "tab_id": 3},       # tab not accepted
    {"args": {"max_chars": protocol.MAX_TEXT_CHARS + 1}},
    {"args": {"max_chars": 10, "evil": 1}},
    {"args": []},
    {"deadline_ms": 0},
    {"deadline_ms": 1.5},
    {"request_id": "not-hex"},
])
def test_malformed_requests_rejected(overrides):
    with pytest.raises(ProtocolError):
        protocol.validate_request(_request(**overrides))


def test_request_with_unknown_key_rejected():
    req = _request()
    req["extra"] = 1
    with pytest.raises(ProtocolError):
        protocol.validate_request(req)


@pytest.mark.parametrize("overrides", [
    {"ok": 1},
    {"truncated": "no"},
    {"tab_id": "7"},
    {"ok": False},                                    # failure without error
    {"error": {"code": "x", "message": "y"}},         # ok with error
    {"observed": {"url": "", "origin": "", "document_id": None}},
    {"observed": {"url": "https://x/", "origin": "https://x", "document_id": None, "cookie": "c"}},
])
def test_malformed_responses_rejected(overrides):
    with pytest.raises(ProtocolError):
        protocol.validate_response(_response(**overrides))


def test_error_codes_must_be_content_free_identifiers():
    bad = _response(ok=False, result=None,
                    error={"code": "Cannot access https://mail.google.com/x", "message": "m"})
    with pytest.raises(ProtocolError):
        protocol.validate_response(bad)


def test_result_typing_is_bounded():
    good_tab = {"tab_id": 1, "window_id": 1, "active": True, "incognito": False, "restricted": False,
                "restriction": None, "site_access": "granted", "status": "complete",
                "url": "https://example.test/", "title": "t"}
    protocol.validate_result("list_tabs", {"tabs": [good_tab], "total": 1})
    with pytest.raises(ProtocolError):
        protocol.validate_result("list_tabs", {"tabs": [good_tab] * (protocol.MAX_TABS + 1),
                                               "total": protocol.MAX_TABS + 1})
    with pytest.raises(ProtocolError):
        protocol.validate_result("extract_text", {"text": "x" * (protocol.MAX_TEXT_CHARS + 1),
                                                  "total_chars": 1, "title": None})
    with pytest.raises(ProtocolError):
        protocol.validate_result("get_links", {"links": [{"text": "a", "href": "https://x/",
                                                          "same_origin": "yes"}], "total_links": 1})
    with pytest.raises(ProtocolError):
        protocol.validate_result("get_tab", dict(good_tab, cookies="x"))


def test_extension_hello_must_match_chrome_origin():
    ext_id = "abcdefghijklmnopabcdefghijklmnop"
    hello = {"v": 1, "type": "hello", "extension_id": ext_id, "instance_id": "c" * 32,
             "extension_version": "0.1.0"}
    protocol.validate_extension_hello(hello, expected_extension_id=ext_id)
    with pytest.raises(ProtocolError):
        protocol.validate_extension_hello(dict(hello, extension_id="p" * 32), expected_extension_id=ext_id)
    with pytest.raises(ProtocolError):
        protocol.validate_extension_hello(dict(hello, instance_id="short"), expected_extension_id=ext_id)


# ── Restricted-surface policy (Python side) ──────────────────────────────

@pytest.mark.parametrize("url, reason", [
    ("chrome://settings/passwords", "restricted_scheme"),
    ("chrome://password-manager/passwords", "restricted_scheme"),
    ("chrome-extension://abcdefghijklmnopabcdefghijklmnop/popup.html", "restricted_scheme"),
    ("chrome-untrusted://terminal/", "restricted_scheme"),
    ("devtools://devtools/bundled/inspector.html", "restricted_scheme"),
    ("view-source:https://example.test/", "restricted_scheme"),
    ("file:///home/user/.ssh/id_ed25519", "restricted_scheme"),
    ("data:text/html,<h1>x</h1>", "restricted_scheme"),
    ("blob:https://example.test/uuid", "restricted_scheme"),
    ("about:blank", "restricted_scheme"),
    ("javascript:alert(1)", "restricted_scheme"),
    ("https://passwords.google.com/", "restricted_host"),
    ("https://pay.google.com/gp/w/home/paymentmethods", "restricted_host"),
    ("https://accounts.google.com/signin", "restricted_host"),
    ("https://chromewebstore.google.com/detail/x", "restricted_host"),
    ("", "invalid_url"),
    (None, "invalid_url"),
    ("https://", "invalid_url"),
])
def test_restricted_surfaces_are_refused(url, reason):
    verdict = policy.classify_url(url)
    assert not verdict.readable and verdict.reason == reason and verdict.origin is None


@pytest.mark.parametrize("url, origin, pattern", [
    ("https://www.reddit.com/r/AgentsInteractive/", "https://www.reddit.com", "https://www.reddit.com/*"),
    ("https://mail.google.com/mail/u/0/#inbox", "https://mail.google.com", "https://mail.google.com/*"),
    ("http://localhost:17493/app", "http://localhost:17493", "http://localhost/*"),
    ("https://example.test:443/x", "https://example.test", "https://example.test/*"),
    ("http://[::1]:8080/", "http://[::1]:8080", "http://[::1]/*"),
])
def test_readable_urls_yield_origin_and_permission_pattern(url, origin, pattern):
    verdict = policy.classify_url(url)
    assert verdict.readable and verdict.origin == origin and verdict.pattern == pattern


# BROWSER-COMPANION-01A-R1 / F2: equivalent DNS spellings of a restricted host
# stay restricted. Chrome's own permission matching trims every trailing dot.
RESTRICTED_ALIASES = [
    "https://accounts.google.com", "https://accounts.google.com./", "https://ACCOUNTS.GOOGLE.COM./",
    "https://passwords.google.com", "https://passwords.google.com./", "https://PASSWORDS.GOOGLE.COM./",
    "https://accounts.google.com../", "https://accounts.google.com.../signin",
    "https://accounts.google.com.:443/", "https://user@accounts.google.com./", "https://passwords.google./",
    "https://Pay.Google.Com./gp", "http://myaccount.google.com./",
]


@pytest.mark.parametrize("url", RESTRICTED_ALIASES)
def test_canonical_spellings_of_restricted_hosts_stay_restricted(url):
    verdict = policy.classify_url(url)
    assert not verdict.readable and verdict.reason == "restricted_host", url
    assert verdict.origin is None and verdict.pattern is None


@pytest.mark.parametrize("url", [
    "https://accounts.google%2Ecom/", "https://accounts%2egoogle.com/", "https://%61ccounts.google.com/",
    "https://accounts.google.com%2E/", "https://ａｃｃｏｕｎｔｓ.google.com/",
    "https://acc­ounts.google.com/", "https://.accounts.google.com/", "https://accounts..google.com/",
    "https://a!b.test/", "https://ex%61mple.test/",
])
def test_hosts_not_in_canonical_form_are_refused_not_guessed(url):
    # The hub never canonicalizes percent-escapes / IDN / full-width itself:
    # Chrome reports canonical hosts, so anything else is refused outright.
    verdict = policy.classify_url(url)
    assert not verdict.readable and verdict.reason == "invalid_url", url


@pytest.mark.parametrize("url, origin, pattern", [
    ("https://example.com./", "https://example.com", "https://example.com/*"),
    ("https://Example.COM../a", "https://example.com", "https://example.com/*"),
    ("http://127.0.0.1.:8080/", "http://127.0.0.1:8080", "http://127.0.0.1/*"),
    ("https://a_b.example.test/", "https://a_b.example.test", "https://a_b.example.test/*"),
])
def test_readable_hosts_use_the_canonical_host_for_origin_and_pattern(url, origin, pattern):
    verdict = policy.classify_url(url)
    assert verdict.readable and verdict.origin == origin and verdict.pattern == pattern


def test_policy_tables_are_json_serialisable_for_parity_checks():
    json.dumps({"schemes": list(policy.READABLE_SCHEMES), "hosts": sorted(policy.RESTRICTED_HOSTS)})
