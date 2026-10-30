"""F0 fixture interaction contract. No external-write authority is minted here.

An inert fixture is an owner-prepared test surface, explicitly attested in
the extension popup. Loopback is an additional restriction, not proof of
inertness. Ordinary sites and unknown controls are deliberately unsupported.
"""
import re
from urllib.parse import unquote, urlsplit

KINDS = frozenset({"focus", "set_checked", "set_expanded"})
MISSION_CLASS = "fixture_interact"
MAX_INTERACTIONS = 30


def is_fixture_url(url) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)", url)
        if not match or int(match[1]) > 65535 or int(match[1]) == 80:
            return False
        parsed = urlsplit(url)
        return not any(unquote(segment).lower() in {".", ".."} for segment in parsed.path.split("/"))
    except ValueError:
        return False


def owner_fixture_url(text) -> str | None:
    # Anchored, exact owner command; page/tool/peer strings cannot mint a mission.
    if not isinstance(text, str):
        return None
    prefix = "test browser fixture "
    if not text.lower().startswith(prefix):
        return None
    words = text[len(prefix):].split()
    candidate = words[0] if words else None
    return candidate if is_fixture_url(candidate) else None
