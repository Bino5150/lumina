"""AGENT-NO-PROGRESS-BREAKER-01 -- progress-aware no-progress circuit breaker.

Slice B of the runaway-containment plan. GATLING-TRIGGER-GUARD-01's budgets
bound HOW MANY calls a turn may run (256 per response, 512 per turn, 60 min);
this module decides whether the calls are still getting anywhere. Two
production runaways (2026-10-16) ran thousands of calls that produced 3 and
26 distinct (call, result) pairs; legitimate retained turns never had more
than 3 stale calls in a row.

The ledger is per turn (one per LuminaAgent._chat_impl() call, so a subagent
turn has its own). Each executed call gets a fingerprint

    (tool name, args digest, normalized-result digest)

and is STALE when that fingerprint was already seen this turn, or when the
call FAILED (incident amendment 4.2: a malformed call is novel, and without
this rule one interleaved every ~15 calls reset the streak forever).
Anything else is PROGRESS and resets the streak. Over the current streak:

    every stale call             warn at 10, trip at 20
    stale write-tier calls       warn at 3,  trip at 5

Results are normalized before hashing (UUIDs and hex runs -> <hex>, digit
runs -> #): chrome_status embeds a live connected_for_s, get_time is a clock
and Chrome action receipts carry a fresh operation_id, so an exact hash
would read every one of those calls as new and never trip on the morning
runaway. The cost, documented in docs/troubleshooting.md: a result whose
ONLY change is a number reads as no new information -- bounded (20
consecutive read-only, 5 write-tier), never silently lost.

Fingerprint digests are HMAC-SHA256 under a random per-turn key that is
never stored, so telemetry can compare results within a turn but cannot be
used to confirm guessed page or file content across turns.

Pure module: no agent, Flight Recorder or UI imports. core/agent.py owns the
wiring at its single registry.call() site.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from core.tool_profiles import TOOL_TIERS

# Breaker-only tier overrides. The permission tier (TOOL_TIERS) is unchanged;
# these tools only observe, so repeated polls of them are judged on the
# read-only thresholds instead of the write-tier ones.
BREAKER_READ_ONLY_OVERRIDES = frozenset({"check_background_task"})

# (default, floor, ceiling). Owner-configurable through prefs only, always
# clamped into these code-enforced bounds: no value can disable the breaker.
_READ_ONLY_TRIP = (20, 5, 64)
_WRITE_TRIP = (5, 2, 16)
_READ_ONLY_WARN_DEFAULT = 10
_WRITE_WARN_DEFAULT = 3

_REPEATED_NAMES_LIMIT = 8
_HASH_HEX_CHARS = 16

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX_RUN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8,}(?![0-9A-Za-z])")
_DIGITS_RE = re.compile(r"\d+")


def result_text(result) -> str:
    """The text a result is fingerprinted from: strings as-is, anything else
    as sorted-key JSON (str() if that fails), so dict results are stable."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, sort_keys=True, default=str)
    except Exception:
        return str(result)


def normalize_result(text: str) -> str:
    """Strip volatile metadata: UUIDs and hex runs of 8+ chars become <hex>,
    then every digit run becomes #."""
    text = _UUID_RE.sub("<hex>", text)
    text = _HEX_RUN_RE.sub("<hex>", text)
    return _DIGITS_RE.sub("#", text)


def breaker_tier(name: str) -> str:
    """"read_only" or "write". Fail-closed: an unknown, MCP or malformed
    tool name (no TOOL_TIERS entry) is write tier."""
    if name in BREAKER_READ_ONLY_OVERRIDES:
        return "read_only"
    return "read_only" if TOOL_TIERS.get(name, "execute") == "read_only" else "write"


def safe_tool_label(name: str) -> str:
    """A tool name fit for a trusted, model-facing notice: only names Lumina's
    own code defines (TOOL_TIERS keys). Anything else -- a custom or MCP tool,
    or a model-authored malformed name -- never crosses into it verbatim."""
    return name if isinstance(name, str) and name in TOOL_TIERS else "an unlisted tool"


@dataclass(frozen=True)
class Thresholds:
    read_only_warn: int
    read_only_trip: int
    write_warn: int
    write_trip: int


def _bounded_int(config, name: str, default: int, floor: int, ceiling: int) -> int:
    value = getattr(config, name, None)
    if isinstance(value, bool):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return min(ceiling, max(floor, value))


def thresholds_from_config(config) -> Thresholds:
    """Read the four prefs-backed thresholds, clamped to the hard bounds.
    A junk value falls back to the default; a warning always precedes its
    trip (warn is clamped to [1, trip - 1])."""
    ro_trip = _bounded_int(config, "NO_PROGRESS_READ_ONLY_TRIP", *_READ_ONLY_TRIP)
    w_trip = _bounded_int(config, "NO_PROGRESS_WRITE_TRIP", *_WRITE_TRIP)
    ro_warn = _bounded_int(config, "NO_PROGRESS_READ_ONLY_WARN", _READ_ONLY_WARN_DEFAULT, 1, ro_trip - 1)
    w_warn = _bounded_int(config, "NO_PROGRESS_WRITE_WARN", _WRITE_WARN_DEFAULT, 1, w_trip - 1)
    return Thresholds(ro_warn, ro_trip, w_warn, w_trip)


@dataclass(frozen=True)
class Observation:
    progress: bool
    stale_streak: int
    write_stale_streak: int
    warn: bool
    warn_kind: Optional[str]
    trip: bool
    trip_kind: Optional[str]
    result_hash: str
    result_norm_hash: str


class ProgressLedger:
    """One turn's progress ledger. observe() once per executed call, in order."""

    def __init__(self, thresholds: Thresholds, *, key: Optional[bytes] = None):
        self.thresholds = thresholds
        self._key = key if key is not None else secrets.token_bytes(32)
        self._seen: set = set()
        self._streak_names: Counter = Counter()
        self.stale_streak = 0
        self.write_stale_streak = 0
        self.observed_calls = 0
        self._warned_this_streak = False

    def _digest(self, text: str) -> str:
        return hmac.new(self._key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()

    @property
    def distinct_fingerprints(self) -> int:
        return len(self._seen)

    def repeated_names(self, limit: int = _REPEATED_NAMES_LIMIT) -> list:
        """(raw tool name, count) over the current stale streak, most common
        first. Raw names are telemetry-only; notices use safe_tool_label()."""
        return self._streak_names.most_common(limit)

    def observe(self, name: str, args, result, *, success: bool) -> Observation:
        self.observed_calls += 1
        text = result_text(result)
        result_hash = self._digest(text)
        result_norm_hash = self._digest(normalize_result(text))
        try:
            args_blob = json.dumps(args, sort_keys=True, default=str)
        except Exception:
            args_blob = str(args)
        fingerprint = (name, self._digest(args_blob), result_norm_hash)
        stale = fingerprint in self._seen or not success
        self._seen.add(fingerprint)

        if stale:
            self.stale_streak += 1
            if breaker_tier(name) == "write":
                self.write_stale_streak += 1
            self._streak_names[name] += 1
        else:
            self.stale_streak = 0
            self.write_stale_streak = 0
            self._streak_names.clear()
            self._warned_this_streak = False

        t = self.thresholds
        trip_kind = ("write" if self.write_stale_streak >= t.write_trip
                     else "read_only" if self.stale_streak >= t.read_only_trip else None)
        warn_kind = None
        if trip_kind is None and not self._warned_this_streak:
            warn_kind = ("write" if self.write_stale_streak >= t.write_warn
                         else "read_only" if self.stale_streak >= t.read_only_warn else None)
            if warn_kind is not None:
                self._warned_this_streak = True
        return Observation(
            progress=not stale,
            stale_streak=self.stale_streak,
            write_stale_streak=self.write_stale_streak,
            warn=warn_kind is not None,
            warn_kind=warn_kind,
            trip=trip_kind is not None,
            trip_kind=trip_kind,
            result_hash=result_hash[:_HASH_HEX_CHARS],
            result_norm_hash=result_norm_hash[:_HASH_HEX_CHARS],
        )

    def warning_notice(self) -> str:
        """The trusted model-facing warning. Carries only Lumina-defined tool
        names and counts -- never arguments, results or model-authored names."""
        labels: Counter = Counter()
        for name, count in self._streak_names.items():
            labels[safe_tool_label(name)] += count
        repeated = ", ".join(f"{label} x{count}" for label, count in labels.most_common(_REPEATED_NAMES_LIMIT))
        return (
            "## No-progress warning\n"
            f"The last {self.stale_streak} tool calls this turn produced nothing new: they repeated "
            f"calls whose results you already have, or they failed ({repeated}). Stop repeating them. "
            "Use the results you already have, try a different approach, or give your answer. "
            "If this continues, Lumina will stop the turn."
        )
