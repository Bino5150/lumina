"""Pure helpers for Lumina's owner-facing slash-command cockpit."""
from dataclasses import dataclass


KNOWN_COMMANDS = {"status", "btw"}


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
    return "Available operator commands: /status · /btw <question>"


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
