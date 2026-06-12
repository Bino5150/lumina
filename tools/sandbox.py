"""
Code Execution Sandbox — run Python snippets, return stdout/stderr.
"""

import sys
import io
import traceback
import contextlib


def register_sandbox_tools(registry):

    def run_python(code: str, timeout: int = 10) -> str:
        """Execute a Python code snippet and return stdout/stderr."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        try:
            with contextlib.redirect_stdout(stdout_buf):
                with contextlib.redirect_stderr(stderr_buf):
                    exec(compile(code, "<sandbox>", "exec"), {})
        except Exception:
            stderr_buf.write(traceback.format_exc())

        stdout = stdout_buf.getvalue()
        stderr = stderr_buf.getvalue()

        parts = []
        if stdout:
            parts.append(f"[stdout]\n{stdout}")
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if not parts:
            return "[No output]"
        return "\n".join(parts)

    registry.register(
        name="run_python",
        fn=run_python,
        description="Execute a Python code snippet and return stdout and stderr.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute."},
            },
            "required": ["code"]
        }
    )