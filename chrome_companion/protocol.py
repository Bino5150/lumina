"""Chrome Companion wire protocol (BROWSER-COMPANION-01A), stdlib-only.

Shared by the Lumina-side hub (core/chrome_companion_hub.py) and the
Chrome-launched native host (chrome_companion/native_host.py). The MV3
extension (chrome_companion/extension/worker.js) implements the same
contract in JavaScript; tests/test_chrome_companion_e2e.py runs the real
worker, the real host and the real hub together so the two sides cannot
drift apart silently.

Framing on BOTH hops (Chrome <-> host stdio, host <-> hub Unix socket) is
Chrome Native Messaging's own format: a native-endian uint32 byte count
followed by that many bytes of UTF-8 JSON. Reusing it on the socket means
the host validates exactly one frame grammar.

Every message is a JSON object carrying ``v`` (protocol version) and
``type``. Message flow:

    extension -> host   hello     {extension_id, instance_id, extension_version}
    host -> extension   host_status {state: hub_connected | hub_unavailable}
    host -> hub         hello     {origin (Chrome's argv, not the page's),
                                   extension_id, instance_id,
                                   extension_version, host_version}
    hub -> extension    welcome   {connection_id, limits}   (or reject {reason})
    hub -> extension    request   {connection_id, request_id, op, tab_id,
                                   deadline_ms, args}
                                  (request_id = sequence number + random,
                                   see sequenced_request_id)
    extension -> hub    response  {connection_id, request_id, ok, result |
                                   error, tab_id, observed, truncated}
    extension -> hub    bye       {connection_id, reason}   (e.g. owner PAUSE)

Validation here is strict on purpose: unknown keys, wrong types, booleans
posing as integers, NaN/Infinity, duplicate JSON keys, invalid UTF-8 and
oversized frames are all rejected rather than coerced.
"""
from __future__ import annotations

import json
import re
import secrets
import struct

PROTOCOL_VERSION = 1
HOST_NAME = "org.lumina.chrome_companion"
HOST_VERSION = "1"

# Application-level frame caps, far below Chrome's own limits (currently
# 64 MiB Chrome->host and 1 MiB host->Chrome per message).
MAX_TO_EXTENSION_BYTES = 64 * 1024        # welcome / reject / request frames
MAX_FROM_EXTENSION_BYTES = 1024 * 1024    # hello / response / bye frames

# Observation bounds. The extension enforces these while extracting; the hub
# re-checks every returned value against them.
DEFAULT_TEXT_CHARS = 12_000
MAX_TEXT_CHARS = 30_000
DEFAULT_LINKS = 60
MAX_LINKS = 150
MAX_TABS = 100
MAX_TAB_URL_CHARS = 4096
MAX_LINK_URL_CHARS = 2048
MAX_TITLE_CHARS = 300
MAX_LINK_TEXT_CHARS = 200
MAX_ERROR_MESSAGE_CHARS = 500
MAX_IN_FLIGHT = 8

# Request ids (BROWSER-COMPANION-01A-R2 / P1). The first REQUEST_SEQ_HEX hex
# digits of a request_id are its sequence number on its connection, assigned
# by the hub in wire order starting at 1; the rest is random. The extension
# accepts a request only if its number is higher than every number that
# connection has already accepted, so a request_id executes at most once for
# the connection's whole life, with O(1) state and no cache to evict. A new
# connection has a new connection_id, so ids never carry across connections.
REQUEST_SEQ_HEX = 12
MAX_REQUEST_SEQ = 16 ** REQUEST_SEQ_HEX - 1

OPS = frozenset({"ping", "list_tabs", "get_active_tab", "get_tab", "extract_text", "get_links"})
# tab_id rules per op: required, optional (null = the active tab), or absent.
OPS_TAB_REQUIRED = frozenset({"get_tab"})
OPS_TAB_OPTIONAL = frozenset({"extract_text", "get_links"})
OPS_TAB_NONE = frozenset({"ping", "list_tabs", "get_active_tab"})
OP_ARGS = {
    "ping": {},
    "list_tabs": {},
    "get_active_tab": {},
    "get_tab": {},
    "extract_text": {"max_chars": (1, MAX_TEXT_CHARS)},
    "get_links": {"max_links": (1, MAX_LINKS)},
}

EXTENSION_TO_HUB_TYPES = frozenset({"response", "bye"})
HUB_TO_EXTENSION_TYPES = frozenset({"welcome", "reject", "request"})
REJECT_REASONS = frozenset({
    "bad_hello", "wrong_origin", "not_installed", "unpaired", "wrong_instance",
    "unsupported_version", "shutting_down",
})
SITE_ACCESS_VALUES = frozenset({"granted", "not_granted", "restricted"})
TAB_STATUS_VALUES = frozenset({"loading", "complete", "unloaded"})

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SURROGATES = re.compile("[\ud800-\udfff]")
_HEADER = struct.Struct("=I")


class ProtocolError(Exception):
    """A frame or message violated the contract. ``code`` is a stable,
    content-free identifier safe to record in telemetry."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def is_hex_id(value) -> bool:
    return isinstance(value, str) and bool(_HEX32.match(value))


def is_extension_id(value) -> bool:
    return isinstance(value, str) and bool(_EXTENSION_ID.match(value))


def sequenced_request_id(seq: int) -> str:
    if not _is_int(seq) or not 1 <= seq <= MAX_REQUEST_SEQ:
        raise ProtocolError("request_seq_exhausted")
    return f"{seq:0{REQUEST_SEQ_HEX}x}{secrets.token_hex((32 - REQUEST_SEQ_HEX) // 2)}"


def request_sequence(request_id) -> int | None:
    return int(request_id[:REQUEST_SEQ_HEX], 16) if is_hex_id(request_id) else None


def extension_origin(extension_id: str) -> str:
    if not is_extension_id(extension_id):
        raise ValueError("invalid Chrome extension ID")
    return f"chrome-extension://{extension_id}/"


def extension_id_from_origin(origin) -> str | None:
    if not isinstance(origin, str):
        return None
    prefix, suffix = "chrome-extension://", "/"
    if not (origin.startswith(prefix) and origin.endswith(suffix)):
        return None
    candidate = origin[len(prefix):-len(suffix)]
    return candidate if is_extension_id(candidate) else None


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def encode_frame(message: dict, max_bytes: int) -> bytes:
    if not isinstance(message, dict):
        raise ProtocolError("not_an_object")
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(payload) > max_bytes:
        raise ProtocolError("frame_too_large", f"{len(payload)} > {max_bytes}")
    return _HEADER.pack(len(payload)) + payload


def _reject_constant(name):
    raise ProtocolError("invalid_json", f"non-finite number {name}")


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolError("invalid_json", "duplicate key")
        out[key] = value
    return out


def _scrub(value):
    """Replace lone UTF-16 surrogates (legal as JSON \\u escapes, but not
    encodable as UTF-8) so a hostile page title can't crash a later
    encode() deep inside Lumina."""
    if isinstance(value, str):
        return _SURROGATES.sub("\ufffd", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    return value


def decode_payload(payload: bytes) -> dict:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid_utf8") from exc
    try:
        value = json.loads(text, parse_constant=_reject_constant,
                           object_pairs_hook=_no_duplicate_keys)
    except ProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ProtocolError("not_an_object")
    return _scrub(value)


def _read_exact(read, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = read(remaining)
        if not chunk:
            raise ProtocolError("truncated_frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame_sized(read, max_bytes: int) -> tuple[dict | None, int]:
    """Read one frame via ``read(n)`` (a stream's read or a socket's recv).
    Returns (None, 0) on a clean EOF at a frame boundary, else (message,
    payload byte count). The declared length is checked against
    ``max_bytes`` BEFORE any payload is read or allocated."""
    first = read(_HEADER.size)
    if not first:
        return None, 0
    header = first if len(first) == _HEADER.size else first + _read_exact(read, _HEADER.size - len(first))
    (size,) = _HEADER.unpack(header)
    if size > max_bytes:
        raise ProtocolError("frame_too_large", f"{size} > {max_bytes}")
    return decode_payload(_read_exact(read, size) if size else b""), size


def read_frame(read, max_bytes: int) -> dict | None:
    return read_frame_sized(read, max_bytes)[0]


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

def _is_int(value) -> bool:
    return type(value) is int


def _is_bool(value) -> bool:
    return type(value) is bool


def _bounded_str(value, limit: int, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    return isinstance(value, str) and len(value) <= limit


def _require_exact_keys(message: dict, required: set, optional: set = frozenset()) -> None:
    keys = set(message)
    missing = required - keys
    unknown = keys - required - optional
    if missing or unknown:
        raise ProtocolError("bad_shape", f"missing={sorted(missing)} unknown={sorted(unknown)}")


def _require_version(message: dict) -> None:
    version = message.get("v")
    if not _is_int(version):
        raise ProtocolError("bad_version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version", str(version))


def validate_extension_hello(message: dict, *, expected_extension_id: str) -> dict:
    """The first frame Chrome delivers from the extension to the host."""
    _require_version(message)
    _require_exact_keys(message, {"v", "type", "extension_id", "instance_id", "extension_version"})
    if message["type"] != "hello":
        raise ProtocolError("bad_hello", "first frame must be hello")
    if message["extension_id"] != expected_extension_id:
        raise ProtocolError("bad_hello", "extension_id does not match Chrome origin")
    if not is_hex_id(message["instance_id"]):
        raise ProtocolError("bad_hello", "instance_id")
    if not _bounded_str(message["extension_version"], 32) or not message["extension_version"]:
        raise ProtocolError("bad_hello", "extension_version")
    return message


def validate_host_hello(message: dict) -> dict:
    """The first frame the hub receives from a native host."""
    _require_version(message)
    _require_exact_keys(message, {"v", "type", "origin", "extension_id", "instance_id",
                                  "extension_version", "host_version"})
    if message["type"] != "hello":
        raise ProtocolError("bad_hello", "first frame must be hello")
    if extension_id_from_origin(message["origin"]) != message["extension_id"]:
        raise ProtocolError("bad_hello", "origin/extension_id mismatch")
    if not is_hex_id(message["instance_id"]):
        raise ProtocolError("bad_hello", "instance_id")
    for key in ("extension_version", "host_version"):
        if not _bounded_str(message[key], 32) or not message[key]:
            raise ProtocolError("bad_hello", key)
    return message


def validate_relay_type(message: dict, allowed: frozenset) -> dict:
    """The native host's type-level check on every relayed frame: right
    version, a type that may legally travel in this direction."""
    _require_version(message)
    if message.get("type") not in allowed:
        raise ProtocolError("unexpected_type", str(message.get("type"))[:32])
    return message


def build_request(*, connection_id: str, request_id: str, op: str, tab_id, deadline_ms: int,
                  args: dict) -> dict:
    message = {
        "v": PROTOCOL_VERSION, "type": "request", "connection_id": connection_id,
        "request_id": request_id, "op": op, "tab_id": tab_id,
        "deadline_ms": deadline_ms, "args": args,
    }
    validate_request(message)
    return message


def validate_request(message: dict) -> dict:
    """Python mirror of the extension's own request validation (worker.js
    validateRequest); the hub validates what it builds before sending."""
    _require_version(message)
    _require_exact_keys(message, {"v", "type", "connection_id", "request_id", "op", "tab_id",
                                  "deadline_ms", "args"})
    if message["type"] != "request":
        raise ProtocolError("unexpected_type")
    if not is_hex_id(message["connection_id"]) or not is_hex_id(message["request_id"]) \
            or request_sequence(message["request_id"]) < 1:
        raise ProtocolError("bad_id")
    op = message["op"]
    if op not in OPS:
        raise ProtocolError("unknown_op", str(op)[:32])
    tab_id = message["tab_id"]
    if op in OPS_TAB_REQUIRED and not (_is_int(tab_id) and tab_id >= 0):
        raise ProtocolError("invalid_args", "tab_id required")
    if op in OPS_TAB_OPTIONAL and not (tab_id is None or (_is_int(tab_id) and tab_id >= 0)):
        raise ProtocolError("invalid_args", "tab_id")
    if op in OPS_TAB_NONE and tab_id is not None:
        raise ProtocolError("invalid_args", "tab_id not accepted")
    if not _is_int(message["deadline_ms"]) or message["deadline_ms"] <= 0:
        raise ProtocolError("invalid_args", "deadline_ms")
    args = message["args"]
    if not isinstance(args, dict):
        raise ProtocolError("invalid_args", "args")
    spec = OP_ARGS[op]
    if set(args) - set(spec):
        raise ProtocolError("invalid_args", "unknown arg")
    for name, (low, high) in spec.items():
        if name in args and not (_is_int(args[name]) and low <= args[name] <= high):
            raise ProtocolError("invalid_args", name)
    return message


def validate_response(message: dict) -> dict:
    """Envelope-level validation of an extension response (op-specific
    result typing is validate_result())."""
    _require_version(message)
    _require_exact_keys(message, {"v", "type", "connection_id", "request_id", "ok", "tab_id",
                                  "observed", "truncated"}, {"result", "error"})
    if message["type"] != "response":
        raise ProtocolError("unexpected_type")
    if not is_hex_id(message["connection_id"]) or not is_hex_id(message["request_id"]):
        raise ProtocolError("bad_id")
    if not _is_bool(message["ok"]) or not _is_bool(message["truncated"]):
        raise ProtocolError("bad_shape", "ok/truncated")
    tab_id = message["tab_id"]
    if not (tab_id is None or (_is_int(tab_id) and tab_id >= 0)):
        raise ProtocolError("bad_shape", "tab_id")
    if message["ok"]:
        if "result" not in message or message.get("error") is not None:
            raise ProtocolError("bad_shape", "ok response needs result, no error")
    else:
        error = message.get("error")
        if message.get("result") is not None or not isinstance(error, dict):
            raise ProtocolError("bad_shape", "failed response needs error, no result")
        _require_exact_keys(error, {"code", "message"})
        if not isinstance(error["code"], str) or not _ERROR_CODE.match(error["code"]):
            raise ProtocolError("bad_shape", "error.code")
        if not _bounded_str(error["message"], MAX_ERROR_MESSAGE_CHARS):
            raise ProtocolError("bad_shape", "error.message")
    observed = message["observed"]
    if observed is not None:
        if not isinstance(observed, dict):
            raise ProtocolError("bad_shape", "observed")
        _require_exact_keys(observed, {"url", "origin", "document_id"})
        if not _bounded_str(observed["url"], MAX_TAB_URL_CHARS) or not observed["url"]:
            raise ProtocolError("bad_shape", "observed.url")
        if not _bounded_str(observed["origin"], MAX_TAB_URL_CHARS):
            raise ProtocolError("bad_shape", "observed.origin")
        if not _bounded_str(observed["document_id"], 128, allow_none=True):
            raise ProtocolError("bad_shape", "observed.document_id")
    return message


def validate_bye(message: dict) -> dict:
    _require_version(message)
    _require_exact_keys(message, {"v", "type", "connection_id", "reason"})
    if message["type"] != "bye" or not is_hex_id(message["connection_id"]):
        raise ProtocolError("bad_shape", "bye")
    if message["reason"] not in {"paused", "shutdown"}:
        raise ProtocolError("bad_shape", "bye.reason")
    return message


def _validate_tab(tab) -> dict:
    if not isinstance(tab, dict):
        raise ProtocolError("bad_result", "tab")
    _require_exact_keys(tab, {"tab_id", "window_id", "active", "incognito", "restricted",
                              "restriction", "site_access", "status", "url", "title"})
    if not (_is_int(tab["tab_id"]) and tab["tab_id"] >= 0 and _is_int(tab["window_id"])):
        raise ProtocolError("bad_result", "tab ids")
    for key in ("active", "incognito", "restricted"):
        if not _is_bool(tab[key]):
            raise ProtocolError("bad_result", key)
    if tab["site_access"] not in SITE_ACCESS_VALUES:
        raise ProtocolError("bad_result", "site_access")
    if tab["status"] is not None and tab["status"] not in TAB_STATUS_VALUES:
        raise ProtocolError("bad_result", "status")
    if not _bounded_str(tab["restriction"], 64, allow_none=True):
        raise ProtocolError("bad_result", "restriction")
    if not _bounded_str(tab["url"], MAX_TAB_URL_CHARS, allow_none=True):
        raise ProtocolError("bad_result", "url")
    if not _bounded_str(tab["title"], MAX_TITLE_CHARS, allow_none=True):
        raise ProtocolError("bad_result", "title")
    return tab


def validate_result(op: str, result):
    """Typed, bounded validation of a successful response's ``result``."""
    if op == "ping":
        if not isinstance(result, dict):
            raise ProtocolError("bad_result", "ping")
        _require_exact_keys(result, {"extension_version"})
        if not _bounded_str(result["extension_version"], 32):
            raise ProtocolError("bad_result", "extension_version")
        return result
    if op == "list_tabs":
        if not isinstance(result, dict):
            raise ProtocolError("bad_result", "list_tabs")
        _require_exact_keys(result, {"tabs", "total"})
        tabs = result["tabs"]
        if not isinstance(tabs, list) or len(tabs) > MAX_TABS:
            raise ProtocolError("bad_result", "tabs")
        if not (_is_int(result["total"]) and result["total"] >= len(tabs)):
            raise ProtocolError("bad_result", "total")
        for tab in tabs:
            _validate_tab(tab)
        return result
    if op == "get_active_tab":
        return None if result is None else _validate_tab(result)
    if op == "get_tab":
        return _validate_tab(result)
    if op == "extract_text":
        if not isinstance(result, dict):
            raise ProtocolError("bad_result", "extract_text")
        _require_exact_keys(result, {"text", "total_chars", "title"})
        if not _bounded_str(result["text"], MAX_TEXT_CHARS):
            raise ProtocolError("bad_result", "text")
        if not (_is_int(result["total_chars"]) and result["total_chars"] >= 0):
            raise ProtocolError("bad_result", "total_chars")
        if not _bounded_str(result["title"], MAX_TITLE_CHARS, allow_none=True):
            raise ProtocolError("bad_result", "title")
        return result
    if op == "get_links":
        if not isinstance(result, dict):
            raise ProtocolError("bad_result", "get_links")
        _require_exact_keys(result, {"links", "total_links"})
        links = result["links"]
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise ProtocolError("bad_result", "links")
        if not (_is_int(result["total_links"]) and result["total_links"] >= len(links)):
            raise ProtocolError("bad_result", "total_links")
        for link in links:
            if not isinstance(link, dict):
                raise ProtocolError("bad_result", "link")
            _require_exact_keys(link, {"text", "href", "same_origin"})
            if not _bounded_str(link["text"], MAX_LINK_TEXT_CHARS):
                raise ProtocolError("bad_result", "link.text")
            if not _bounded_str(link["href"], MAX_LINK_URL_CHARS) or not link["href"]:
                raise ProtocolError("bad_result", "link.href")
            if not _is_bool(link["same_origin"]):
                raise ProtocolError("bad_result", "link.same_origin")
        return result
    raise ProtocolError("unknown_op", str(op)[:32])
