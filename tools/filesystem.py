"""
Filesystem Tools — read, write, list, search files.
"""

import os
import fnmatch

def register_filesystem_tools(registry):

    def read_file(path: str, start: int = 0, max_bytes: int = 32000) -> str:
        """Read a file and return its contents."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return f"[Error: file not found: {path}]"
        if not os.path.isfile(path):
            return f"[Error: not a file: {path}]"
        size = os.path.getsize(path)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(start)
                content = f.read(max_bytes)
            truncated = (start + max_bytes) < size
            header = f"[file: {path} | {size} bytes total | reading from byte {start}{' | more remaining' if truncated else ' | end of file'}]\n"
            return header + content
        except Exception as e:
            return f"[Error reading {path}: {e}]"

    def write_file(path: str, content: str, mode: str = "overwrite") -> str:
        """Write content to a file. mode: overwrite or append."""
        path = os.path.expanduser(path)
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            write_mode = 'a' if mode == 'append' else 'w'
            with open(path, write_mode, encoding='utf-8') as f:
                f.write(content)
            size = os.path.getsize(path)
            return f"[Written: {path} | {size} bytes | mode: {mode}]"
        except Exception as e:
            return f"[Error writing {path}: {e}]"

    def list_dir(path: str = ".", show_hidden: bool = False) -> str:
        """List contents of a directory."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return f"[Error: path not found: {path}]"
        if not os.path.isdir(path):
            return f"[Error: not a directory: {path}]"
        try:
            entries = os.listdir(path)
            if not show_hidden:
                entries = [e for e in entries if not e.startswith('.')]
            entries.sort()
            lines = [f"[directory: {os.path.abspath(path)}]"]
            for entry in entries:
                full = os.path.join(path, entry)
                if os.path.isdir(full):
                    lines.append(f"  📁 {entry}/")
                else:
                    size = os.path.getsize(full)
                    lines.append(f"  📄 {entry} ({size} bytes)")
            lines.append(f"\n{len(entries)} items")
            return '\n'.join(lines)
        except Exception as e:
            return f"[Error listing {path}: {e}]"

    def search_files(path: str = ".", pattern: str = "*", content: str = "") -> str:
        """Search for files by name pattern and optionally by content."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return f"[Error: path not found: {path}]"
        matches = []
        try:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for filename in fnmatch.filter(files, pattern):
                    filepath = os.path.join(root, filename)
                    if content:
                        try:
                            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                                if content.lower() in f.read().lower():
                                    matches.append(filepath)
                        except Exception:
                            pass
                    else:
                        matches.append(filepath)
            if not matches:
                return f"[No files found matching '{pattern}'" + (f" containing '{content}'" if content else "") + "]"
            result = f"[Found {len(matches)} file(s)]\n"
            result += '\n'.join(matches[:50])
            if len(matches) > 50:
                result += f"\n... and {len(matches)-50} more"
            return result
        except Exception as e:
            return f"[Error searching {path}: {e}]"

    registry.register(
            name="read_file",
            fn=read_file,
            description="Read a file and return its contents. For large files, use start offset to page through. The header tells you total file size and whether more remains.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read. Supports ~ expansion."},
                    "start": {"type": "integer", "description": "Byte offset to start reading from. Default 0. Use to page through large files."},
                    "max_bytes": {"type": "integer", "description": "Max bytes to read. Default 32000."}
                },
                "required": ["path"]
            }
        )

    registry.register(
            name="write_file",
            fn=write_file,
            description="Write content to a file on disk.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write."},
                    "content": {"type": "string", "description": "Content to write."},
                    "mode": {"type": "string", "description": "overwrite or append. Default overwrite."}
                },
                "required": ["path", "content"]
            }
        )

    registry.register(
            name="list_dir",
            fn=list_dir,
            description="List contents of a directory.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path. Default current dir."},
                    "show_hidden": {"type": "boolean", "description": "Include hidden files. Default false."}
                },
                "required": []
            }
        )

    registry.register(
            name="search_files",
            fn=search_files,
            description="Search for files by name pattern and optionally by content string.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Root directory to search. Default current dir."},
                    "pattern": {"type": "string", "description": "Filename glob pattern e.g. '*.py'. Default '*'."},
                    "content": {"type": "string", "description": "Optional string to search for inside files."}
                },
                "required": []
            }
        )