"""
Dreaming — idle-triggered memory sweep for the main build.
Fires on session idle, summarizes what's happened since the last sweep,
writes to the nightstand wing (isolated from curated Palace content).
"""
import re
import requests
from datetime import datetime
from tools.memory import load_chat_messages
from tools.palace import palace_store
from core.context import estimate_tokens
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
_resolved_model_cache: dict[str, str | None] = {"model": None}


def _resolve_model() -> str | None:
    """Ask the backend what's actually loaded instead of trusting
    config.DEFAULT_MODEL. F-62 quick fix: DEFAULT_MODEL is None by default,
    so the old code sent "model": None straight into the OpenAI-shape
    request body — a guaranteed 400 on every dream sweep on the default
    llama.cpp/LM Studio backend. Cached after first success since the
    loaded model doesn't change mid-session; falls back to DEFAULT_MODEL
    (still possibly None) only if the backend can't be reached at all,
    so a transient network hiccup doesn't wipe the cache."""
    if _resolved_model_cache["model"]:
        return _resolved_model_cache["model"]
    try:
        resp = requests.get(f"{config.LLM_BACKEND_URL}/models", timeout=5)
        data = resp.json().get("data", [])
        if data:
            _resolved_model_cache["model"] = data[0]["id"]
            return _resolved_model_cache["model"]
    except Exception as e:
        print(f"[DREAMING] model resolution failed: {e}", flush=True)
    return config.DEFAULT_MODEL


def run_summarization_call(raw_text: str) -> str | None:
    try:
        resp = requests.post(
            f"{config.LLM_BACKEND_URL}/chat/completions",
            json={
                "model": _resolve_model(),
                "messages": [
                    {"role": "user", "content": f"{DREAM_PROMPT}\n\n{raw_text[:6000]}"},
                    {"role": "assistant", "content": "SUMMARY:"},   # non-empty prefill — S23 fix for thinking bleed
                ],
                "max_tokens": 500,
                "temperature": 0.3,
                "thinking": {"type": "disabled"},
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=30,
        )
        if not resp.ok:
            print(f"[DREAMING] error body: {resp.text}", flush=True)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)  # unclosed — truncated mid-think
        content = content.strip()
        content = re.sub(r'^SUMMARY:\s*', '', content, flags=re.IGNORECASE)
        if not content:
            print("[DREAMING] response was empty after stripping — skipping write", flush=True)
            return None
        return content
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