"""
Generic staging + approval gate for MB-33 Tier 2 -- irreversible/high-blast-
radius tool calls (edit_prompt, reset_chat, delete_knowledge, delete_memory)
that shouldn't fire on the model's say-so alone. Same principle as
toolmaker.py's create_tool/approve_pending_tool pipeline, generalized so
these four don't each need their own bespoke staging logic.

stage_action() records what was requested and never performs it. _apply_action()
is NOT registered with the agent's tool registry -- reachable only from the
Pending Actions panel in Settings, called against the live agent's real
registry/context_manager, same proven pattern as tool approval. Nothing in a
chat turn, injected or not, can reach _apply_action.
"""

import os
import json
import uuid
from datetime import datetime

import config

QUEUE_PATH = os.path.join(config.DATA_DIR, "memory", "pending_actions.json")
AUDIT_LOG_PATH = os.path.join(config.DATA_DIR, "memory", "pending_actions_audit.log")


def _load_queue() -> dict:
    if not os.path.exists(QUEUE_PATH):
        return {}
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_queue(queue: dict):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def _append_audit(event: str, action_id: str, kind: str, payload: dict, reason: str = ""):
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "event": event,  # "staged" | "approved" | "rejected"
        "action_id": action_id,
        "kind": kind,
        "payload": payload,
        "reason": reason,
    }
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[PENDING_ACTIONS] audit log write failed: {e}", flush=True)


def stage_action(kind: str, payload: dict, reason: str = "") -> str:
    """Records a requested action without performing it."""
    action_id = uuid.uuid4().hex[:8]
    queue = _load_queue()
    queue[action_id] = {
        "kind": kind,
        "payload": payload,
        "reason": reason,
        "staged_at": datetime.now().isoformat(),
    }
    _save_queue(queue)
    _append_audit("staged", action_id, kind, payload, reason)
    return (f"Staged as action #{action_id} ({kind}) — requires approval in "
            f"Settings > Tools > Pending Actions before it takes effect. Not applied.")


def list_pending_actions() -> str:
    queue = _load_queue()
    if not queue:
        return "No pending actions."
    return "\n".join(
        f"[{aid}] {a['kind']} — {json.dumps(a['payload'])} (staged {a['staged_at']})"
        for aid, a in queue.items()
    )


def reject_pending_action(action_id: str) -> str:
    """Safe -- only removes a staged entry, never touches live state."""
    queue = _load_queue()
    if action_id not in queue:
        return f"No pending action '{action_id}'."
    entry = queue.pop(action_id)
    _save_queue(queue)
    _append_audit("rejected", action_id, entry["kind"], entry["payload"], entry.get("reason", ""))
    return f"Rejected action #{action_id} ({entry['kind']}). Discarded, never applied."


def _apply_action(action_id: str, agent) -> str:
    """THE gate. Not registered as an agent tool on purpose."""
    queue = _load_queue()
    if action_id not in queue:
        return f"[Error: no pending action '{action_id}'.]"
    entry = queue[action_id]
    kind = entry["kind"]
    payload = entry["payload"]

    try:
        if kind == "edit_prompt":
            agent.ctx.update_system_prompt(payload["new_prompt"])
            result = "System prompt updated."
        elif kind == "reset_chat":
            agent.ctx.clear()
            result = "Chat history cleared."
        elif kind == "delete_knowledge":
            from tools.knowledge import _delete_knowledge_direct
            result = _delete_knowledge_direct(payload.get("entry_id"), payload.get("category"))
        elif kind == "delete_memory":
            from tools.memory import _delete_memory_direct
            result = _delete_memory_direct(payload["memory_id"])
        else:
            return f"[Error: unknown action kind '{kind}'.]"
    except Exception as e:
        return f"[Error applying action: {e}]"

    queue.pop(action_id)
    _save_queue(queue)
    _append_audit("approved", action_id, kind, payload, entry.get("reason", ""))
    return result
