"""Pure helpers for Lumina's owner-facing slash-command cockpit."""
from dataclasses import dataclass


KNOWN_COMMANDS = {"status", "btw", "compact", "stop"}
@dataclass(frozen=True)
class OperatorCommand:
    name: str
    argument: str = ""
    known: bool = True


def parse_operator_command(text: str):
    """Parse a leading slash command, or return None for normal chat text."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None

    head, sep, tail = stripped.partition(" ")
    name = head[1:].strip().lower()
    return OperatorCommand(
        name=name,
        argument=tail.strip() if sep else "",
        known=name in KNOWN_COMMANDS,
    )


def command_help() -> str:
    return "Available operator commands: /status · /btw <question> · /compact · /stop"


def format_duration(seconds: float) -> str:
    """Compact human-readable duration for live operator status."""
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_tokens(count: int) -> str:
    count = max(0, int(count or 0))
    if count >= 1_000_000:
        value = count / 1_000_000
        return f"{value:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        value = count / 1_000
        return f"{value:.1f}k".replace(".0k", "k")
    return str(count)


def compaction_cut_index(history: list, keep_user_turns: int = 2):
    """Index of the oldest message that must remain live, or None if too short.

    The cut always lands immediately before a user turn, so a retained tail
    can never begin in the middle of an assistant tool-call/tool-result group.
    """
    keep_user_turns = max(1, int(keep_user_turns or 1))
    user_indexes = [i for i, m in enumerate(history) if m.get("role") == "user"]
    if len(user_indexes) <= keep_user_turns:
        return None
    return user_indexes[-keep_user_turns]


def persisted_compaction_skip_count(messages: list, keep_user_turns: int = 2) -> int:
    """How many persisted user/assistant rows precede the retained user tail."""
    conversational = [m for m in messages if m.get("role") in ("user", "assistant")]
    cut = compaction_cut_index(conversational, keep_user_turns=keep_user_turns)
    return int(cut or 0)


def _content_for_compaction(content) -> str:
    """Text-safe representation that never feeds attachment base64 to the summarizer."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = str(block.get("type") or "multipart")
            if block_type == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
            elif block_type in ("image", "image_url"):
                parts.append("[image attachment]")
            elif block_type in ("audio", "input_audio"):
                parts.append("[audio attachment]")
            else:
                parts.append(f"[{block_type} attachment]")
        return " ".join(parts).strip()
    if content is None:
        return ""
    return str(content).strip()


def _message_for_compaction(message: dict) -> str:
    role = str(message.get("role") or "message")
    content = _content_for_compaction(message.get("content"))
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        names = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            names.append(str(fn.get("name") or call.get("name") or "tool"))
        if names:
            suffix = f"[tool calls: {', '.join(names)}]"
            content = f"{content} {suffix}".strip()
    return f"{role}: {content}".strip()


def chunk_compaction_history(messages: list, max_chars: int = 5500) -> list[str]:
    """Serialize history into bounded chunks without splitting normal messages.

    The underlying utility summarizer truncates each raw_text argument at 6000
    characters, so this deliberately stays below that ceiling.
    """
    max_chars = max(256, min(5900, int(max_chars or 5500)))
    lines = [_message_for_compaction(m) for m in messages]
    lines = [line for line in lines if line and not line.endswith(":")]
    chunks = []
    current = ""

    for line in lines:
        if len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(line), max_chars):
                chunks.append(line[start:start + max_chars])
            continue

        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = line

    if current:
        chunks.append(current)
    return chunks


def unwrap_background_result(entry: dict) -> str:
    """Turn task_queue + spawn_subagent's nested result shape into display text."""
    if not entry:
        return "Background sidequest result expired before it could be displayed."

    status = entry.get("status")
    if status == "success":
        inner = entry.get("result")
        if isinstance(inner, dict) and "success" in inner:
            if inner.get("success"):
                return str(inner.get("result") or "(completed with no text result)")
            return f"Sidequest failed: {inner.get('error') or 'unknown error'}"
        return str(inner or "(completed with no text result)")
    if status == "error":
        result = entry.get("result")
        if isinstance(result, dict):
            result = result.get("error") or result
        return f"Sidequest failed: {result}"
    if status == "cancelled":
        return "Sidequest was cancelled before it started."
    return f"Sidequest is still {status or 'unknown'}."
