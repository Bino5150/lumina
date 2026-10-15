"""Owner browsing missions (BROWSER-COMPANION-COMPLETION-01 Slice 1).

A mission is the authority for routine, generic browsing during ONE
authenticated owner turn: Lumina may observe pages and navigate among the
owner's sites without asking again at every step. It is minted only from a
fresh, authenticated OWNER_DIRECT ingress event -- the same trusted point
chrome_companion/navigation.py uses -- and lives in this turn's execution
context. It is never persisted, never written to conversation history, and
never readable or writable through any model, tool or page text.

What a mission covers (Slice 1):

  * classes: observe and navigate ONLY. Typing, clicking controls, posting,
    sending, purchases, account or security changes are not in it, and no
    page, tool result or model output can add them.
  * sites: the sites the owner has allowed in the Chrome Companion popup
    (checked live by the extension, revocable at any instant) PLUS sites the
    owner explicitly names in this turn's own text by address
    (``https://example.org/...``, ``www.example.org`` or ``example.org``).
    A page cannot widen either set.
  * session: the Chrome Companion connection it is first used on. A reconnect
    (extension reload, Chrome restart, PAUSE/RESUME) ends it, and the
    extension's own per-connection Navigation Allow is still required.
  * lifetime and budget: this owner turn, at most ``LIFETIME_S`` seconds, at
    most ``MAX_NAVIGATIONS`` navigations. Replaying the owner event cannot
    renew a spent budget within the process; across a restart a replayed
    event could mint a fresh observe/navigate mission -- acceptable ONLY
    because a mission carries no external-commit class (see F1 in the
    campaign packet). No commit authority may ever be added here.

PAUSE, a popup revoke, the Navigation Allow switch and the restricted-surface
policy are enforced by the extension independently of this module.
"""
from __future__ import annotations

import contextvars
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from chrome_companion import policy, protocol

LIFETIME_S = 30 * 60
MAX_NAVIGATIONS = 60
CLASSES = frozenset({"observe", "navigate"})

# Addresses written by the owner. Scheme URLs anywhere in the text; bare hosts
# only when they end in a common public suffix, so "notes.txt" or "main.py"
# never becomes a site. (A wrong extra site would still need the extension's
# Navigation Allow, and could never be a restricted surface.)
_SCHEME_URL = re.compile(r"\bhttps?://[^\s<>\"'`]+", re.IGNORECASE)
_BARE_HOST = re.compile(
    r"(?<![\w@./-])((?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|org|net|io|dev|ai|edu|gov|app|co|uk|de|fr|ca|au|us|info|me|wiki|news|blog|tech|gg|tv|so|xyz))"
    r"(?![\w-])(?::\d{1,5})?(?:/[^\s<>\"'`]*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ".,;:!?)]}'\""

_current: contextvars.ContextVar["Mission | None"] = contextvars.ContextVar(
    "chrome_companion_mission", default=None
)


class MissionRefused(Exception):
    """A mission does not cover this request. ``code`` is stable and content-free."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def owner_named_sites(text) -> frozenset[str]:
    """Site patterns for every address written in the owner's own text."""
    if not isinstance(text, str):
        return frozenset()
    found = []
    for match in _SCHEME_URL.finditer(text):
        found.append(match.group(0))
    for match in _BARE_HOST.finditer(text):
        found.append("https://" + match.group(0))
    patterns = []
    for raw in found:
        candidate = raw.rstrip(_TRAILING_PUNCT)
        verdict = policy.classify_url(candidate)
        if verdict.readable and verdict.pattern not in patterns:
            patterns.append(verdict.pattern)
        if len(patterns) >= protocol.MAX_NAVIGATE_SCOPE:
            break
    return frozenset(patterns)


@dataclass
class Mission:
    mission_id: str
    event_id: str
    owner_sites: frozenset[str]
    created: float = field(default_factory=time.monotonic)
    classes: frozenset[str] = CLASSES
    navigations: int = 0
    connection_id: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(self, action_class: str, connection_id: str | None) -> None:
        """Raise MissionRefused unless this mission covers ``action_class`` on
        this connection now. A navigate check also spends one unit of budget;
        it is spent BEFORE dispatch, so an ambiguous outcome is never re-tried
        for free."""
        with self._lock:
            if action_class not in self.classes:
                raise MissionRefused("outside_mission_class",
                                     f"this browsing mission does not include {action_class}")
            if time.monotonic() - self.created > LIFETIME_S:
                raise MissionRefused("mission_expired", "this browsing mission has expired; the owner can ask again")
            if connection_id is None:
                raise MissionRefused("not_connected", "Lumina's Chrome is not connected")
            if self.connection_id is None:
                self.connection_id = connection_id
            elif self.connection_id != connection_id:
                raise MissionRefused("mission_session_changed",
                                     "the Chrome connection changed during this mission; the owner can ask again")
            if action_class == "navigate":
                if self.navigations >= MAX_NAVIGATIONS:
                    raise MissionRefused("mission_budget_exhausted",
                                         f"this mission's {MAX_NAVIGATIONS} navigations are used up")
                self.navigations += 1

    def scope(self) -> list[str]:
        return sorted(self.owner_sites)


def begin_turn(user_input, *, source: str, owner: bool, event_id: str):
    """Mint a mission only for an authenticated owner's direct turn."""
    mission = None
    if owner and source == "OWNER_DIRECT" and isinstance(event_id, str) and event_id:
        if isinstance(user_input, str):
            text = user_input
        elif isinstance(user_input, list):  # multipart turn: the owner's own text blocks only
            text = " ".join(block["text"] for block in user_input
                            if isinstance(block, dict) and block.get("type") == "text"
                            and isinstance(block.get("text"), str))
        else:
            text = ""
        mission = Mission(mission_id=secrets.token_hex(8), event_id=event_id,
                          owner_sites=owner_named_sites(text))
    return _current.set(mission)


def end_turn(token) -> None:
    _current.reset(token)


def current() -> Mission | None:
    return _current.get()
