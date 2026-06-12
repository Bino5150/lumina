import difflib
import subprocess
from tools.registry import ToolRegistry


def _unified_diff(old_text: str, new_text: str, old_label: str = "before", new_label: str = "after") -> str:
    # Ensure trailing newline so difflib renders cleanly
    if old_text and not old_text.endswith("\n"):
        old_text += "\n"
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label))
    return "".join(diff) if diff else "(no differences)"

def register_diff_tools(registry: ToolRegistry):

    def diff_texts(old_text: str, new_text: str, old_label: str = "before", new_label: str = "after") -> str:
        try:
            result = _unified_diff(old_text, new_text, old_label, new_label)
            return f"```diff\n{result}\n```"
        except Exception as e:
            return f"Error computing diff: {e}"

    def diff_files(old_path: str, new_path: str) -> str:
        try:
            with open(old_path, "r", encoding="utf-8", errors="replace") as f:
                old_text = f.read()
            with open(new_path, "r", encoding="utf-8", errors="replace") as f:
                new_text = f.read()
            result = _unified_diff(old_text, new_text, old_path, new_path)
            return f"```diff\n{result}\n```"
        except FileNotFoundError as e:
            return f"File not found: {e}"
        except Exception as e:
            return f"Error: {e}"

    def apply_patch(original_text: str, patch: str) -> str:
        try:
            result = subprocess.run(
                ["patch", "-p0", "--output=-"],
                input=patch.encode(),
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout.decode()
            return f"Patch failed: {result.stderr.decode()}"
        except Exception as e:
            return f"Error: {e}"

    registry.register(
        name="diff_texts",
        fn=diff_texts,
        description="Compare two text strings and return a unified diff showing what changed. Use for comparing code, configs, or any text.",
        parameters={
            "type": "object",
            "properties": {
                "old_text": {"type": "string", "description": "The original/before text"},
                "new_text": {"type": "string", "description": "The modified/after text"},
                "old_label": {"type": "string", "description": "Label for the old version (default: 'before')"},
                "new_label": {"type": "string", "description": "Label for the new version (default: 'after')"},
            },
            "required": ["old_text", "new_text"],
        },
    )

    registry.register(
        name="diff_files",
        fn=diff_files,
        description="Compare two files on disk and return a unified diff. Provide absolute paths to both files.",
        parameters={
            "type": "object",
            "properties": {
                "old_path": {"type": "string", "description": "Path to the original file"},
                "new_path": {"type": "string", "description": "Path to the modified file"},
            },
            "required": ["old_path", "new_path"],
        },
    )

    registry.register(
        name="apply_patch",
        fn=apply_patch,
        description="Apply a unified diff patch to text and return the patched result.",
        parameters={
            "type": "object",
            "properties": {
                "original_text": {"type": "string", "description": "The original text to patch"},
                "patch": {"type": "string", "description": "Unified diff patch string"},
            },
            "required": ["original_text", "patch"],
        },
    )