"""
FE-13 — One-time, idempotent migration of personal/runtime state out of the
tracked repo tree and into a platformdirs-managed data directory.

Why: memory/lumina.db, memory/prefs.json, memory/tool_audit.log, approved
custom tools written straight into tools/, and the entire projects/ tree are
all gitignored today -- correctly -- but they sit one .gitignore regression
away from a public commit, and mixing runtime-generated content into the
tracked source tree is exactly what forced last session's manual archiving
of temporal_decay_engine.py off to the OG build. This moves all of it out
from under BASE_DIR entirely, so a fresh `git clone` never mixes personal or
dynamically-created content with tracked source again.

Safe to call unconditionally on every startup -- no-op after the first real
move. Never overwrites an existing file at the new location; if something's
already there, the legacy copy is left in place untouched rather than
clobbered, so a partial or interrupted migration is always safely re-runnable.

Deliberately does NOT import `config` or `tools.toolmaker` -- this module is
imported from the very top of config.py, before config.py has finished
defining its own constants, so any import that pulls config back in would be
circular. The tool-approval event-replay logic below is a intentional,
commented duplicate of toolmaker.py's _deletable_tool_names() for exactly
this reason.
"""
import os
import shutil
import json


# Names that went through the toolmaker approval pipeline at some point but
# have since become genuine, statically-imported package dependencies rather
# than purely dispatched tools -- relocating these out of tools/ would break
# a real `from tools.X import Y` elsewhere in the codebase at import time,
# not just remove a callable tool. Currently just temporal_decay.py, which
# tools/palace.py imports directly (`from tools.temporal_decay import
# decay_engine`) for L2 recency sorting -- found while building this
# migration, by checking for static imports before assuming "approved in
# the audit log" meant "safe to move."
NEVER_MIGRATE = {"temporal_decay"}


def _approved_tool_names(audit_log_path: str) -> set:
    """Same event-replay logic as tools/toolmaker.py's _deletable_tool_names().
    Duplicated here rather than imported -- see module docstring. Fail-closed:
    any read failure returns an empty set, same posture as the original."""
    approved = set()
    if not os.path.exists(audit_log_path):
        return approved
    try:
        with open(audit_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = entry.get("name")
                event = entry.get("event")
                if not name:
                    continue
                if event == "approved":
                    approved.add(name)
                elif event == "deleted":
                    approved.discard(name)
    except Exception:
        return set()
    return approved


def migrate_legacy_state(base_dir: str, data_dir: str) -> list[str]:
    """Moves legacy in-repo state to the platformdirs data dir. Returns the
    list of new-location paths actually moved (empty list = already migrated
    or nothing to migrate -- both are the normal steady state)."""
    moved = []

    # 1. memory/ -- db, prefs, tool audit log. Order matters: the audit log
    # has to land in its NEW location before step 2 reads it, since step 2
    # needs the post-move path to know which custom tools are approved.
    memory_moves = {
        os.path.join(base_dir, "memory", "lumina.db"):      os.path.join(data_dir, "memory", "lumina.db"),
        os.path.join(base_dir, "memory", "prefs.json"):     os.path.join(data_dir, "memory", "prefs.json"),
        os.path.join(base_dir, "memory", "tool_audit.log"): os.path.join(data_dir, "memory", "tool_audit.log"),
        # core/idempotency.py's ledger -- same class of runtime state as
        # lumina.db, found during a final path-reference sweep after the
        # rest of this migration was already built.
        os.path.join(base_dir, "memory", "ledger.db"):      os.path.join(data_dir, "memory", "ledger.db"),
    }
    for old, new in memory_moves.items():
        if os.path.exists(old) and not os.path.exists(new):
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.move(old, new)
            moved.append(new)

    # 2. Custom tools -- only ones that actually went through
    # create_tool() -> approve_pending_tool() (mirrors toolmaker.py's own
    # fail-closed gate), plus anything still sitting in _pending/ awaiting
    # review. Built-in shipped modules (palace.py, web.py, etc.) never match
    # an "approved" audit log entry, so they're never touched.
    audit_log_path = os.path.join(data_dir, "memory", "tool_audit.log")
    approved = _approved_tool_names(audit_log_path) - NEVER_MIGRATE
    legacy_tools_dir = os.path.join(base_dir, "tools")
    new_custom_dir = os.path.join(data_dir, "custom_tools")
    new_pending_dir = os.path.join(new_custom_dir, "_pending")

    for name in approved:
        old = os.path.join(legacy_tools_dir, f"{name}.py")
        new = os.path.join(new_custom_dir, f"{name}.py")
        if os.path.exists(old) and not os.path.exists(new):
            os.makedirs(new_custom_dir, exist_ok=True)
            shutil.move(old, new)
            moved.append(new)

    legacy_pending_dir = os.path.join(legacy_tools_dir, "_pending")
    if os.path.isdir(legacy_pending_dir):
        for fname in os.listdir(legacy_pending_dir):
            old = os.path.join(legacy_pending_dir, fname)
            new = os.path.join(new_pending_dir, fname)
            if os.path.isfile(old) and not os.path.exists(new):
                os.makedirs(new_pending_dir, exist_ok=True)
                shutil.move(old, new)
                moved.append(new)

    # 3. projects/*/chats.json ONLY -- project.md, codebase.md, and
    # projectlist.md stay tracked in the repo tree on purpose (confirmed
    # against the real .gitignore: it only ever excluded chats.json, not
    # the rest of projects/). Those are shareable project-journal docs,
    # same category as the tracked skills/*.md files -- moving the whole
    # tree would kill a real feature, not just fix a leak. Only the
    # per-project chat-linkage file was ever personal state.
    legacy_projects_dir = os.path.join(base_dir, "projects")
    new_projects_dir = os.path.join(data_dir, "projects")
    if os.path.isdir(legacy_projects_dir):
        for entry in os.listdir(legacy_projects_dir):
            old_chats = os.path.join(legacy_projects_dir, entry, "chats.json")
            new_chats = os.path.join(new_projects_dir, entry, "chats.json")
            if os.path.isfile(old_chats) and not os.path.exists(new_chats):
                os.makedirs(os.path.dirname(new_chats), exist_ok=True)
                shutil.move(old_chats, new_chats)
                moved.append(new_chats)

    if moved:
        print(f"[MIGRATE] Moved {len(moved)} legacy state path(s) to {data_dir}", flush=True)

    return moved
