"""
Meta Tools — self-awareness and system control.
Lean descriptions. No essays.
"""

from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_time() -> str:
    """Return current date and time."""
    return datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")


def reset_chat(context_manager) -> str:
    """Clear conversation history."""
    context_manager.clear()
    return "Chat history cleared."


def view_prompt(context_manager) -> str:
    """Return the current fully assembled system prompt including palace memory."""
    return context_manager._build_system_prompt()


def edit_prompt(context_manager, new_prompt: str) -> str:
    """Replace the system prompt."""
    context_manager.update_system_prompt(new_prompt)
    return "System prompt updated."


def list_tools(registry) -> str:
    """List all available tools."""
    tools = registry.list_tools()
    return "Available tools:\n" + "\n".join(f"  - {t}" for t in tools)


def register_meta_tools(registry, context_manager):
    """Register all meta tools with the registry."""

    registry.register(
        "get_time", get_time,
        "Return current date and time.",
        {"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        "list_tools", lambda: list_tools(registry),
        "List all available tools.",
        {"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        "view_prompt", lambda: view_prompt(context_manager),
        "Return the current system prompt.",
        {"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        "edit_prompt", lambda new_prompt: edit_prompt(context_manager, new_prompt),
        "Replace the system prompt with new_prompt.",
        {
            "type": "object",
            "properties": {
                "new_prompt": {"type": "string", "description": "New system prompt text."}
            },
            "required": ["new_prompt"]
        }
    )

    registry.register(
        "reset_chat", lambda: reset_chat(context_manager),
        "Clear all conversation history.",
        {"type": "object", "properties": {}, "required": []}
    )
