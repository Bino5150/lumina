"""Private, durable ChatGPT Plan destination binding for saved chats.

Only a digest of the verified OAuth identity and profile is stored. Raw
subjects, access tokens and reasoning never enter the conversation database.
"""

import hashlib
import hmac
import json

from core.db import connect


class ChatBindingError(RuntimeError):
    pass


def account_digest(profile_id: str, identity: tuple[str, str]) -> str:
    encoded = json.dumps([profile_id, *identity], separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def check_or_bind_chat(chat_id: int, profile_id: str, identity: tuple[str, str],
                       lane: str, model: str) -> None:
    """Atomically bind a fresh chat or refuse a changed destination.

    An older chat with more than its newly submitted user row and no binding
    has unknown provenance. It must start a fresh chat before Plan can send
    that history. The caller invokes this before catalog or inference I/O.
    """
    if type(chat_id) is not int or chat_id <= 0:
        raise ChatBindingError("ChatGPT chat identity is unavailable. Start a new chat.")
    digest = account_digest(profile_id, identity)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chatgpt_plan_chat_bindings (
                chat_id INTEGER PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                account_digest TEXT NOT NULL,
                backend_lane TEXT NOT NULL,
                model TEXT NOT NULL
            )
        """)
        if conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone() is None:
            raise ChatBindingError("ChatGPT chat identity is unavailable. Start a new chat.")
        row = conn.execute(
            "SELECT account_digest, backend_lane, model "
            "FROM chatgpt_plan_chat_bindings WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if row is None:
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            if count > 1:
                raise ChatBindingError(
                    "ChatGPT chat has unbound previous history. Start a new chat."
                )
            conn.execute(
                "INSERT INTO chatgpt_plan_chat_bindings "
                "(chat_id, account_digest, backend_lane, model) VALUES (?, ?, ?, ?)",
                (chat_id, digest, lane, model),
            )
        elif not hmac.compare_digest(row["account_digest"], digest):
            raise ChatBindingError("ChatGPT account changed for this chat. Start a new chat.")
        elif row["backend_lane"] != lane:
            raise ChatBindingError("ChatGPT backend lane changed for this chat. Start a new chat.")
        elif row["model"] != model:
            raise ChatBindingError("ChatGPT model changed for this chat. Start a new chat.")
        conn.commit()
    finally:
        conn.close()


def bound_chat_refuses_lane(chat_id: int, lane: str) -> bool:
    """Keep a Plan-bound chat off every other backend after a Settings swap."""
    if type(chat_id) is not int or chat_id <= 0:
        return False
    if lane == "openai_chatgpt_plan":
        return False  # Its own account/profile/model guard checks the request.
    conn = connect()
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='chatgpt_plan_chat_bindings'"
        ).fetchone()
        if table is None:
            return False
        return conn.execute(
            "SELECT 1 FROM chatgpt_plan_chat_bindings WHERE chat_id=?", (chat_id,)
        ).fetchone() is not None
    finally:
        conn.close()
