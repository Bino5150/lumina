"""
Projects Tools — lightweight project workspace management.
"""

import os
import json
import config

from core.project_context import (
    BindingNotFound,
    BindingRootInvalid,
    MalformedBinding,
    ProjectBindingError,
    load_project_binding,
    save_project_binding,
    validate_project_name,
)

PROJECTS_DIR = os.path.join(config.BASE_DIR, "projects")
PROJECTLIST  = os.path.join(PROJECTS_DIR, "projectlist.md")

# FE-13: chats.json is the only piece of projects/ that was ever gitignored
# personal state -- project.md/codebase.md/projectlist.md stay tracked in
# the repo on purpose (shareable project journals, same category as the
# tracked skills/*.md files). Only the chat-linkage file moves.
#
# CODING-02B-A: the machine-local project->root *binding* (binding.json,
# core/project_context.py) is a second, independent piece of personal state
# living alongside chats.json in this same DATA_DIR directory -- for the
# same reason chats.json moved out in FE-13. Tracked project.md/
# projectlist.md now hold identity/description/notes only; they are never
# read as an execution-root source (see CODING-02A's source-vet: the
# tracked **Root:** line was already proven stale/wrong on both the release
# and dev-local trees).
PROJECT_CHATS_DIR = os.path.join(config.DATA_DIR, "projects")


def init_projects():
    """Create PROJECTS_DIR and projectlist.md if they don't exist."""
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


def register_projects_tools(registry, project_state=None):

    def create_project(name: str, description: str, root_path: str) -> str:
        """Create a new project workspace: tracked identity (project.md/
        codebase.md/projectlist.md — name, description, notes) plus a
        machine-local execution-root binding for root_path.

        CODING-02B-A: root_path is no longer written into any tracked file
        (see CODING-02A's source-vet — a tracked root is machine-specific
        and was already proven stale/contradictory across trees). It is
        still used, locally and only on this machine, to create the
        binding.json execution-root binding via save_project_binding().

        Validates name and root_path fully before writing anything. If the
        tracked identity is created but the binding fails (or vice versa),
        reports the true partial state rather than a blanket success —
        no rollback is attempted or claimed."""
        try:
            name = validate_project_name(name)
        except ValueError as e:
            return f"[Error: {e}]"

        canon_root = os.path.abspath(os.path.expanduser(root_path))
        if not os.path.isdir(canon_root):
            return f"[Error: root_path does not exist or is not a directory: {canon_root}]"

        project_dir = os.path.join(PROJECTS_DIR, name)
        try:
            os.makedirs(project_dir, exist_ok=True)
            # Create project.md — identity/description/notes only, no root.
            project_md = os.path.join(project_dir, "project.md")
            if not os.path.exists(project_md):
                with open(project_md, 'w', encoding='utf-8') as f:
                    f.write(f"# {name}\n\n**Description:** {description}\n\n## Status\n\n## Notes\n")
            # Create chats.json — lives in the data dir, not project_dir (FE-13)
            chats_json = os.path.join(PROJECT_CHATS_DIR, name, "chats.json")
            if not os.path.exists(chats_json):
                os.makedirs(os.path.dirname(chats_json), exist_ok=True)
                with open(chats_json, 'w', encoding='utf-8') as f:
                    json.dump([], f, indent=2)
            # Create codebase.md placeholder
            codebase_md = os.path.join(project_dir, "codebase.md")
            if not os.path.exists(codebase_md):
                with open(codebase_md, 'w', encoding='utf-8') as f:
                    f.write(f"# Codebase Index — {name}\n\n*Run refresh_codebase_index to populate.*\n")
            # Append to projectlist.md — no root here either.
            current = _read_projectlist()
            entry = f"- **{name}** — {description}\n"
            if entry.strip() not in current:
                with open(PROJECTLIST, 'a', encoding='utf-8') as f:
                    f.write(entry)
        except Exception as e:
            return f"[Error creating tracked project '{name}': {e}]"

        try:
            save_project_binding(name, canon_root)
        except Exception as e:
            return (
                f"[Project '{name}' created (tracked identity only) — local execution "
                f"root NOT configured: {e}. Use set_project_root to configure it.]"
            )

        return f"[Project created: {project_dir} | local execution root bound: {canon_root}]"

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
            # CODING-02B-A: no machine root line -- codebase.md is a tracked,
            # public artifact (see CODING-02A's finding that this line was
            # already leaking an absolute local path into it). root_path is
            # still used operationally, just above, to perform the walk.
            lines = [f"# Codebase Index — {name}\n"]
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

    def set_project_root(name: str, root_path: str) -> str:
        """Configure (or update) the machine-local execution-root binding
        for an existing tracked project. Pure configuration -- does NOT
        activate the project for this or any agent (see activate_project),
        and never touches tracked Markdown. Owner-only (see
        core/tool_profiles.py's OWNER_ONLY_TOOLS) -- this is persistent
        machine-local configuration, not routine coding context."""
        try:
            name = validate_project_name(name)
        except ValueError as e:
            return f"[Error: {e}]"

        project_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.exists(project_dir):
            return f"[Error: no project named '{name}'. Create it first with create_project.]"

        try:
            context = save_project_binding(name, root_path)
        except (ValueError, ProjectBindingError) as e:
            return f"[Error: {e}]"

        return (
            f"[Local execution root configured for '{name}': {context.root} "
            f"(not activated — use activate_project to select it)]"
        )

    def activate_project(name: str) -> str:
        """Make an existing tracked project's machine-local execution root
        this agent's active project -- affects only this LuminaAgent
        instance's ProjectContextState, never any other agent, never a
        process-global. Fails closed: an unknown project, or a project with
        no local binding, never guesses or falls back to a tracked
        **Root:** line (CODING-02A proved those are unreliable)."""
        try:
            name = validate_project_name(name)
        except ValueError as e:
            return f"[Error: {e}]"

        project_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.exists(project_dir):
            return f"[Error: no project named '{name}'. Check projectlist.]"

        if project_state is None:
            return "[Error: no project context available for this session]"

        try:
            context = load_project_binding(name)
        except BindingNotFound:
            return (
                f"[Error: project '{name}' has no local execution root configured "
                f"on this machine. Use set_project_root to configure it.]"
            )
        except (MalformedBinding, BindingRootInvalid) as e:
            return f"[Error: {e}]"

        project_state.set(context)
        return f"[Active project: {context.name} | root: {context.root}]"

    def get_active_project() -> str:
        """Read-only introspection -- never guesses; reports this agent's
        actual current ProjectContextState, truthfully, including 'none'."""
        context = project_state.get() if project_state is not None else None
        if context is None:
            return "[No active project]"
        return f"[Active project: {context.name} | root: {context.root}]"

    def clear_active_project() -> str:
        """Clears only this agent's active project. Project-aware tools
        (filesystem, edit_file, run_command) immediately revert to their
        legacy, pre-CODING-02B-A default behavior for this agent."""
        if project_state is not None:
            project_state.clear()
        return "[Active project cleared]"

    def link_chat(name: str, chat_id: int, summary: str) -> str:
        """Link a chat session to a project with a one-line summary."""
        project_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.exists(project_dir):
            return f"[Error: no project named '{name}'. Create it first.]"
        chats_path = os.path.join(PROJECT_CHATS_DIR, name, "chats.json")
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
            os.makedirs(os.path.dirname(chats_path), exist_ok=True)
            with open(chats_path, 'w', encoding='utf-8') as f:
                json.dump(chats, f, indent=2)
            return f"[Chat {chat_id} linked to project '{name}']"
        except Exception as e:
            return f"[Error linking chat to '{name}': {e}]"

    def get_project_chats(name: str) -> str:
        """List all chats linked to a project."""
        chats_path = os.path.join(PROJECT_CHATS_DIR, name, "chats.json")
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
                "name":        {"type": "string", "description": "Short project identifier: letters, digits, '-', '_' only, starting with a letter or digit. e.g. 'lumina-dev'"},
                "description": {"type": "string", "description": "One-line description of the project."},
                "root_path":   {"type": "string", "description": "Absolute path to the project's source root on this machine. e.g. '~/lumina'. Used only to create a machine-local execution-root binding — never written into tracked project files."}
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

    registry.register(
        name="set_project_root",
        fn=set_project_root,
        description="Configure (or update) the machine-local execution-root binding for an "
                     "existing project. Does not activate the project — use activate_project "
                     "for that. Owner-only.",
        parameters={
            "type": "object",
            "properties": {
                "name":      {"type": "string", "description": "Existing project name."},
                "root_path": {"type": "string", "description": "Absolute path to the project's source root on this machine."}
            },
            "required": ["name", "root_path"]
        }
    )

    registry.register(
        name="activate_project",
        fn=activate_project,
        description="Make an existing project's machine-local execution root this agent's "
                     "active project — relative filesystem/edit paths and run_command's default "
                     "working directory then default to it. Fails if the project is unknown or "
                     "has no local execution root configured on this machine.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name to activate."}
            },
            "required": ["name"]
        }
    )

    registry.register(
        name="get_active_project",
        fn=get_active_project,
        description="Report this agent's currently active project, if any.",
        parameters={"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        name="clear_active_project",
        fn=clear_active_project,
        description="Clear this agent's active project. Project-aware tools revert to their "
                     "legacy default path/cwd behavior.",
        parameters={"type": "object", "properties": {}, "required": []}
    )