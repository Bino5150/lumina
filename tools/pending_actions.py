"""
Generic staging + approval gate for MB-33 Tier 2 -- irreversible/high-blast-
radius tool calls (edit_prompt, reset_chat, delete_knowledge, delete_memory)
that shouldn't fire on the model's say-so alone. Same principle as
toolmaker.py's create_tool/approve_pending_tool pipeline, generalized so
these four don't each need their own bespoke staging logic. (A fifth kind,
palace_promote, is staged here as a REQUEST only and is never applied by
_apply_action -- see PALACE_PROMOTE below.)

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


def _stage_entry(kind: str, payload: dict, reason: str = "") -> str:
    """Writes one queue entry + its 'staged' audit line; returns the action id."""
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
    return action_id


def stage_action(kind: str, payload: dict, reason: str = "") -> str:
    """Records a requested action without performing it."""
    action_id = _stage_entry(kind, payload, reason)
    return (f"Staged as action #{action_id} ({kind}) — requires approval in "
            f"Settings > Tools > Pending Actions before it takes effect. Not applied.")


# PALACE-GUARD-01B-3: a request to promote one Palace drawer or hall into L0/L1.
# The entry is a REQUEST only. Its payload is closed and code-generated (a
# target kind, a target id and a destination layer -- no model free text), and
# nothing about it -- queue membership, audit lines, any field it carries -- is
# an approval: approval is an owner event over the record's LIVE state
# (01B-4), never something read from this file.
PALACE_PROMOTE = "palace_promote"
PALACE_PROMOTE_TARGET_KINDS = ("drawer", "hall")


def stage_palace_promotion(target_kind: str, target_id: int, dest_layer: int) -> str:
    """Stages a promotion request for exactly one drawer or hall; returns the
    action id. Not registered as an agent tool: reachable only from the
    palace_remember/palace_hall wrappers after their L2 write has committed."""
    if target_kind not in PALACE_PROMOTE_TARGET_KINDS:
        raise ValueError(f"unknown promotion target kind {target_kind!r}")
    if type(target_id) is not int or type(dest_layer) is not int or dest_layer not in (0, 1):
        raise ValueError("promotion request needs an int target id and a destination layer of 0 or 1")
    return _stage_entry(
        PALACE_PROMOTE,
        {"target_kind": target_kind, "target_id": target_id, "dest_layer": dest_layer},
        reason=f"requested L{dest_layer} via palace_remember/palace_hall (stored at L2)",
    )


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


def _complete_palace_promotion(action_id: str, receipt: dict) -> str:
    """Bookkeeping AFTER core.palace_promotion has already promoted the record
    under a live owner approval: drop the staged request and write the
    'approved' audit line carrying the promotion receipt id. This is not the
    approval and grants nothing -- calling it (or forging its inputs) changes no
    Palace state; it only tidies the queue. Not registered as an agent tool."""
    queue = _load_queue()
    entry = queue.pop(action_id, None)
    _save_queue(queue)
    payload = (entry or {}).get("payload") or {
        "target_kind": receipt.get("target_kind"), "target_id": receipt.get("target_id"),
        "dest_layer": receipt.get("to_layer"),
    }
    _append_audit(
        "approved", action_id, PALACE_PROMOTE, payload,
        reason=f"owner promotion over live state; receipt {receipt.get('approval_id')}",
    )
    return (f"Promoted {receipt.get('target_kind')} #{receipt.get('target_id')} to "
            f"L{receipt.get('to_layer')} (receipt {str(receipt.get('approval_id'))[:8]}).")


def _apply_action(action_id: str, agent) -> str:
    """THE gate. Not registered as an agent tool on purpose."""
    queue = _load_queue()
    if action_id not in queue:
        return f"[Error: no pending action '{action_id}'.]"
    entry = queue[action_id]
    kind = entry["kind"]
    payload = entry["payload"]

    if kind == PALACE_PROMOTE:
        # The generic Yes/No box shows only this file's JSON, which any
        # write_file call can forge. A promotion is approved only by the owner
        # review over the record's LIVE state; queue possession is not approval.
        # Refused before anything is touched, and the request stays queued.
        return ("[Refused: a Palace promotion is approved only through the owner "
                "review of the record's live state. This staged entry is a request, "
                "not an approval.]")

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
