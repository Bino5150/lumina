"""
Dreaming — idle-triggered memory sweep for the main build.
Fires on session idle, summarizes what's happened since the last sweep,
writes to the nightstand wing (isolated from curated Palace content).
"""
from tools.memory import load_chat_messages
from tools.palace import palace_store
from core.context import estimate_tokens
from core.backends.loader import get_llm_backend
from datetime import datetime
import config

# DREAM-LIFECYCLE-01: structured sweep outcomes. The UI layer maps only
# DREAM_COMPLETED to "idle window consumed"; ineligible/failed/cancelled
# probes must remain admit-able on a later timer tick in the same window.
DREAM_COMPLETED = "completed"
DREAM_INELIGIBLE = "ineligible"
DREAM_FAILED = "failed"
DREAM_CANCELLED = "cancelled"

DREAM_PROMPT = (
    "Summarize this conversation from the USER's perspective — what they said, "
    "asked for, decided, or worked on. Concrete facts, decisions, discoveries, "
    "outcomes only.\n\n"
    "The assistant's replies may contain personality, banter, and scene-setting "
    "flourishes — ignore those. Only include something from the assistant's side "
    "if it states a concrete fact, decision, or outcome that isn't restated "
    "anywhere in the user's own messages.\n\n"
    "No commentary, no restating questions, no narrative framing. Bullet form. "
    "This is memory synthesis, not a transcript."
)

COMPACTION_PROMPT = (
    "Extract only concrete facts, decisions, and outcomes from this exchange. "
    "No commentary, no restating questions, no narrative framing. Bullet form. "
    "This is memory compression, not a transcript."
)

PROFILE_CURATION_PROMPT = (
    "You maintain a running profile of {user} based on conversation, separate "
    "from their own self-written bio.\n\n"
    "AUTHORITATIVE BIO (never contradict, do not repeat verbatim):\n{bio}\n\n"
    "YOUR EXISTING NOTES (your own prior observations -- refine and update "
    "these, don't just append; preserve anything that looks like a deliberate "
    "correction):\n{existing}\n\n"
    "RECENT CONVERSATION:\n{raw_text}\n\n"
    "Write updated notes: concrete facts, ongoing projects, preferences, or "
    "situational details not already covered by the bio. Bullet form. No "
    "commentary, no restating the bio."
)

def curate_human_profile(raw_text: str, bio: str, existing_profile: str) -> str | None:
    """Resynthesizes the Lumina-authored 'curated profile' layer of My
    Human, from real conversation. Bio is passed in as authoritative
    context, never overwritten by this."""
    try:
        backend = get_llm_backend()
        prompt = PROFILE_CURATION_PROMPT.format(
            user=config.USER_NAME, bio=bio or "(none written yet)",
            existing=existing_profile or "(none yet)", raw_text=raw_text[:6000]
        )
        return backend.complete_utility(
            prompt=prompt, prefill="NOTES:", max_tokens=400, temperature=0.3,
        )
    except Exception as e:
        print(f"[DREAMING] profile curation call failed: {e}", flush=True)
        return None

_last_dream_sweep: dict[int, str] = {}  # chat_id -> ISO timestamp of last sweep


def run_summarization_call(raw_text: str, prompt: str = DREAM_PROMPT, max_tokens: int = 500) -> str | None:
    """
    S41 / F-62 real fix: this used to be a bespoke requests.post() straight
    to config.LLM_BACKEND_URL with its own hand-rolled model resolution and
    a hardcoded timeout=30 — completely bypassing the backend abstraction
    every real chat turn goes through. That's what caused the dream-sweep
    timeout bug (30s tied to nothing, vs config.TOOL_CALL_TIMEOUT everywhere
    else) and meant this only ever worked against a local OpenAI-compatible
    server — pointing LLM_BACKEND at a cloud provider would have silently
    broken dreaming entirely. get_llm_backend() returns whichever backend is
    actually active right now, so this correctly follows backend switches,
    real auth headers, and the real timeout config, same as every other
    call in the app.

    MB-11 (S57 correction 2): prompt/max_tokens are now keyword-parameterized
    rather than duplicating this whole function for compaction's own prompt —
    defaults match the original dream-sweep behavior exactly, so existing
    callers (on_session_idle) are unaffected. Compaction calls this with
    prompt=COMPACTION_PROMPT, max_tokens=300.
    """
    try:
        backend = get_llm_backend()
        return backend.complete_utility(
            prompt=f"{prompt}\n\n{raw_text[:6000]}",
            prefill="SUMMARY:",
            max_tokens=max_tokens,
            temperature=0.3,
        )
    except Exception as e:
        print(f"[DREAMING] summarization call failed: {e}", flush=True)
        return None

def on_session_idle(chat_id: int, expected_epoch: int = None) -> str:
    """Called when a desktop chat session goes idle. Sweeps new messages
    since the last sweep, writes an L2 nightstand entry if worth it.

    DREAM-LIFECYCLE-01: returns a structured lifecycle outcome
    (DREAM_COMPLETED / DREAM_INELIGIBLE / DREAM_FAILED / DREAM_CANCELLED)
    so the caller can distinguish "this idle window was validly consumed
    by a completed sweep" from "this probe was ineligible, failed, or
    cancelled" — the latter must stay admit-able on a later tick.

    expected_epoch (3A.2 Part H): the emergency-stop epoch the caller
    captured BEFORE spawning this onto its own background thread (see
    ui/main_window.py's _check_dream_idle). None (default) preserves
    existing behavior exactly for any caller that doesn't track epochs —
    runs with no emergency execution lease at all, same as every pre-3A.2
    call site. Passing a real epoch wraps the actual sweep in an
    execution_scope() pinned to it, so a stale/latched epoch admits no
    work at all, and a latch landing mid-sweep (while the blocking
    summarizer/profile-curation calls are in flight) discards their
    output before either persistent write rather than trusting it."""
    if not getattr(config, "DREAM_SWEEP_ENABLED", False) or not chat_id:
        return DREAM_INELIGIBLE

    if expected_epoch is None:
        return _run_session_idle_sweep(chat_id, expected_epoch=None)

    from core import emergency_stop
    try:
        with emergency_stop.execution_scope(
            kind="dream_sweep", label=str(chat_id), expected_epoch=expected_epoch,
        ):
            return _run_session_idle_sweep(chat_id, expected_epoch=expected_epoch)
    except emergency_stop.EmergencyStopError:
        # stale/latched before admission -- no summarizer call, no writes
        return DREAM_CANCELLED


def _run_session_idle_sweep(chat_id: int, expected_epoch: int = None) -> str:
    msgs = load_chat_messages(chat_id)
    last = _last_dream_sweep.get(chat_id)
    # FE-29: last is datetime.now().isoformat() — local time, no tz offset —
    # compared straight against msgs' created_at strings. Fine today because
    # the same local clock writes both sides of the comparison. This breaks
    # silently (under-or-over-filters instead of erroring) the moment either
    # side moves to UTC — flagging for the compaction build, which reuses
    # this exact watermark pattern.
    if last:
        msgs = [m for m in msgs if m.get("created_at", "") > last]
    if not msgs:
        return DREAM_INELIGIBLE

    raw_text = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-40:] if m.get("content"))
    if estimate_tokens(raw_text) < getattr(config, "DREAM_MIN_TOKENS", 800):
        return DREAM_INELIGIBLE

    summary = run_summarization_call(raw_text)
    if not summary:
        return DREAM_FAILED

    if expected_epoch is not None:
        from core import emergency_stop
        if not emergency_stop.execution_permitted(expected_epoch):
            # Latched/stale while the summarizer call was blocked -- allowed
            # to return, but its output is discarded here, before the
            # Palace write, and the watermark below is never advanced.
            return DREAM_CANCELLED

    try:
        palace_store(
            content=summary,
            wing="nightstand",
            room=str(chat_id),
            layer=2,
            tags=["dream-sweep", f"session:{chat_id}"]
        )
    except Exception as e:
        # DREAM-LIFECYCLE-01: a failed persistence attempt is a FAILED
        # outcome, not a crash — the watermark below is never advanced and
        # the window stays admit-able for a later retry.
        print(f"[DREAMING] palace write failed: {e}", flush=True)
        return DREAM_FAILED

    if getattr(config, "HUMAN_PROFILE_CURATION_ENABLED", False):
        from core.persistence import load as load_prefs, save as save_prefs
        prefs = load_prefs()
        bio = prefs.get("human_bio", "")
        existing = prefs.get("human_profile_curated", "")
        updated = curate_human_profile(raw_text, bio, existing)
        if updated:
            if expected_epoch is not None:
                from core import emergency_stop
                if not emergency_stop.execution_permitted(expected_epoch):
                    return  # discard stale/latched curation output before saving
            prefs["human_profile_curated"] = updated.strip()
            save_prefs(prefs)
            print(f"[DREAMING] human profile curated", flush=True)

    _last_dream_sweep[chat_id] = datetime.now().isoformat()
    print(f"[DREAMING] session {chat_id} swept into nightstand", flush=True)
    return DREAM_COMPLETED