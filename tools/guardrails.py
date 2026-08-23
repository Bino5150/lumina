"""
Deterministic pre-execution guard for tools/terminal.py::run_command.

Regex denylist, not an LLM judgment call — this exists specifically so a
hallucinating model or an injected instruction can't argue its way past it.
Not a hard security boundary against a determined adversary (regex is
beatable by encoding/indirection); it's a backstop against the "oops" case
and unsophisticated injection, same trust tier as the rest of terminal.py's
posture. No bypass parameter, by design — see MB-33.
"""

from collections.abc import Sequence
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


def _argv_structure_error(argv) -> str | None:
    if isinstance(argv, (str, bytes, bytearray)) or not isinstance(argv, Sequence):
        return "argv must be a sequence of strings"
    if not argv:
        return "argv must not be empty"
    for index, value in enumerate(argv):
        if not isinstance(value, str):
            return f"argv[{index}] must be a string"
        if "\0" in value:
            return f"argv[{index}] must not contain NUL"
    if argv[0] == "":
        return "argv[0] must not be empty"
    return None


def _executable_basename(value: str) -> str:
    """Lexically normalize POSIX/Windows executable spellings.

    This deliberately performs no PATH lookup, filesystem access, symlink
    resolution, or cwd-relative interpretation.  Direct argv admission must be
    deterministic before process creation.
    """
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name[:-4] if name.endswith(".exe") else name


def _has_short_flag(args, flag: str) -> bool:
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and not arg.startswith("--") and flag in arg[1:]:
            return True
    return False


def _is_root_target(target: str) -> bool:
    # POSIX root and obvious lexical equivalents such as // and /./.
    if target.startswith("/") and all(part in ("", ".") for part in target.split("/")):
        return True

    # Windows drive/UNC roots.  This is lexical only; it does not resolve the
    # target or ask the host filesystem what it points at.
    normalized = target.replace("/", "\\")
    if re.fullmatch(r"[A-Za-z]:\\(?:\.\\?)*", normalized):
        return True
    if normalized.startswith("\\\\"):
        parts = [part for part in normalized.split("\\") if part and part != "."]
        return len(parts) == 2 and normalized.endswith("\\")
    return False


def _inline_shell_command(argv) -> str | None:
    executable = _executable_basename(argv[0])
    args = argv[1:]

    if executable in {"bash", "sh", "zsh", "dash", "fish"}:
        for index, arg in enumerate(args):
            if arg == "--":
                break
            is_inline_flag = (
                arg == "-c"
                or (arg.startswith("-") and not arg.startswith("--")
                    and "c" in arg[1:])
                or (executable == "fish" and arg == "--command")
            )
            if is_inline_flag:
                return args[index + 1] if index + 1 < len(args) else None
        return None

    if executable == "cmd":
        for index, arg in enumerate(args):
            if arg.casefold() == "/c":
                return args[index + 1] if index + 1 < len(args) else None
        return None

    if executable in {"powershell", "pwsh"}:
        for index, arg in enumerate(args):
            if arg.casefold() in {"-command", "/command", "-c"}:
                return args[index + 1] if index + 1 < len(args) else None
        return None

    return None


def _git_subcommand(argv):
    """Return (subcommand, remaining_args) after common Git globals."""
    args = argv[1:]
    options_with_value = {
        "-c", "-C", "--git-dir", "--work-tree", "--namespace",
        "--super-prefix", "--config-env",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in options_with_value:
            index += 2
            continue
        if any(arg.startswith(prefix) for prefix in (
            "--git-dir=", "--work-tree=", "--namespace=",
            "--super-prefix=", "--config-env=",
        )):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg.casefold(), args[index + 1:]
    if index < len(args):
        return args[index].casefold(), args[index + 1:]
    return None, []


def _sudo_command(argv) -> str | None:
    args = argv[1:]
    options_with_value = {
        "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
        "-C", "--close-from", "-R", "--chroot", "-T", "--command-timeout",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in options_with_value:
            index += 2
            continue
        if arg.startswith("--") and "=" in arg:
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if "=" in arg and not arg.startswith(("/", ".")):
            index += 1
            continue
        break
    return _executable_basename(args[index]) if index < len(args) else None


def check_argv(argv) -> str | None:
    """Return a block reason for catastrophic direct argv, else ``None``.

    Unlike :func:`check_command`, this function is not a shell parser and
    never reconstructs argv into shell text.  The sole bridge is a recognized
    shell interpreter's real inline-command argument, which is passed to
    ``check_command`` unchanged because that exact string will be interpreted
    as shell code.

    This is a catastrophic direct-command/shell-inline backstop, not a
    sandbox.  It does not attempt to prove arbitrary executables, scripts, or
    interpreter payloads such as ``python -c`` are safe.
    """
    structural_error = _argv_structure_error(argv)
    if structural_error:
        return structural_error

    inline_command = _inline_shell_command(argv)
    if inline_command is not None:
        return check_command(inline_command)

    executable = _executable_basename(argv[0])
    args = list(argv[1:])

    if executable == "sudo" and _sudo_command(argv) == "rm":
        return "sudo rm"

    if executable == "rm":
        recursive = "--recursive" in args or _has_short_flag(args, "r") or _has_short_flag(args, "R")
        force = "--force" in args or _has_short_flag(args, "f")
        if recursive and force:
            after_double_dash = False
            targets = []
            for arg in args:
                if not after_double_dash and arg == "--":
                    after_double_dash = True
                    continue
                if not after_double_dash and arg.startswith("-"):
                    continue
                targets.append(arg)
            if any(_is_root_target(target) for target in targets):
                return "recursive/force delete of root"

    if executable == "dd" and any(arg.startswith("of=/dev/") for arg in args):
        return "dd writing directly to a device"

    if executable == "mkfs" or executable.startswith("mkfs."):
        return "filesystem format"

    if executable == "chmod":
        recursive = "--recursive" in args or _has_short_flag(args, "R")
        operands = [arg for arg in args if arg == "--" or not arg.startswith("-")]
        operands = [arg for arg in operands if arg != "--"]
        if recursive and operands and operands[0] == "777" and any(
            _is_root_target(target) for target in operands[1:]
        ):
            return "recursive world-writable on root"

    if executable == "git":
        subcommand, subargs = _git_subcommand(argv)
        if subcommand == "push" and (
            any(arg == "--force" or arg.startswith("--force=")
                or arg.startswith("--force-with-lease") for arg in subargs)
            or _has_short_flag(subargs, "f")
        ):
            return "force push"
        if subcommand == "reset" and any(
            arg == "--hard" or arg.startswith("--hard=") for arg in subargs
        ):
            return "git reset --hard (discards uncommitted work)"
        if subcommand == "clean" and (
            "--force" in subargs or _has_short_flag(subargs, "f")
        ):
            return "git clean with force flag (deletes untracked files)"
        if subcommand == "branch" and _has_short_flag(subargs, "D"):
            return "git branch -D (force delete, discards unmerged commits)"

    if executable == "gh":
        lowered = [arg.casefold() for arg in args]
        if any(
            lowered[index:index + 2] == ["repo", "delete"]
            for index in range(max(0, len(lowered) - 1))
        ):
            return "gh repo delete"

    return None
