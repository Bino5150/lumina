"""Bounded, semantic recognition of OS-local ChatGPT custody documents.

This module does not import the credential store or instantiate its records.
"""
import json

MAX_DOCUMENT_BYTES = 1024 * 1024
_SCHEMA_PREFIXES = ("lumina.chatgpt.sessions/", "lumina.chatgpt.host/")


class _AmbiguousSchema(ValueError):
    pass


def _object(pairs):
    seen_schema = False
    result = {}
    for key, value in pairs:
        if key == "schema":
            if seen_schema:
                raise _AmbiguousSchema()
            seen_schema = True
        result[key] = value
    return result


def _candidate_object_text(sample: bytes):
    """Decode only bounded object-shaped input for malformed-JSON decisions."""
    if not (sample.lstrip().startswith((b"{", b"\xef\xbb\xbf"))
            or b"\x00" in sample[:32] or sample.startswith((b"\xff\xfe", b"\xfe\xff"))):
        return None
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                     "utf-32", "utf-32-le", "utf-32-be"):
        try:
            candidate = sample.decode(encoding, errors="ignore").lstrip("\ufeff \t\r\n")
        except UnicodeError:
            continue
        if candidate.startswith("{") and "\x00" not in candidate[:64]:
            return candidate
    return None


def is_session_document(data: bytes) -> bool:
    """Exclude recognized documents and ambiguous candidates, including
    differently escaped JSON. Oversized JSON objects are conservatively
    excluded because their schema cannot be checked within the size cap."""
    sample = data[:MAX_DOCUMENT_BYTES + 1]
    candidate = _candidate_object_text(sample)
    if len(data) > MAX_DOCUMENT_BYTES:
        return candidate is not None
    try:
        decoded = json.loads(data, object_pairs_hook=_object)
    except _AmbiguousSchema:
        return True
    except (UnicodeError, ValueError):
        if candidate is None:
            return False
        lowered = candidate.casefold()
        return "chatgpt" in lowered or "schema" in lowered or "\\u" in lowered
    schema = decoded.get("schema") if isinstance(decoded, dict) else None
    return isinstance(schema, str) and schema.startswith(_SCHEMA_PREFIXES)
