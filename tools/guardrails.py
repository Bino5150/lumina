"""
Deterministic pre-execution guard for tools/terminal.py::run_command.

Regex denylist, not an LLM judgment call — this exists specifically so a
hallucinating model or an injected instruction can't argue its way past it.
Not a hard security boundary against a determined adversary (regex is
beatable by encoding/indirection); it's a backstop against the "oops" case
and unsophisticated injection, same trust tier as the rest of terminal.py's
posture. No bypass parameter, by design — see MB-33.
"""

import re

# Each pattern: (compiled regex, human-readable reason)
_DENYLIST = [
    # rm -rf /  or  rm -rf /*  (bare root, optionally followed by wildcard)
    (re.compile(r"rm\s+(-\w*[rf]\w*\s+)+/(\s|$|\*|['\"])"), "recursive/force delete of root"),
    # rm -rf /home/*, /var/*, /usr/* — any absolute path followed by a wildcard
    (re.compile(r"rm\s+(-\w*[rf]\w*\s+)+/[\w/]*\*"), "recursive/force delete of absolute path with wildcard"),
    (re.compile(r"rm\s+(-\w*[rf]\w*\s+)+~(\s|/|$|['\"])"), "recursive/force delete of home"),
    (re.compile(r"rm\s+(-\w*[rf]\w*\s+)+\*"), "recursive/force delete with wildcard"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), "dd writing directly to a device"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b"), "pipe remote content to shell"),
    (re.compile(r"\bsudo\s+rm\b"), "sudo rm"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/(\s|$)"), "recursive world-writable on root"),
    (re.compile(r"\bgit\s+push\b.*(--force\b|\s-f\b)"), "force push"),
    (re.compile(r">\s*/dev/sd[a-z]\b"), "raw write to a block device"),
    # MB-33 extension: git-destructive beyond force-push, and gh repo delete
    (re.compile(r"\bgit\s+reset\b.*--hard\b"), "git reset --hard (discards uncommitted work)"),
    (re.compile(r"\bgit\s+clean\b.*\s-\w*f\w*"), "git clean with force flag (deletes untracked files)"),
    (re.compile(r"\bgit\s+branch\b.*\s-D\b"), "git branch -D (force delete, discards unmerged commits)"),
    (re.compile(r"\bgh\s+repo\s+delete\b"), "gh repo delete"),
]


def check_command(command: str) -> str | None:
    """Returns a block reason if command matches the denylist, else None."""
    for pattern, reason in _DENYLIST:
        if pattern.search(command):
            return reason
    return None
