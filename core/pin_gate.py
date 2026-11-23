"""
PIN / Codeword Gate — code-level dispatch check, not a prompt-level instruction.
The model can roleplay asking for a codeword for UX — whether a sensitive-tier
tool actually fires never depends on what the model decided. PIN verification
is in-memory per process; it never persists across restarts by design, so a
fresh app launch or a fresh headless invocation always starts unverified.
"""
import hashlib, hmac, json, os, re, time
from core.persistence import load as load_prefs, save as save_prefs

_HEX64 = re.compile(r"[0-9a-f]{64}")
_SALT_HEX = re.compile(r"(?:[0-9a-f]{2})+")  # even length: what bytes.fromhex reads

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
    if not _stored_pair_ok(pin_hash, pin_salt):   # TR1-01: fail closed, never a raw error
        return False, "The stored PIN can't be read, so it can't be checked."

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


# ---------------------------------------------------------------------------
# C1-S2: owner PIN enrollment on a restored copy (A2-1, A2-R1, PS-1..10)
# ---------------------------------------------------------------------------
# The owner PIN is never archived, so a restored copy starts with none and
# never needs the original installation's PIN (DR-1). Enrollment is set-once
# (no replace or reset in this version, DR-4), reached only through
# core.suspension_runtime.set_owner_pin (GUI envelope + restored-copy
# predicate), and never marks any channel verified (PS-6).

PIN_MIN_LENGTH = 6          # a 6-digit numeric PIN is valid (owner decision)
PIN_MAX_LENGTH = 128        # bounds the PBKDF2 input; not a complexity rule
_PIN_KEYS = ("pin_hash", "pin_salt")


def _stored_pair_ok(pin_hash, pin_salt) -> bool:
    """True only for the shape set_pin and set_initial_owner_pin write: a
    64-hex hash and a non-empty, even-length lowercase hex salt. Anything
    else can't be checked, so it never counts as a PIN (TR1-01)."""
    return (type(pin_hash) is str and _HEX64.fullmatch(pin_hash) is not None
            and type(pin_salt) is str and _SALT_HEX.fullmatch(pin_salt) is not None)


def owner_pin_state() -> str:
    """Strict read: "set" only for a pair verify_pin can check (_stored_pair_ok);
    "unset" when neither key is present (or the file is absent); anything
    else, including a half-present pair or an unreadable prefs.json, is
    "unreadable" (TA2-06). Never the lenient load()."""
    from core import persistence
    try:
        with open(persistence.PREFS_PATH, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "unset"
    except OSError:
        return "unreadable"
    try:
        prefs = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "unreadable"
    if type(prefs) is not dict:
        return "unreadable"
    present = [k for k in _PIN_KEYS if k in prefs]
    if not present:
        return "unset"
    if len(present) == 2 and _stored_pair_ok(prefs.get("pin_hash"), prefs.get("pin_salt")):
        return "set"
    return "unreadable"


def pin_input_problem(pin, confirm) -> str:
    """"" when acceptable, else a short reason code. No character class is
    required: digits only, letters only or mixed are all fine (A2-R1)."""
    if type(pin) is not str or type(confirm) is not str:
        return "invalid"
    if pin != confirm:
        return "mismatch"
    if len(pin) < PIN_MIN_LENGTH:
        return "too_short"
    if len(pin) > PIN_MAX_LENGTH:
        return "too_long"
    if pin != pin.strip():
        return "whitespace"
    try:
        pin.encode("utf-8")       # SC2-03: the hash needs UTF-8 bytes
    except UnicodeEncodeError:
        return "invalid"
    return ""


def set_initial_owner_pin(pin: str) -> str:
    """Set the owner PIN once. Returns "set", "already_set", "unreadable" or
    "write_failed". The caller checks the input first (pin_input_problem)
    and the authority (the runtime wrapper). Never marks a channel verified."""
    if pin_input_problem(pin, pin):
        return "invalid"
    from core import persistence
    salt = os.urandom(16)
    return persistence.set_if_unset(_PIN_KEYS, {"pin_hash": _hash_pin(pin, salt),
                                                "pin_salt": salt.hex()})
