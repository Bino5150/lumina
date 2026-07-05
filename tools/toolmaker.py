"""
Toolmaker — Lumina can write her own tools, but they no longer go live
on her own say-so.

F-34 fix: create_tool used to write straight into tools/ and hot-load
immediately — no review gate, and OWNER_ONLY_TOOLS only walls it off from
Discord, not from a prompt-injected owner session. A single crafted string
reaching this tool used to mean live, callable, arbitrary code with zero
human awareness.

New flow:
  1. create_tool()      -> writes to tools/_pending/, does NOT hot-load,
                            appends a full-source entry to the audit log.
                            Only a syntax check (compile(), never exec()) —
                            no code from an unapproved tool ever runs.
  2. list_pending_tools() / show_pending_tool_source() -> read-only,
                            lets the owner review what's waiting, in-chat.
  3. reject_pending_tool() -> deletes a staged file. Still just file
                            deletion — never touches the live registry.
  4. approve_pending_tool() -> the actual gate. Moves the file into tools/
                            and hot-loads it. Deliberately NOT registered
                            with the agent's tool registry, so nothing in
                            a chat turn — injected or not — can call it.
                            Run it from a real terminal: see
                            scripts/approve_tool.py.

delete_tool (removing an already-approved, live tool) is unchanged — it's
the existing undo half of this, same as it always was.
"""

import os
import json
import importlib.util
import sys
import traceback
from datetime import datetime

import config

PROTECTED = {"registry", "meta", "memory", "knowledge", "web",
             "filesystem", "sandbox", "terminal", "toolmaker"}

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_DIR = os.path.join(TOOLS_DIR, "_pending")
AUDIT_LOG_PATH = os.path.join(config.BASE_DIR, "memory", "tool_audit.log")


def _append_audit_log(event: str, name: str, description: str = "", code: str = ""):
    """Append-only forensic trail — every stage of every tool's life,
    full source included, regardless of what happens to it later."""
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "event": event,          # "staged" | "approved" | "rejected" | "deleted"
        "name": name,
        "description": description,
        "code": code,
    }
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[TOOLMAKER] audit log write failed: {e}", flush=True)


def approve_pending_tool(name: str, registry) -> str:
    """
    THE review gate. Not registered as an agent tool on purpose — call this
    from scripts/approve_tool.py, run by a human in a real terminal, after
    actually reading the source (list_pending_tools /
    show_pending_tool_source surface it in-chat for exactly that purpose).

    Moves the staged file from tools/_pending/ into tools/ and hot-loads it.
    This is the only place in the whole toolmaker pipeline where untrusted
    code actually executes.
    """
    if name in PROTECTED:
        return f"[Error: '{name}' is a protected tool name.]"

    pending_path = os.path.join(PENDING_DIR, f"{name}.py")
    if not os.path.exists(pending_path):
        return f"[Error: no pending tool named '{name}'.]"

    live_path = os.path.join(TOOLS_DIR, f"{name}.py")
    with open(pending_path, "r", encoding="utf-8") as f:
        code = f.read()

    try:
        os.replace(pending_path, live_path)
    except Exception as e:
        return f"[Error moving tool into place: {e}]"

    try:
        spec = importlib.util.spec_from_file_location(name, live_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception:
        return f"[Error loading tool module: {traceback.format_exc()}]"

    register_fn_name = f"register_{name}_tool"
    if not hasattr(module, register_fn_name):
        return f"[Error: module missing '{register_fn_name}' function.]"

    try:
        getattr(module, register_fn_name)(registry)
    except Exception:
        return f"[Error registering tool: {traceback.format_exc()}]"

    _append_audit_log("approved", name, code=code)
    return f"[Tool '{name}' approved, loaded, and live.]"


def register_toolmaker_tools(registry, agent):

    def create_tool(name: str, description: str, code: str) -> str:
        """
        Stage a new tool for human review. Does NOT go live and does NOT
        execute — only checked for valid Python syntax. An owner (or
        anyone with the Settings terminal) must explicitly run
        scripts/approve_tool.py before this becomes a callable tool.
        """
        if name in PROTECTED:
            return f"[Error: '{name}' is a protected tool name.]"

        try:
            compile(code, f"<pending:{name}>", "exec")
        except SyntaxError as e:
            return f"[Error: code has a syntax error and was not staged: {e}]"

        os.makedirs(PENDING_DIR, exist_ok=True)
        pending_path = os.path.join(PENDING_DIR, f"{name}.py")
        try:
            with open(pending_path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return f"[Error writing pending tool file: {e}]"

        _append_audit_log("staged", name, description=description, code=code)
        return (f"[Tool '{name}' staged for review — NOT loaded yet. "
                f"Ask the owner to review it (list_pending_tools / "
                f"show_pending_tool_source) and run scripts/approve_tool.py "
                f"{name} from a terminal to activate it.]")

    def list_pending_tools() -> str:
        """List tools staged for review but not yet approved/loaded."""
        os.makedirs(PENDING_DIR, exist_ok=True)
        pending = sorted(f[:-3] for f in os.listdir(PENDING_DIR) if f.endswith(".py"))
        if not pending:
            return "[No tools pending review.]"
        return f"[Pending review: {', '.join(pending)}]"

    def show_pending_tool_source(name: str) -> str:
        """Show the full source of a staged (not-yet-approved) tool, for review."""
        pending_path = os.path.join(PENDING_DIR, f"{name}.py")
        if not os.path.exists(pending_path):
            return f"[Error: no pending tool named '{name}'.]"
        with open(pending_path, "r", encoding="utf-8") as f:
            return f.read()

    def reject_pending_tool(name: str) -> str:
        """Discard a staged tool without ever loading it."""
        pending_path = os.path.join(PENDING_DIR, f"{name}.py")
        if not os.path.exists(pending_path):
            return f"[Error: no pending tool named '{name}'.]"
        try:
            os.remove(pending_path)
            _append_audit_log("rejected", name)
            return f"[Pending tool '{name}' discarded — never loaded.]"
        except Exception as e:
            return f"[Error rejecting tool: {e}]"

    def list_custom_tools() -> str:
        """List all live custom tools written by Lumina."""
        protected = PROTECTED | {"__init__", "_pending"}
        custom = []
        for f in os.listdir(TOOLS_DIR):
            if f.endswith(".py"):
                stem = f[:-3]
                if stem not in protected:
                    custom.append(stem)
        if not custom:
            return "[No custom tools yet.]"
        return f"[Custom tools: {', '.join(sorted(custom))}]"

    def delete_tool(name: str) -> str:
        """Delete a live custom tool file and unregister it."""
        if name in PROTECTED:
            return f"[Error: '{name}' is protected and cannot be deleted.]"

        filepath = os.path.join(TOOLS_DIR, f"{name}.py")

        if not os.path.exists(filepath):
            return f"[Error: tool file '{name}.py' not found.]"

        try:
            os.remove(filepath)
            if name in agent.registry._tools:
                del agent.registry._tools[name]
            if name in sys.modules:
                del sys.modules[name]
            _append_audit_log("deleted", name)
            return f"[Tool '{name}' deleted and unregistered.]"
        except Exception as e:
            return f"[Error deleting tool: {e}]"

    registry.register(
        name="create_tool",
        fn=create_tool,
        description="Stage a new Python tool for human review. Does not go live until approved.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name — also the filename (no .py)."},
                "description": {"type": "string", "description": "What the tool does."},
                "code": {"type": "string", "description": "Full Python source. Must define the tool function and a register_{name}_tool(registry) function."}
            },
            "required": ["name", "description", "code"]
        }
    )

    registry.register(
        name="list_pending_tools",
        fn=list_pending_tools,
        description="List tools staged for review but not yet approved.",
        parameters={"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        name="show_pending_tool_source",
        fn=show_pending_tool_source,
        description="Show the full source of a tool pending review.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pending tool name."}},
            "required": ["name"]
        }
    )

    registry.register(
        name="reject_pending_tool",
        fn=reject_pending_tool,
        description="Discard a pending tool without ever loading it.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pending tool name to discard."}},
            "required": ["name"]
        }
    )

    registry.register(
        name="list_custom_tools",
        fn=list_custom_tools,
        description="List all live custom tools written by Lumina.",
        parameters={
            "type": "object",
            "properties": {},
            "required": []
        }
    )

    registry.register(
        name="delete_tool",
        fn=delete_tool,
        description="Delete a live custom tool and unregister it from the registry.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the tool to delete."}
            },
            "required": ["name"]
        }
    )
