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

_last_dream_sweep: dict[int, str] = {}  # chat_id -> ISO timestamp of last sweep


def run_summarization_call(raw_text: str) -> str | None:
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
    """
    try:
        backend = get_llm_backend()
        return backend.complete_utility(
            prompt=f"{DREAM_PROMPT}\n\n{raw_text[:6000]}",
            prefill="SUMMARY:",
            max_tokens=500,
            temperature=0.3,
        )
    except Exception as e:
        print(f"[DREAMING] summarization call failed: {e}", flush=True)
        return None

def on_session_idle(chat_id: int):
    """Called when a desktop chat session goes idle. Sweeps new messages
    since the last sweep, writes an L2 nightstand entry if worth it."""
    if not getattr(config, "DREAM_SWEEP_ENABLED", False) or not chat_id:
        return

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
        return

    raw_text = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-40:] if m.get("content"))
    if estimate_tokens(raw_text) < getattr(config, "DREAM_MIN_TOKENS", 800):
        return

    summary = run_summarization_call(raw_text)
    if not summary:
        return

    palace_store(
        content=summary,
        wing="nightstand",
        room=str(chat_id),
        layer=2,
        tags=["dream-sweep", f"session:{chat_id}"]
    )
    _last_dream_sweep[chat_id] = datetime.now().isoformat()
    print(f"[DREAMING] session {chat_id} swept into nightstand", flush=True)