"""Recognize copied ChatGPT custody documents without inspecting credential values."""
import json
import re

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_EMBEDDED_DEPTH = 1024
MAX_RESERIALIZATION_LAYERS = 4
MAX_EMBEDDED_STRING_CHARS = MAX_DOCUMENT_BYTES
MAX_EMBEDDED_WORK = 8 * MAX_DOCUMENT_BYTES
_SCHEMA_PREFIXES = ("lumina.chatgpt.sessions/", "lumina.chatgpt.host/")
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_SCHEMA_FIELD = re.compile(r'"schema"\s*:\s*"(lumina\.chatgpt\.(?:sessions|host)/)')
_SERIALIZED_SCHEMA_FIELD = re.compile(
    r'\\+"schema\\+"\s*:\s*\\+"(lumina\.chatgpt\.(?:sessions|host)/)'
)


def _decoded_views(data: bytes):
    # Later chunks have no BOM. Try UTF-16/32 when NULs suggest wide text.
    yield data.decode("utf-8-sig", errors="ignore")
    if b"\x00" in data:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be",
                         "utf-32", "utf-32-le", "utf-32-be"):
            try:
                yield data.decode(encoding, errors="ignore")
            except UnicodeError:
                continue


def _has_schema_field(data: bytes) -> bool:
    for view in _decoded_views(data):
        # JSON permits Unicode escapes in either the key or schema value.
        normalized = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), view)
        if _SCHEMA_FIELD.search(normalized):
            return True
    return False


def _has_serialized_schema_field(data: bytes) -> bool:
    # Large files are scanned in fixed windows; their JSON cannot be parsed
    # as one bounded document. Match only the exact escaped schema field in
    # a text view, not an incidental string within binary database pages.
    for view in _decoded_views(data):
        if "\x00" in view:
            continue
        normalized = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), view)
        if _SERIALIZED_SCHEMA_FIELD.search(normalized):
            return True
    return False


def _has_embedded_document(decoded) -> bool:
    """Inspect JSON string values without unbounded recursive reparsing.

    Ambiguous candidates that exceed a bound fail closed. Non-JSON prose and
    ordinary stringified JSON stay archivable.
    """
    pending = [(decoded, 0, 0)]
    work = 0
    while pending:
        value, depth, layers = pending.pop()
        work += 1
        if depth > MAX_EMBEDDED_DEPTH or work > MAX_EMBEDDED_WORK:
            return True
        if isinstance(value, dict):
            schema = value.get("schema")
            if isinstance(schema, str) and schema.startswith(_SCHEMA_PREFIXES):
                return True
            pending.extend((item, depth + 1, layers) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1, layers) for item in value)
        elif isinstance(value, str):
            candidate = value.lstrip()
            if not candidate.startswith(("{", "[", '"')):
                continue
            work += len(candidate)
            if (len(candidate) > MAX_EMBEDDED_STRING_CHARS
                    or work > MAX_EMBEDDED_WORK
                    or layers >= MAX_RESERIALIZATION_LAYERS):
                return True
            try:
                nested = json.loads(candidate)
            except RecursionError:
                return True
            except (UnicodeError, ValueError):
                continue
            # Reuse the precise schema-field scan for a parsed JSON value.
            # Escaped quotes in ordinary prose do not become a field here.
            if isinstance(nested, (dict, list)) and _has_schema_field(
                candidate.encode("utf-8", errors="surrogatepass")
            ):
                return True
            pending.append((nested, depth + 1, layers + 1))
    return False


def is_session_document(data: bytes) -> bool:
    """Classify captured bytes; deeply nested ambiguous JSON fails closed.

    The any-position field scan catches credential copies in fences, arrays,
    nested objects and leading text. Generic prose/JSONL words such as
    ``schema`` or ``ChatGPT`` do not identify a credential document.
    Callers with large sources scan every chunk of their captured snapshot.
    """
    if len(data) > MAX_DOCUMENT_BYTES:
        # Fixed-size windows bound decoding and normalization for Agent
        # Backup members, which are already captured as immutable bytes.
        window = 64 * 1024
        overlap = 8192
        for start in range(0, len(data), window):
            chunk = data[max(0, start - overlap):start + window]
            if _has_schema_field(chunk) or _has_serialized_schema_field(chunk):
                return True
        return False
    if _has_schema_field(data):
        return True
    try:
        decoded = json.loads(data)
    except RecursionError:
        return True
    except (UnicodeError, ValueError):
        # Memory Backup supplies overlapping slices after the first MiB.
        # Such slices are not standalone JSON, but may contain a late
        # serialized copy's exact escaped schema field.
        return _has_serialized_schema_field(data)
    return _has_embedded_document(decoded)
