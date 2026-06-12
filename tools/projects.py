"""
Projects Tools — lightweight project workspace management.
"""

import os
import json

PROJECTS_DIR = os.path.expanduser("~/lumina/projects")
PROJECTLIST  = os.path.join(PROJECTS_DIR, "projectlist.md")


def init_projects():
    """Create ~/lumina/projects/ and projectlist.md if they don't exist."""
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    if not os.path.exists(PROJECTLIST):
        with open(PROJECTLIST, 'w', encoding='utf-8') as f:
            f.write("# Projects\n\n")
        print("[Projects] Initialized projectlist.md")


def _read_projectlist() -> str:
    try:
        with open(PROJECTLIST, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"[Error reading projectlist: {e}]"


def _write_projectlist(content: str):
    with open(PROJECTLIST, 'w', encoding='utf-8') as f:
        f.write(content)


def register_projects_tools(registry):

    def create_project(name: str, description: str, root_path: str) -> str:
        """Create a new project workspace."""
        project_dir = os.path.join(PROJECTS_DIR, name)
        try:
            os.makedirs(project_dir, exist_ok=True)
            # Create project.md
            project_md = os.path.join(project_dir, "project.md")
            if not os.path.exists(project_md):
                with open(project_md, 'w', encoding='utf-8') as f:
                    f.write(f"# {name}\n\n**Description:** {description}\n**Root:** {root_path}\n\n## Status\n\n## Notes\n")
            # Create chats.json
            chats_json = os.path.join(project_dir, "chats.json")
            if not os.path.exists(chats_json):
                with open(chats_json, 'w', encoding='utf-8') as f:
                    json.dump([], f, indent=2)
            # Create codebase.md placeholder
            codebase_md = os.path.join(project_dir, "codebase.md")
            if not os.path.exists(codebase_md):
                with open(codebase_md, 'w', encoding='utf-8') as f:
                    f.write(f"# Codebase Index — {name}\n\n*Run refresh_codebase_index to populate.*\n")
            # Append to projectlist.md
            current = _read_projectlist()
            entry = f"- **{name}** `{root_path}` — {description}\n"
            if entry.strip() not in current:
                with open(PROJECTLIST, 'a', encoding='utf-8') as f:
                    f.write(entry)
            return f"[Project created: {project_dir}]"
        except Exception as e:
            return f"[Error creating project '{name}': {e}]"

    def load_project(name: str) -> str:
        """Load a project's project.md handoff document."""
        path = os.path.join(PROJECTS_DIR, name, "project.md")
        if not os.path.exists(path):
            return f"[Error: no project named '{name}'. Check projectlist.]"
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"[Error loading project '{name}': {e}]"

    def update_project(name: str, content: str) -> str:
        """Overwrite a project's project.md with updated content."""
        project_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.exists(project_dir):
            return f"[Error: no project named '{name}'. Create it first.]"
        path = os.path.join(project_dir, "project.md")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"[project.md updated for '{name}']"
        except Exception as e:
            return f"[Error updating project '{name}': {e}]"

    def refresh_codebase_index(name: str, root_path: str,
                                extensions: list = None,
                                exclude_dirs: list = None) -> str:
        """Walk root_path and write a file-level index to codebase.md."""
        project_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.exists(project_dir):
            return f"[Error: no project named '{name}'. Create it first.]"
        root = os.path.expanduser(root_path)
        if not os.path.exists(root):
            return f"[Error: root_path not found: {root}]"
        ext_filter = set(
            e if e.startswith('.') else f'.{e}'
            for e in extensions
        ) if extensions else None
        skip_dirs   = set(exclude_dirs) if exclude_dirs else {
            "__pycache__", ".git", "node_modules", ".mypy_cache",
            "build", "dist", ".venv", "venv", ".idea"
        }
        try:
            lines = [f"# Codebase Index — {name}", f"*Root: {root}*\n"]
            file_count = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in skip_dirs and not d.startswith('.')
                )
                rel_dir = os.path.relpath(dirpath, root)
                for filename in sorted(filenames):
                    if filename.startswith('.'):
                        continue
                    if ext_filter:
                        _, ext = os.path.splitext(filename)
                        if ext.lower() not in ext_filter:
                            continue
                    rel_path = os.path.join(rel_dir, filename)
                    if rel_path.startswith('./'):
                        rel_path = rel_path[2:]
                    full_path = os.path.join(dirpath, filename)
                    size = os.path.getsize(full_path)
                    lines.append(f"- `{rel_path}` ({size}b)")
                    file_count += 1
            lines.append(f"\n*{file_count} files indexed.*")
            content = '\n'.join(lines)
            codebase_path = os.path.join(project_dir, "codebase.md")
            with open(codebase_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"[Codebase index updated: {file_count} files indexed → {codebase_path}]"
        except Exception as e:
            return f"[Error indexing codebase for '{name}': {e}]"

    def load_codebase(name: str) -> str:
        """Load a project's codebase index."""
        path = os.path.join(PROJECTS_DIR, name, "codebase.md")
        if not os.path.exists(path):
            return f"[Error: no codebase index for '{name}'. Run refresh_codebase_index first.]"
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"[Error loading codebase index for '{name}': {e}]"

    def link_chat(name: str, chat_id: int, summary: str) -> str:
        """Link a chat session to a project with a one-line summary."""
        project_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.exists(project_dir):
            return f"[Error: no project named '{name}'. Create it first.]"
        chats_path = os.path.join(project_dir, "chats.json")
        try:
            if os.path.exists(chats_path):
                with open(chats_path, 'r', encoding='utf-8') as f:
                    chats = json.load(f)
            else:
                chats = []
            # Update if already linked, otherwise append
            for entry in chats:
                if entry.get("chat_id") == chat_id:
                    entry["summary"] = summary
                    break
            else:
                chats.append({"chat_id": chat_id, "summary": summary})
            with open(chats_path, 'w', encoding='utf-8') as f:
                json.dump(chats, f, indent=2)
            return f"[Chat {chat_id} linked to project '{name}']"
        except Exception as e:
            return f"[Error linking chat to '{name}': {e}]"

    def get_project_chats(name: str) -> str:
        """List all chats linked to a project."""
        chats_path = os.path.join(PROJECTS_DIR, name, "chats.json")
        if not os.path.exists(chats_path):
            return f"[Error: no project named '{name}' or no chats linked yet.]"
        try:
            with open(chats_path, 'r', encoding='utf-8') as f:
                chats = json.load(f)
            if not chats:
                return f"[No chats linked to '{name}' yet.]"
            lines = [f"# Chats — {name}"]
            for c in chats:
                lines.append(f"- chat_id {c['chat_id']}: {c['summary']}")
            return '\n'.join(lines)
        except Exception as e:
            return f"[Error reading chats for '{name}': {e}]"

    registry.register(
        name="create_project",
        fn=create_project,
        description="Create a new project workspace with project.md, codebase.md, and chats.json.",
        parameters={
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Short project identifier, no spaces. e.g. 'lumina-dev'"},
                "description": {"type": "string", "description": "One-line description of the project."},
                "root_path":   {"type": "string", "description": "Absolute path to the project's source root. e.g. '~/lumina'"}
            },
            "required": ["name", "description", "root_path"]
        }
    )

    registry.register(
        name="load_project",
        fn=load_project,
        description="Load a project's project.md handoff document.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name as listed in projectlist.md"}
            },
            "required": ["name"]
        }
    )

    registry.register(
        name="update_project",
        fn=update_project,
        description="Overwrite a project's project.md with updated content. Use after significant work to keep the handoff current.",
        parameters={
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "Project name."},
                "content": {"type": "string", "description": "Full updated markdown content for project.md."}
            },
            "required": ["name", "content"]
        }
    )

    registry.register(
        name="refresh_codebase_index",
        fn=refresh_codebase_index,
        description="Walk a project's root directory and write a file-level index to codebase.md.",
        parameters={
            "type": "object",
            "properties": {
                "name":         {"type": "string", "description": "Project name."},
                "root_path":    {"type": "string", "description": "Root directory to index."},
                "extensions":   {"type": "array",  "items": {"type": "string"}, "description": "Optional list of file extensions to include e.g. ['.py', '.md']. Default: all files."},
                "exclude_dirs": {"type": "array",  "items": {"type": "string"}, "description": "Optional list of directory names to skip. Defaults to common noise dirs."}
            },
            "required": ["name", "root_path"]
        }
    )

    registry.register(
        name="load_codebase",
        fn=load_codebase,
        description="Load a project's codebase file index.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name."}
            },
            "required": ["name"]
        }
    )

    registry.register(
        name="link_chat",
        fn=link_chat,
        description="Link the current chat session to a project with a one-line summary.",
        parameters={
            "type": "object",
            "properties": {
                "name":    {"type": "string",  "description": "Project name."},
                "chat_id": {"type": "integer", "description": "Chat ID from the current session."},
                "summary": {"type": "string",  "description": "One-line summary of what this chat covered."}
            },
            "required": ["name", "chat_id", "summary"]
        }
    )

    registry.register(
        name="get_project_chats",
        fn=get_project_chats,
        description="List all chat sessions linked to a project.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name."}
            },
            "required": ["name"]
        }
    )