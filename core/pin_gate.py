"""
PIN / Codeword Gate — code-level dispatch check, not a prompt-level instruction.
The model can roleplay asking for a codeword for UX — whether a sensitive-tier
tool actually fires never depends on what the model decided. PIN verification
is in-memory per process; it never persists across restarts by design, so a
fresh app launch or a fresh headless invocation always starts unverified.
"""
import hashlib, hmac, os, time
from core.persistence import load as load_prefs, save as save_prefs

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 min lockout after MAX_ATTEMPTS failures

_session_state = {
    "verified_channels": set(),   # channel_ids that are PIN-verified this session
    "attempts": {},               # channel_id -> [timestamps of failed attempts]
}


def _hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 200_000).hex()


def set_pin(pin: str):
    """Owner-only action — set/replace the PIN. Stored hashed+salted in
    prefs.json, never plaintext, never in any persona JSON."""
    salt = os.urandom(16)
    prefs = load_prefs()
    prefs["pin_hash"] = _hash_pin(pin, salt)
    prefs["pin_salt"] = salt.hex()
    save_prefs(prefs)


def pin_is_configured() -> bool:
    return bool(load_prefs().get("pin_hash"))


def is_locked_out(channel_id: str = "default"):
    """Returns (locked_out: bool, seconds_remaining: float)."""
    attempts = _session_state["attempts"].get(channel_id, [])
    now = time.time()
    recent = [t for t in attempts if now - t < LOCKOUT_SECONDS]
    _session_state["attempts"][channel_id] = recent
    if len(recent) >= MAX_ATTEMPTS:
        remaining = LOCKOUT_SECONDS - (now - recent[0])
        return remaining > 0, max(0, remaining)
    return False, 0.0


def verify_pin(pin: str, channel_id: str = "default"):
    """Returns (success: bool, message: str)."""
    locked, remaining = is_locked_out(channel_id)
    if locked:
        mins = int(remaining // 60) + 1
        return False, f"Locked out. Try again in ~{mins} min."

    prefs = load_prefs()
    pin_hash = prefs.get("pin_hash")
    pin_salt = prefs.get("pin_salt")
    if not pin_hash or not pin_salt:
        return False, "No PIN configured."

    candidate = _hash_pin(pin, bytes.fromhex(pin_salt))
    if hmac.compare_digest(candidate, pin_hash):
        _session_state["verified_channels"].add(channel_id)
        _session_state["attempts"][channel_id] = []
        return True, "Verified."

    _session_state["attempts"].setdefault(channel_id, []).append(time.time())
    return False, "Incorrect PIN."


def is_verified(channel_id: str = "default") -> bool:
    return channel_id in _session_state["verified_channels"]


def reset_verification(channel_id: str = "default"):
    """Force re-verification — e.g. a new Discord day, or owner request."""
    _session_state["verified_channels"].discard(channel_id)