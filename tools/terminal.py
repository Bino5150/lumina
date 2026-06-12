"""
Terminal Tool — execute shell commands, return output.
"""

import subprocess
import os


def register_terminal_tools(registry):

    def run_command(command: str, cwd: str = None, timeout: int = 30) -> str:
        """Execute a shell command and return stdout/stderr."""
        try:
            cwd = os.path.expanduser(cwd) if cwd else os.path.expanduser("~")
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            parts = []
            if result.stdout.strip():
                parts.append(f"[stdout]\n{result.stdout.strip()}")
            if result.stderr.strip():
                parts.append(f"[stderr]\n{result.stderr.strip()}")
            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")
            return "\n".join(parts) if parts else "[No output]"
        except subprocess.TimeoutExpired:
            return f"[Error: command timed out after {timeout}s]"
        except Exception as e:
            return f"[Error: {e}]"

    registry.register(
        name="run_command",
        fn=run_command,
        description="Execute a shell command and return stdout and stderr.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "cwd": {"type": "string", "description": "Working directory. Default home dir."},
                "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."}
            },
            "required": ["command"]
        }
    )