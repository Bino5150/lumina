"""
core/project_context.py — per-agent active-project state and the
machine-local project -> root binding store.

CODING-02B-A: Mode A (ergonomic path/cwd defaulting) only — not filesystem
confinement or authorization. See CODING-02A's source-vet for why:
  - execution roots cannot live in tracked projects/*.md (proven stale and
    contradictory there on both the release and dev-local trees);
  - they must instead be machine-local, DATA_DIR-scoped state;
  - active-project selection must be owned per-LuminaAgent-instance, never a
    process-global (config.ACTIVE_PROJECT, CURRENT_PROJECT, os.chdir(), ...).

Binding storage lives at DATA_DIR/projects/<name>/binding.json, a sibling of
the FE-13 chats.json store (tools/projects.py's PROJECT_CHATS_DIR) -- same
directory, same "machine-local, not tracked" precedent, different file.
"""
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import config

# Module-level constant, not a function call -- so tests can monkeypatch it
# directly, same convention as core/persistence.py's PREFS_PATH and
# core/secrets.py's SECRETS_PATH.
PROJECT_BINDINGS_DIR = os.path.join(config.DATA_DIR, "projects")

_BINDING_FILENAME = "binding.json"

# Deliberately conservative but wide enough to preserve every existing
# tracked project identifier (lumina, lumina-dev, oracle, motunes): letters,
# digits, '-', '_', must start with a letter or digit. Excludes path
# separators, '.', '..', '~', and leading/trailing whitespace by
# construction -- there is no character class here that can produce any of
# those, so no separate check is needed once this matches.
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ProjectBindingError(Exception):
    """Base class for a genuine binding problem — never raised for the
    ordinary 'no binding exists yet' case, see BindingNotFound."""


class BindingNotFound(ProjectBindingError):
    """No binding.json exists for this project name on this machine."""


class MalformedBinding(ProjectBindingError):
    """binding.json exists but doesn't parse as the expected shape."""


class BindingRootInvalid(ProjectBindingError):
    """binding.json parses fine, but its 'root' no longer exists or is not
    a directory — a stale binding must not silently activate."""


@dataclass(frozen=True)
class ProjectContext:
    """Immutable. Both fields are already validated/canonicalized by the
    time one of these is constructed — nothing downstream re-validates."""
    name: str
    root: str


class ProjectContextState:
    """Mutable holder owned by exactly one LuminaAgent. The ProjectContext
    it holds is itself immutable — this class only ever swaps which one (or
    None) is currently held, never mutates one in place. Two agents must
    each get their own ProjectContextState instance; nothing here is
    process-global."""

    def __init__(self, initial: Optional[ProjectContext] = None):
        self._context = initial

    def get(self) -> Optional[ProjectContext]:
        return self._context

    def set(self, context: Optional[ProjectContext]):
        if context is not None and not isinstance(context, ProjectContext):
            raise TypeError("ProjectContextState.set() requires a ProjectContext or None")
        self._context = context

    def clear(self):
        self._context = None

    def snapshot(self) -> Optional[ProjectContext]:
        """Returns the currently-held (immutable) ProjectContext, same value
        get() would return. Named separately so CODING-02B-B's
        capture-at-dispatch code (spawn_subagent/submit_task) can express
        intent — 'the value as of right now, to bake into a child' — at the
        exact call site, rather than reusing get()'s more general name."""
        return self._context


def validate_project_name(name: str) -> str:
    """Returns name unchanged (case preserved) if valid, else raises
    ValueError with a human-readable reason. Centralizes every check so a
    project name can't reach a filesystem join un-validated — see
    CODING-02A's finding that os.path.join(PROJECTS_DIR, name) was
    previously unguarded against traversal and absolute-path override."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("project name must not be empty or whitespace-only")
    if name != name.strip():
        raise ValueError(f"project name must not have leading/trailing whitespace: {name!r}")
    if os.path.isabs(name):
        raise ValueError(f"project name must not be an absolute path: {name!r}")
    if name.startswith("~"):
        raise ValueError(f"project name must not start with '~': {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"project name must not contain a path separator: {name!r}")
    if name in (".", ".."):
        raise ValueError(f"project name must not be '.' or '..': {name!r}")
    if not _VALID_NAME_RE.match(name):
        raise ValueError(
            "project name must start with a letter or digit and contain only "
            f"letters, digits, '-', '_': {name!r}"
        )
    return name


def _check_no_case_collision(name: str):
    """'Foo' and 'foo' must not silently become two different machine-local
    bindings whose identity depends on the underlying filesystem's case
    sensitivity. Only checked at save time — by construction, whatever
    already exists on disk was already collision-checked when it was
    saved."""
    if not os.path.isdir(PROJECT_BINDINGS_DIR):
        return
    target_key = name.casefold()
    for existing in os.listdir(PROJECT_BINDINGS_DIR):
        if existing == name:
            continue  # re-saving this same project's own binding
        if existing.casefold() == target_key:
            raise ValueError(
                f"project name {name!r} collides case-insensitively with "
                f"existing local binding {existing!r}"
            )


def load_project_binding(name: str) -> ProjectContext:
    """Raises BindingNotFound / MalformedBinding / BindingRootInvalid —
    never returns None, so callers can't accidentally treat 'no binding' as
    a falsy-but-silent success. name is validated as a side effect (raises
    ValueError if invalid, same as validate_project_name)."""
    name = validate_project_name(name)
    binding_path = os.path.join(PROJECT_BINDINGS_DIR, name, _BINDING_FILENAME)

    if not os.path.exists(binding_path):
        raise BindingNotFound(
            f"no local execution root configured for project {name!r} on this machine"
        )

    try:
        with open(binding_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MalformedBinding(f"binding file for project {name!r} is unreadable/malformed: {e}")

    if not isinstance(data, dict) or not isinstance(data.get("root"), str) or not data.get("root"):
        raise MalformedBinding(f"binding file for project {name!r} is malformed: missing/invalid 'root'")

    root = data["root"]
    if not os.path.isdir(root):
        raise BindingRootInvalid(
            f"project {name!r}'s bound root no longer exists or is not a directory: {root}"
        )

    return ProjectContext(name=name, root=root)


def save_project_binding(name: str, root: str) -> ProjectContext:
    """Validates + canonicalizes both name and root, atomically writes
    binding.json, and returns the resulting ProjectContext. Pure
    machine-local configuration write — does NOT activate anything for any
    agent (see tools/projects.py's set_project_root vs activate_project)."""
    name = validate_project_name(name)
    _check_no_case_collision(name)

    canon_root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(canon_root):
        raise BindingRootInvalid(f"root does not exist or is not a directory: {canon_root}")

    project_dir = os.path.join(PROJECT_BINDINGS_DIR, name)
    os.makedirs(project_dir, exist_ok=True)
    binding_path = os.path.join(project_dir, _BINDING_FILENAME)
    tmp_path = binding_path + ".tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"root": canon_root}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, binding_path)
    except OSError as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise ProjectBindingError(f"failed to save binding for project {name!r}: {e}")

    return ProjectContext(name=name, root=canon_root)


def resolve_project_path(path: str, project_state: Optional[ProjectContextState]) -> str:
    """Turns a possibly-relative path into a concrete path, honoring:
      1. '~'-prefixed -> expanduser, explicit user intent, never
         project-relative.
      2. already absolute -> honored as-is, explicit caller intent wins.
      3. relative + an active project on project_state -> lexically joined
         against that project's root (abspath(join(root, path))) — collapses
         '.'/'..'/redundant separators, never touches symlink components.
      4. relative + no active project -> returned via expanduser() only,
         byte-identical to every pre-CODING-02B-A call site's own
         `os.path.expanduser(path)` first line — legacy behavior preserved
         exactly, including tools (filesystem.py) that never called
         abspath() on this value themselves.

    project_state: a ProjectContextState, or None — callers/tests that
    predate CODING-02B-A can omit it entirely and get exactly today's
    behavior.

    Deliberately lexical only — os.path.abspath()/os.path.join(), never
    os.path.realpath(). Mode A is defaulting, not confinement: a relative
    '../other/file' may lexically escape the active root, and that's by
    design (CODING-02A section 8/9). Never resolving symlinks here also
    means this can run safely ahead of edit_file's own deliberate symlink
    rejection (CODING-01's kernel) without silently defeating it.
    """
    if path.startswith("~"):
        return os.path.expanduser(path)

    if os.path.isabs(path):
        return path

    context = project_state.get() if project_state is not None else None
    if context is not None:
        return os.path.abspath(os.path.join(context.root, path))

    return os.path.expanduser(path)
