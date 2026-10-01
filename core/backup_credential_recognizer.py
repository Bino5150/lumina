"""Recognize copied ChatGPT custody documents without inspecting credential values."""
import json
import re

MAX_DOCUMENT_BYTES = 1024 * 1024
_SCHEMA_PREFIXES = ("lumina.chatgpt.sessions/", "lumina.chatgpt.host/")
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_SCHEMA_FIELD = re.compile(r'"schema"\s*:\s*"(lumina\.chatgpt\.(?:sessions|host)/)')


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
            if _has_schema_field(data[max(0, start - overlap):start + window]):
                return True
        return False
    if _has_schema_field(data):
        return True
    try:
        decoded = json.loads(data)
    except RecursionError:
        return True
    except (UnicodeError, ValueError):
        return False
    schema = decoded.get("schema") if isinstance(decoded, dict) else None
    return isinstance(schema, str) and schema.startswith(_SCHEMA_PREFIXES)
