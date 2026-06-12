"""
Toolmaker — Lumina can write, save, and hot-load her own tools.
"""

import os
import importlib.util
import sys
import traceback


def register_toolmaker_tools(registry, agent):

    def create_tool(name: str, description: str, code: str) -> str:
        """
        Write a new tool to disk and hot-load it into the registry.
        code must define a function with the same name as `name`,
        and a register_{name}_tool(registry) function that registers it.
        """
        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
        filepath = os.path.join(tools_dir, f"{name}.py")

        # Safety check — don't overwrite core tools
        protected = {"registry", "meta", "memory", "knowledge", "web",
                     "filesystem", "sandbox", "terminal", "toolmaker"}
        if name in protected:
            return f"[Error: '{name}' is a protected tool name.]"

        # Write the tool file
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return f"[Error writing tool file: {e}]"

        # Hot-load the module
        try:
            spec = importlib.util.spec_from_file_location(name, filepath)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            return f"[Error loading tool module: {traceback.format_exc()}]"

        # Call the register function
        register_fn_name = f"register_{name}_tool"
        if not hasattr(module, register_fn_name):
            return f"[Error: module missing '{register_fn_name}' function.]"

        try:
            getattr(module, register_fn_name)(registry)
        except Exception as e:
            return f"[Error registering tool: {traceback.format_exc()}]"

        return f"[Tool '{name}' created and loaded successfully — ready to use.]"

    def list_custom_tools() -> str:
        """List all custom tools written by Lumina."""
        tools_dir = os.path.dirname(os.path.abspath(__file__))
        protected = {"registry", "meta", "memory", "knowledge", "web",
                     "filesystem", "sandbox", "terminal", "toolmaker",
                     "__init__"}
        custom = []
        for f in os.listdir(tools_dir):
            if f.endswith(".py"):
                stem = f[:-3]
                if stem not in protected:
                    custom.append(stem)
        if not custom:
            return "[No custom tools yet.]"
        return f"[Custom tools: {', '.join(sorted(custom))}]"

    def delete_tool(name: str) -> str:
        """Delete a custom tool file and unregister it."""
        protected = {"registry", "meta", "memory", "knowledge", "web",
                     "filesystem", "sandbox", "terminal", "toolmaker"}
        if name in protected:
            return f"[Error: '{name}' is protected and cannot be deleted.]"

        tools_dir = os.path.dirname(os.path.abspath(__file__))
        filepath = os.path.join(tools_dir, f"{name}.py")

        if not os.path.exists(filepath):
            return f"[Error: tool file '{name}.py' not found.]"

        try:
            os.remove(filepath)
            # Remove from registry if present
            if name in agent.registry._tools:
                del agent.registry._tools[name]
            # Remove from sys.modules
            if name in sys.modules:
                del sys.modules[name]
            return f"[Tool '{name}' deleted and unregistered.]"
        except Exception as e:
            return f"[Error deleting tool: {e}]"

    registry.register(
        name="create_tool",
        fn=create_tool,
        description="Write a new Python tool to disk and hot-load it into the registry.",
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
        name="list_custom_tools",
        fn=list_custom_tools,
        description="List all custom tools written by Lumina.",
        parameters={
            "type": "object",
            "properties": {},
            "required": []
        }
    )

    registry.register(
        name="delete_tool",
        fn=delete_tool,
        description="Delete a custom tool and unregister it from the registry.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the tool to delete."}
            },
            "required": ["name"]
        }
    )