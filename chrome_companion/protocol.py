"""Chrome Companion wire protocol (01A reads and 01B-A navigation), stdlib-only.

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

from chrome_companion import interaction, policy

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

# BROWSER-COMPANION-COMPLETION-01 Slice 1: generic observation. An element
# reference ("e12") names one element the extension registered in its
# isolated world for ONE document; it is a claim to be re-verified, never a
# selector. Everything the page supplies is bounded here and re-checked by
# the hub.
DEFAULT_OBSERVE_ITEMS = 40
MAX_OBSERVE_ITEMS = 120
MAX_QUERY_CHARS = 100
MAX_ELEMENT_NAME_CHARS = 120
MAX_ELEMENT_CONTEXT_CHARS = 160
MAX_ELEMENT_HREF_CHARS = 512
MAX_ELEMENT_VALUE_CHARS = 80
MAX_ROLE_CHARS = 32
MAX_HEADINGS = 12
MAX_NAVIGATE_SCOPE = 16
SCROLL_VALUES = frozenset({"up", "down", "top", "bottom"})
ELEMENT_KINDS = frozenset({"link", "button", "field", "toggle", "option", "other"})
ELEMENT_STATES = frozenset({"disabled", "checked", "unchecked", "mixed", "expanded", "collapsed",
                            "selected", "pressed", "required", "readonly", "has_value"})
NAVIGATE_KINDS = frozenset({"url", "ref", "back", "forward"})
_REF = re.compile(r"^e[1-9][0-9]{0,6}$")

# Request ids (BROWSER-COMPANION-01A-R2 / P1). The first REQUEST_SEQ_HEX hex
# digits of a request_id are its sequence number on its connection, assigned
# by the hub in wire order starting at 1; the rest is random. The extension
# accepts a request only if its number is higher than every number that
# connection has already accepted, so a request_id executes at most once for
# the connection's whole life, with O(1) state and no cache to evict. A new
# connection has a new connection_id, so ids never carry across connections.
REQUEST_SEQ_HEX = 12
MAX_REQUEST_SEQ = 16 ** REQUEST_SEQ_HEX - 1

OPS = frozenset({"ping", "list_tabs", "get_active_tab", "get_tab", "extract_text", "get_links",
                 "open_owner_url", "switch_tab", "follow_link", "observe", "navigate", "interact"})
# tab_id rules per op: required, optional (null = the active tab), or absent.
# navigate: per kind (validate_request) -- only kind "url" may omit it (new tab).
OPS_TAB_REQUIRED = frozenset({"get_tab", "switch_tab", "follow_link", "interact"})
OPS_TAB_OPTIONAL = frozenset({"extract_text", "get_links", "observe", "navigate"})
OPS_TAB_NONE = frozenset({"ping", "list_tabs", "get_active_tab", "open_owner_url"})
OP_ARGS = {
    "ping": {},
    "list_tabs": {},
    "get_active_tab": {},
    "get_tab": {},
    "extract_text": {"max_chars": (1, MAX_TEXT_CHARS)},
    "get_links": {"max_links": (1, MAX_LINKS)},
    # Action arguments are checked separately below; they are strings and
    # identities, not the bounded integer arguments of the 01A read ops.
    "open_owner_url": {},
    "switch_tab": {},
    # Stage 2 (FINGER). A semantic CLAIM about one already-observed link.
    # ``href`` is never a destination here: the extension re-reads the live
    # document and navigates only to what that document itself reports.
    "follow_link": {},
    # Slice 1 generic ops; checked separately below.
    "observe": {},
    "navigate": {},
    "interact": {},
}


def validate_interact_args(args: dict) -> None:
    kind = args.get("kind")
    required = {"kind", "window_id", "document_id", "ref", "gen", "fixture_url"}
    if kind in {"set_checked", "set_expanded"}:
        required.add("value")
    if (kind not in interaction.KINDS or set(args) != required
            or not _is_int(args.get("window_id")) or args["window_id"] < 0
            or not _bounded_str(args.get("document_id"), 128) or not args["document_id"]
            or not is_ref(args.get("ref"))
            or not _is_int(args.get("gen")) or args["gen"] < 1
            or not interaction.is_fixture_url(args.get("fixture_url"))
            or ("value" in args and not _is_bool(args["value"]))):
        raise ProtocolError("invalid_args", "fixture interaction identity")


def is_ref(value) -> bool:
    return isinstance(value, str) and bool(_REF.match(value))


def is_site_pattern(value) -> bool:
    """A Chrome host-permission pattern exactly as classify_url derives one."""
    return (isinstance(value, str) and len(value) <= 300 and value.endswith("/*")
            and policy.classify_url(value[:-1]).pattern == value)


def _validate_observe_args(args: dict) -> None:
    if set(args) - {"max_items", "query", "since_gen", "scroll"}:
        raise ProtocolError("invalid_args", "unknown observe arg")
    if "max_items" in args and not (_is_int(args["max_items"]) and 1 <= args["max_items"] <= MAX_OBSERVE_ITEMS):
        raise ProtocolError("invalid_args", "max_items")
    if "query" in args and not (_bounded_str(args["query"], MAX_QUERY_CHARS) and args["query"]):
        raise ProtocolError("invalid_args", "query")
    if "since_gen" in args and not (_is_int(args["since_gen"]) and args["since_gen"] >= 0):
        raise ProtocolError("invalid_args", "since_gen")
    if "scroll" in args and args["scroll"] not in SCROLL_VALUES:
        raise ProtocolError("invalid_args", "scroll")


def _validate_navigate_args(tab_id, args: dict) -> None:
    kind = args.get("kind")
    if kind not in NAVIGATE_KINDS:
        raise ProtocolError("invalid_args", "navigate kind")
    bound = {"window_id", "document_id"}  # the exact document being navigated FROM
    if kind == "url":
        required = {"kind", "url", "scope"} | (bound if tab_id is not None else set())
    elif kind == "ref":
        required = {"kind", "ref", "scope"} | bound
    else:
        required = {"kind"} | bound
    if set(args) != required:
        raise ProtocolError("invalid_args", "navigate shape")
    if kind != "url" and tab_id is None:
        raise ProtocolError("invalid_args", "tab_id required")
    if "window_id" in args and not (_is_int(args["window_id"]) and args["window_id"] >= 0):
        raise ProtocolError("invalid_args", "window_id")
    if "document_id" in args and not (_bounded_str(args["document_id"], 128) and args["document_id"]):
        raise ProtocolError("invalid_args", "document_id")
    if kind == "url" and not (_bounded_str(args["url"], MAX_TAB_URL_CHARS)
                              and policy.classify_url(args["url"]).readable):
        raise ProtocolError("invalid_args", "url")
    if kind == "ref" and not is_ref(args["ref"]):
        raise ProtocolError("invalid_args", "ref")
    if "scope" in args:
        scope = args["scope"]
        if not isinstance(scope, list) or len(scope) > MAX_NAVIGATE_SCOPE \
                or not all(is_site_pattern(p) for p in scope) or len(set(scope)) != len(scope):
            raise ProtocolError("invalid_args", "scope")

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
    if op == "ping":
        if args != {} and not (set(args) == {"include_navigation"} and args["include_navigation"] is True):
            raise ProtocolError("invalid_args", "ping args")
        return message
    if op == "open_owner_url":
        if set(args) != {"url"} or not _bounded_str(args.get("url"), MAX_TAB_URL_CHARS) \
                or not args["url"]:
            raise ProtocolError("invalid_args", "url")
        return message
    if op == "switch_tab":
        if set(args) != {"window_id", "expected_url"} \
                or not _is_int(args.get("window_id")) \
                or not _bounded_str(args.get("expected_url"), MAX_TAB_URL_CHARS) \
                or not args["expected_url"]:
            raise ProtocolError("invalid_args", "tab identity")
        return message
    if op == "follow_link":
        if set(args) != {"window_id", "text", "href", "document_id"} \
                or not _is_int(args.get("window_id")) or args.get("window_id", -1) < 0 \
                or not _bounded_str(args.get("text"), MAX_LINK_TEXT_CHARS) \
                or not _bounded_str(args.get("href"), MAX_LINK_URL_CHARS) \
                or not args["href"] \
                or not _bounded_str(args.get("document_id"), 128) \
                or not args["document_id"]:
            raise ProtocolError("invalid_args", "link identity")
        # Mirror of the extension's own refusal (worker.js validateRequest):
        # a claim built from a javascript:/data:/restricted URL is not a link
        # this companion will ever follow. Both sides agree, so neither can
        # drift into accepting what the other refuses.
        if not policy.classify_url(args["href"]).readable:
            raise ProtocolError("invalid_args", "link identity")
        return message
    if op == "observe":
        _validate_observe_args(args)
        return message
    if op == "navigate":
        _validate_navigate_args(tab_id, args)
        return message
    if op == "interact":
        validate_interact_args(args)
        return message
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


def _validate_element(element) -> dict:
    if not isinstance(element, dict):
        raise ProtocolError("bad_result", "element")
    _require_exact_keys(element, {"ref", "role", "name", "kind", "context", "href", "same_origin",
                                  "state", "value", "sensitive", "in_viewport"})
    if not is_ref(element["ref"]):
        raise ProtocolError("bad_result", "element.ref")
    if not _bounded_str(element["role"], MAX_ROLE_CHARS) or element["kind"] not in ELEMENT_KINDS:
        raise ProtocolError("bad_result", "element.role")
    if not _bounded_str(element["name"], MAX_ELEMENT_NAME_CHARS) \
            or not _bounded_str(element["context"], MAX_ELEMENT_CONTEXT_CHARS):
        raise ProtocolError("bad_result", "element.text")
    if element["href"] is None:
        if element["same_origin"] is not None:
            raise ProtocolError("bad_result", "element.same_origin")
    elif not (_bounded_str(element["href"], MAX_ELEMENT_HREF_CHARS) and element["href"]
              and _is_bool(element["same_origin"])):
        raise ProtocolError("bad_result", "element.href")
    state = element["state"]
    if not isinstance(state, list) or len(state) > len(ELEMENT_STATES) \
            or not all(s in ELEMENT_STATES for s in state) or len(set(state)) != len(state):
        raise ProtocolError("bad_result", "element.state")
    if not _is_bool(element["sensitive"]) or not _is_bool(element["in_viewport"]):
        raise ProtocolError("bad_result", "element flags")
    # A credential/payment field's value never crosses the bridge.
    if not _bounded_str(element["value"], MAX_ELEMENT_VALUE_CHARS, allow_none=True) \
            or (element["sensitive"] and element["value"] is not None):
        raise ProtocolError("bad_result", "element.value")
    return element


def _validate_observation(result) -> dict:
    if not isinstance(result, dict):
        raise ProtocolError("bad_result", "observe")
    _require_exact_keys(result, {"gen", "title", "elements", "total_elements", "removed", "headings",
                                 "viewport", "dialog", "scrolled"})
    if not (_is_int(result["gen"]) and result["gen"] >= 1):
        raise ProtocolError("bad_result", "gen")
    if not _bounded_str(result["title"], MAX_TITLE_CHARS):
        raise ProtocolError("bad_result", "title")
    elements = result["elements"]
    if not isinstance(elements, list) or len(elements) > MAX_OBSERVE_ITEMS:
        raise ProtocolError("bad_result", "elements")
    for element in elements:
        _validate_element(element)
    if len({e["ref"] for e in elements}) != len(elements):
        raise ProtocolError("bad_result", "duplicate ref")
    if not (_is_int(result["total_elements"]) and result["total_elements"] >= len(elements)):
        raise ProtocolError("bad_result", "total_elements")
    removed = result["removed"]
    if not isinstance(removed, list) or len(removed) > MAX_OBSERVE_ITEMS or not all(is_ref(r) for r in removed):
        raise ProtocolError("bad_result", "removed")
    headings = result["headings"]
    if not isinstance(headings, list) or len(headings) > MAX_HEADINGS:
        raise ProtocolError("bad_result", "headings")
    for heading in headings:
        if not isinstance(heading, dict):
            raise ProtocolError("bad_result", "heading")
        _require_exact_keys(heading, {"level", "text"})
        if not (_is_int(heading["level"]) and 1 <= heading["level"] <= 6) \
                or not _bounded_str(heading["text"], MAX_ELEMENT_NAME_CHARS):
            raise ProtocolError("bad_result", "heading")
    viewport = result["viewport"]
    if not isinstance(viewport, dict):
        raise ProtocolError("bad_result", "viewport")
    _require_exact_keys(viewport, {"scroll_y", "scroll_height", "viewport_height"})
    if not all(_is_int(viewport[k]) and viewport[k] >= 0 for k in viewport):
        raise ProtocolError("bad_result", "viewport")
    if not _bounded_str(result["dialog"], MAX_ELEMENT_NAME_CHARS, allow_none=True) \
            or not _is_bool(result["scrolled"]):
        raise ProtocolError("bad_result", "dialog/scrolled")
    return result


def validate_result(op: str, result):
    """Typed, bounded validation of a successful response's ``result``."""
    if op == "ping":
        if not isinstance(result, dict):
            raise ProtocolError("bad_result", "ping")
        _require_exact_keys(result, {"extension_version"}, {"navigation_allowed"})
        if not _bounded_str(result["extension_version"], 32):
            raise ProtocolError("bad_result", "extension_version")
        if "navigation_allowed" in result and not _is_bool(result["navigation_allowed"]):
            raise ProtocolError("bad_result", "navigation_allowed")
        return result
    if op == "observe":
        return _validate_observation(result)
    if op in {"open_owner_url", "switch_tab", "follow_link", "navigate", "interact"}:
        if not isinstance(result, dict):
            raise ProtocolError("bad_result", "action")
        required = {"operation_id", "status", "tab_id", "window_id", "observed_url", "load_confirmed"}
        if op == "interact":
            required.update({"kind", "ref", "gen", "before", "after"})
            if (result.get("kind") not in interaction.KINDS or not is_ref(result.get("ref"))
                    or not _is_int(result.get("gen")) or result["gen"] < 1
                    or any(result.get(k) is not None and not _is_bool(result[k]) for k in ("before", "after"))):
                raise ProtocolError("bad_result", "interaction effect")
            if result.get("status") == "browser_local_effect_observed" and (
                    result.get("after") is None or result.get("before") is None):
                raise ProtocolError("bad_result", "interaction observation missing")
        _require_exact_keys(result, required)
        if not _bounded_str(result["operation_id"], 65) \
                or not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{32}", result["operation_id"]):
            raise ProtocolError("bad_result", "operation_id")
        if result["status"] not in {"dispatched", "browser_local_effect_observed",
                                    "ambiguous_after_dispatch"}:
            raise ProtocolError("bad_result", "action status")
        if result["tab_id"] is not None and not (_is_int(result["tab_id"]) and result["tab_id"] >= 0):
            raise ProtocolError("bad_result", "tab_id")
        if result["window_id"] is not None and not _is_int(result["window_id"]):
            raise ProtocolError("bad_result", "window_id")
        if not _bounded_str(result["observed_url"], MAX_TAB_URL_CHARS, allow_none=True):
            raise ProtocolError("bad_result", "observed_url")
        if not _is_bool(result["load_confirmed"]):
            raise ProtocolError("bad_result", "load_confirmed")
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
