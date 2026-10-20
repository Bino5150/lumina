"""TELEGRAM-MASTER-SWITCH-01 -- the persistent Telegram remote-access switch.

One owner-controlled master switch decides whether Telegram may be used at
all, in either direction:

  ON   the bridge may listen (once something starts it) and the outbound
       tools may send to the owner's Telegram.
  OFF  nothing polls, nothing is received, nothing is sent -- regardless of
       startup, outbound sends, reconnection, a standalone bridge process
       or another runtime on the shared data dir (Mint/Kali). OFF wins.

Who can change it:
  * ON only by the owner's explicit local action: the desktop Settings
    switch (ui/settings/communications_tab.py) is the only caller of
    turn_on_by_owner(). No tool, model output, Telegram message or command
    can reach it.
  * OFF by that same switch, or by the emergency stop
    (ui/main_window.py's _trigger_emergency_stop). Re-arming the emergency
    stop never turns it back on.

State lives in prefs (the shared data dir), written with
persistence.update() so only these keys change. A failed OFF write still
holds OFF for this process (_forced_off), so a disk error can never leave
Telegram reachable. Every gate reads remote_access_enabled() fresh, so a
change made by another process is seen at the next check.

The switch is transport policy only: it is not the emergency stop's
execution authority and not the (future) Gatling suspension latch.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from core import persistence

PREF_KEY = "telegram_remote_access"
REASON_KEY = "telegram_remote_access_off_reason"
ON_SINCE_KEY = "telegram_remote_access_on_since"

ACCESS_OFF_MESSAGE = (
    "Telegram remote access is off — turn it on in Settings → Communications."
)
STALE_BACKLOG_REPLY = (
    "[Lumina: this message arrived while Telegram remote access was off, so it was not run. "
    "Send it again if you still want it.]"
)

_lock = threading.Lock()
_forced_off = False


def remote_access_enabled() -> bool:
    """True only when the switch is ON on disk and this process has not been
    forced OFF. Only a real boolean True counts as ON."""
    with _lock:
        if _forced_off:
            return False
    return persistence.load().get(PREF_KEY) is True


def off_reason():
    return persistence.load().get(REASON_KEY)


def on_since():
    """Unix time of the last owner OFF→ON flip, or None."""
    value = persistence.load().get(ON_SINCE_KEY)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def turn_on_by_owner() -> bool:
    """The owner flipped the desktop Settings switch ON. Returns True when
    persisted; a failed write leaves remote access off."""
    global _forced_off
    if not persistence.update({PREF_KEY: True, REASON_KEY: None, ON_SINCE_KEY: time.time()}):
        return False
    with _lock:
        _forced_off = False
    return True


def turn_off(reason: str) -> bool:
    """Turn remote access OFF ("owner" or "emergency_stop"). OFF holds in
    this process even if the write fails; returns whether it persisted.
    Clears the return-route ledger so no old route can capture a later
    reply."""
    global _forced_off
    with _lock:
        _forced_off = True
    _clear_routes()
    return persistence.update({PREF_KEY: False, REASON_KEY: reason})


@dataclass(frozen=True)
class IngressAuthority:
    """TELEGRAM-OWNER-INBOUND-WAKE-01 (R3 C-1) -- the remote-access
    authority an inbound update was admitted under: the switch's last
    OFF->ON time and the emergency-stop epoch (latch() advances it, so even
    a latch that was already re-armed changes it)."""
    on_since: float | None
    epoch: int


def capture_ingress_authority() -> IngressAuthority | None:
    """Snapshot taken when an inbound update passes the gate. None while
    remote access is OFF or the emergency stop is latched."""
    from core import emergency_stop
    if emergency_stop.is_latched() or not remote_access_enabled():
        return None
    return IngressAuthority(on_since=on_since(), epoch=emergency_stop.current_epoch())


def ingress_authority_valid(authority: IngressAuthority | None) -> bool:
    """Re-checked at final turn admission and before any reply: the switch
    is still ON (fresh read, so OFF from another process counts), no E-stop
    is latched, and neither an OFF->ON flip nor an E-stop has happened since
    the snapshot."""
    if authority is None:
        return False
    from core import emergency_stop
    if emergency_stop.is_latched() or not remote_access_enabled():
        return False
    return on_since() == authority.on_since and emergency_stop.current_epoch() == authority.epoch


def _clear_routes() -> None:
    try:
        from comms import telegram_origin_routing
        telegram_origin_routing.clear_routes()
    except Exception as exc:
        print(f"[TELEGRAM] could not clear return routes: {type(exc).__name__}", flush=True)


def _reset_for_tests() -> None:
    global _forced_off
    with _lock:
        _forced_off = False
