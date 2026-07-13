"""
Tool Registry
Loads tools, builds lean OpenAI-compatible schemas, dispatches calls.
"""

import importlib
import traceback
from typing import Callable, Optional


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}      # name -> {fn, schema}
        self._disabled: set[str] = set()        # names of disabled tools
        self._gate_fn: Optional[Callable[[str], tuple]] = None
        # gate_fn(name) -> (allowed: bool, reason: str). Checked AFTER _disabled —
        # for tools that are enabled but need one more runtime check (PIN) before firing.

    def register(self, name: str, fn: Callable, description: str, parameters: dict):
        """Register a tool with a lean description."""
        self._tools[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            }
        }

    def enable(self, name: str):
        """Enable a previously disabled tool."""
        self._disabled.discard(name)

    def disable(self, name: str):
        """Disable a tool — excluded from schemas and call dispatch."""
        self._disabled.add(name)

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled

    def set_disabled(self, names: list[str]):
        """Bulk-set the disabled set (e.g. loaded from prefs on startup)."""
        self._disabled = set(names)

    def get_disabled(self) -> list[str]:
        """Return list of disabled tool names for persistence."""
        return list(self._disabled)

    def all_tool_names(self) -> list[str]:
        """Full registered universe, regardless of disabled state. Use this — never
        ._tools directly, never get_schemas()/list_enabled() — when computing a
        profile's complement. The filtered accessors silently lose track of
        anything already disabled."""
        return list(self._tools.keys())

    def set_gate(self, gate_fn):
        """Install an additional dispatch-time check (PIN gate). gate_fn(name) -> (allowed, reason)."""
        self._gate_fn = gate_fn

    def get_schemas(self, names: list = None) -> list:
        """Return OpenAI tool schemas for enabled tools. Optionally filter by name list."""
        if names is None:
            return [
                t["schema"] for n, t in self._tools.items()
                if n not in self._disabled
            ]
        return [
            self._tools[n]["schema"] for n in names
            if n in self._tools and n not in self._disabled
        ]

    def call(self, name: str, args: dict) -> str:
        """Execute a tool by name. Always returns a string."""
        if name not in self._tools:
            return f"[Tool error: '{name}' not found]"
        if name in self._disabled:
            return f"[Tool '{name}' is currently disabled.]"
        if self._gate_fn:
            allowed, reason = self._gate_fn(name)
            if not allowed:
                return f"[Tool '{name}' blocked: {reason}]"
        try:
            result = self._tools[name]["fn"](**args)
            return str(result) if result is not None else "[No result]"
        except TypeError as e:
            return f"[Tool error: bad arguments for '{name}': {e}]"
        except Exception as e:
            return f"[Tool error in '{name}': {e}]"

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def list_enabled(self) -> list[str]:
        return [n for n in self._tools if n not in self._disabled]

    def schema_token_estimate(self) -> int:
        """Rough token count for enabled schemas only."""
        total = 0
        for n, t in self._tools.items():
            if n not in self._disabled:
                total += len(str(t["schema"])) // 4
        return total
