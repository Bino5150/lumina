"""
Deterministic pre-execution guard for model shell/process adapters and trusted
internal argv launches.

Regex denylist, not an LLM judgment call — this exists specifically so a
hallucinating model or an injected instruction can't argue its way past it.
Not a hard security boundary against a determined adversary (regex is
beatable by encoding/indirection); it's a backstop against the "oops" case
and unsophisticated injection, same trust tier as the rest of terminal.py's
posture. No bypass parameter, by design — see MB-33.
"""

from collections.abc import Sequence
import re
import shlex

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

    # CODING-06R.1: the regexes above catch a contiguous "-f"/"-D"/"--force"
    # token but miss a combined short-flag spelling like "-uf" (set-upstream
    # + force) or "-fD" (force + delete) -- confirmed 06R bypass. Reuses the
    # exact same combined/reordered-short-flag-aware parser check_argv
    # already relies on, applied to each Git invocation found after a
    # best-effort shell-string tokenization, rather than widening the regex
    # with more naive substring matching.
    reason = _git_command_reason(command)
    if reason:
        return reason

    # CODING-06R.1: none of the patterns above have any Windows-native
    # coverage -- on a Windows host this function provides zero protection
    # against rd/rmdir, del/erase, format, or PowerShell Remove-Item.
    reason = _windows_destructive_reason(command)
    if reason:
        return reason

    return None


def check_model_command(command: str) -> str | None:
    """Model-originated shell guard with dedicated-worktree enforcement.

    ``check_command`` remains the shared catastrophic backstop used by trusted
    internal argv operations.  The extra rule here is intentionally applied
    only by the raw model adapters (run_command/start_process), so A2's exact,
    manager-owned ``git worktree remove`` argv is not given a generic bypass
    and is not accidentally blocked by a global deny rule.
    """
    reason = check_command(command)
    if reason:
        return reason
    return _git_command_reason(command, reason_fn=_git_model_reason)


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


# CODING-06R.3 ABBREVIATION CORRECTIVE: Git's long-option parser accepts any
# unambiguous prefix of a long option name (e.g. `--del` for `--delete`), and
# which prefixes are unambiguous is subcommand-specific -- it depends on what
# *other* long options exist on that same subcommand and would collide. These
# sets are not a generic "starts-with" rule; each is the literal, finite,
# empirically-verified list of spellings Git itself accepts as that option on
# that subcommand (git version 2.43.0, live-tested against a disposable local
# repo). A prefix shorter than every entry below is ambiguous and Git itself
# rejects it (e.g. `git branch --for` errors "ambiguous option: for (could be
# --force or --format)"; `git push --forc` similarly errors against
# --force-with-lease/--force-if-includes) -- such a spelling can never reach
# a shell as a working destructive command, so it is deliberately absent from
# every set here, per the "ambiguous-and-rejected need not be classified as
# catastrophic" scoping for this corrective.
#
#   git branch --delete: --d --de --del --dele --delet --delete (all unique;
#     no other branch long option starts with "d")
#   git branch --force:  --forc --force (unique only from "forc" on; "--f"/
#     "--fo"/"--for" collide with --format and are rejected by Git)
#   git reset --hard:    --h --ha --har --hard (all unique; no other reset
#     long option starts with "h", and single-dash "-h" is a distinct,
#     separately-parsed help flag that this set does not include)
#   git clean --force:   --f --fo --for --forc --force (all unique; no other
#     clean long option starts with "f")
#   git push --force: audited, no corrective needed -- Git requires the
#     exact, complete spelling "--force" here (an exact match short-circuits
#     ambiguity checking); every strict abbreviation ("--forc" and shorter)
#     collides with --force-with-lease/--force-if-includes and Git rejects it
#     as ambiguous. The pre-existing exact "--force" / "--force="- /
#     "--force-with-lease"-prefixed checks in _git_catastrophic_reason
#     already cover every spelling Git accepts.
#
# Residual risk, explicitly: this table is pinned to Git 2.43.0's option set.
# A future Git version that removes a colliding option (e.g. drops
# --format from git-branch) could make a currently-ambiguous prefix
# unambiguous, and this table would then under-detect it until re-verified.
# That is the accepted cost of the "no invoking Git itself to decide safety"
# design constraint -- the alternative (shelling out to Git to resolve
# abbreviations) would make the guardrail's own safety decision
# non-deterministic and dependent on the host's Git binary.
_GIT_BRANCH_DELETE_LONG_ABBREVS = frozenset({
    "--d", "--de", "--del", "--dele", "--delet", "--delete",
})
_GIT_BRANCH_FORCE_LONG_ABBREVS = frozenset({"--forc", "--force"})
_GIT_RESET_HARD_LONG_ABBREVS = frozenset({"--h", "--ha", "--har", "--hard"})
_GIT_CLEAN_FORCE_LONG_ABBREVS = frozenset({
    "--f", "--fo", "--for", "--forc", "--force",
})


def _branch_force_delete_requested(subargs) -> bool:
    """CODING-06R.3: delete-intent and force-intent are independent axes on
    `git branch` -- `-D` is shorthand for `-d -f` (equivalently
    `--delete --force`), and Git treats any combination carrying both as a
    forced, unmerged-safe delete regardless of how the two are spelled or
    ordered (confirmed live: `git branch -d -f <unmerged>` deletes it exactly
    like `-D` does). Deciding on delete_requested AND force_requested,
    computed independently, is what closes the R.2-confirmed `-d -f`/`-fd`/
    `--delete --force` bypass without regressing a plain `-d`/`--delete`
    (safe, merge-checked) or a plain `-f` (force branch reset, not a delete)
    to blocked. Reuses _has_short_flag exactly as the other subcommands
    below do -- no second parser, no substring matching against arbitrary
    argument text (a branch NAMED "force" or "delete" is a positional
    argument, never examined by _has_short_flag or the exact-membership
    checks here, abbreviation-aware or not).

    CODING-06R.3 ABBREVIATION CORRECTIVE: the exact "--delete"/"--force"
    membership checks alone missed a Git-accepted abbreviation like
    `--del --forc` -- confirmed live to force-delete an unmerged branch
    exactly like `-D` does. Membership against the finite, empirically
    verified _GIT_BRANCH_DELETE_LONG_ABBREVS / _GIT_BRANCH_FORCE_LONG_ABBREVS
    sets above closes that without matching any prefix Git itself would
    reject as ambiguous.
    """
    delete_requested = (
        _has_short_flag(subargs, "D")
        or _has_short_flag(subargs, "d")
        or any(arg in _GIT_BRANCH_DELETE_LONG_ABBREVS for arg in subargs)
    )
    force_requested = (
        _has_short_flag(subargs, "D")
        or _has_short_flag(subargs, "f")
        or any(arg in _GIT_BRANCH_FORCE_LONG_ABBREVS for arg in subargs)
    )
    return delete_requested and force_requested


def _git_catastrophic_reason(argv) -> str | None:
    """Shared by check_argv (direct) and check_command (after best-effort
    shell-string tokenization, see _git_command_reason below) so a Git
    catastrophic form is decided identically by both guardrail entry
    points instead of drifting into two separately-maintained rules.
    argv[0] must be the git executable itself; argv[1:] are its own args.
    """
    subcommand, subargs = _git_subcommand(argv)
    if subcommand == "push" and (
        any(arg == "--force" or arg.startswith("--force=")
            or arg.startswith("--force-with-lease") for arg in subargs)
        or _has_short_flag(subargs, "f")
    ):
        return "force push"
    if subcommand == "reset" and any(
        arg in _GIT_RESET_HARD_LONG_ABBREVS or arg.startswith("--hard=")
        for arg in subargs
    ):
        return "git reset --hard (discards uncommitted work)"
    if subcommand == "clean" and (
        any(arg in _GIT_CLEAN_FORCE_LONG_ABBREVS for arg in subargs)
        or _has_short_flag(subargs, "f")
    ):
        return "git clean with force flag (deletes untracked files)"
    if subcommand == "branch" and _branch_force_delete_requested(subargs):
        return "git branch -D (force delete, discards unmerged commits)"
    return None


def _git_model_worktree_reason(argv) -> str | None:
    """Reject raw model access to destructive worktree lifecycle commands.

    Git 2.43.0 accepts only the exact nested subcommands ``remove`` and
    ``prune`` (the strict prefixes are rejected), so exact membership mirrors
    live Git semantics without a broad substring rule.
    """
    subcommand, subargs = _git_subcommand(argv)
    if subcommand != "worktree":
        return None
    for arg in subargs:
        lowered = arg.casefold()
        if lowered in {"-h", "--help", "--version"}:
            return None
        if arg == "--" or arg.startswith("-"):
            continue
        if lowered == "remove":
            return "raw git worktree remove bypasses Lumina's managed removal boundary"
        if lowered == "prune":
            return "raw git worktree prune bypasses Lumina's managed removal boundary"
        return None
    return None


def _git_model_reason(argv) -> str | None:
    return _git_catastrophic_reason(argv) or _git_model_worktree_reason(argv)


# CODING-06R.1: splits a raw shell string on chaining/pipe operators (&&,
# ||, ;, |, &, newline) into plain-text segments -- not a real shell
# parser, deliberately. A segment boundary inside a quoted string (e.g. a
# commit message containing a literal ";") is not recognized; that can
# only ever cause a missed split (segments stay too large), never a false
# one, which matches this module's existing backstop-not-sandbox posture.
_SHELL_CHAIN_SPLIT_RE = re.compile(r"&&|\|\||[;|&\n]")


def _shell_chain_segments(command: str):
    return [segment for segment in _SHELL_CHAIN_SPLIT_RE.split(command) if segment.strip()]


def _git_command_reason(command: str, reason_fn=_git_catastrophic_reason) -> str | None:
    """check_command's Git structural pass: find every "git" token inside
    each chain segment (git need not be the segment's first word --
    "sudo git push -f" is real usage) and decide it exactly like
    check_argv does. shlex, not str.split(), so a quoted commit message
    containing "-f" or "-D" is one token, not several -- an unparsable
    segment (unbalanced quotes) is skipped rather than raising, since this
    remains a backstop and the pre-existing _DENYLIST regexes above already
    ran unconditionally regardless of what happens here.
    """
    for segment in _shell_chain_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if _executable_basename(token) == "git":
                reason = reason_fn(tokens[index:])
                if reason:
                    return reason
    return None


# CODING-06R.1: narrow Windows-native catastrophic-command backstop, same
# granularity as the existing POSIX rm/dd/mkfs entries above (root or
# wildcard-broad targets only -- an ordinary `rd /s build\output` or
# `del report.txt` is untouched, per module docstring: backstop, not a
# sandbox). Reached both directly (a raw command string spawned on a
# Windows host by core/process_control.py's _spawn_windows) and via
# check_argv's _inline_shell_command() delegation for `cmd /c ...` /
# `powershell|pwsh -Command ...`, since both route the extracted inline
# text through check_command().
_WINDOWS_DESTRUCTIVE_EXECUTABLES = {"rd", "rmdir", "del", "erase", "format", "remove-item", "ri"}


def _windows_drive_target(token: str) -> bool:
    stripped = token.strip("\"'")
    return bool(re.fullmatch(r"[A-Za-z]:\\?", stripped))


def _windows_broad_target(token: str) -> bool:
    stripped = token.strip("\"'")
    if _is_root_target(stripped):
        return True
    return stripped.endswith("*")


def _windows_destructive_reason_for(executable: str, args) -> str | None:
    flags = [a for a in args if a.startswith(("/", "-"))]
    targets = [a for a in args if not a.startswith(("/", "-"))]
    flags_casefold = {a.casefold() for a in flags}

    if executable in {"rd", "rmdir"}:
        # /S is structurally required here -- unlike `del`, Windows `rd`
        # without it can only ever remove an already-empty directory, so a
        # bare `rd C:\` targeting a populated root is not itself
        # catastrophic; it just fails.
        if "/s" in flags_casefold and any(_windows_broad_target(t) for t in targets):
            return "Windows recursive directory delete of a root or wildcard-broad target"

    if executable in {"del", "erase"}:
        # Mirrors the POSIX rm entries' looser gate: any one of force,
        # recurse, or quiet (which suppresses the wildcard-delete
        # confirmation prompt, effectively making it non-interactive)
        # alongside a root/wildcard target is treated as destructive intent.
        if flags_casefold & {"/f", "/s", "/q"} and any(_windows_broad_target(t) for t in targets):
            return "Windows force/recursive file delete of a root or wildcard-broad target"

    if executable == "format" and any(_windows_drive_target(t) for t in targets):
        return "Windows drive format"

    if executable in {"remove-item", "ri"}:
        if "-recurse" in flags_casefold and "-force" in flags_casefold and any(
            _windows_broad_target(t) for t in targets
        ):
            return "PowerShell Remove-Item -Recurse -Force on a root or wildcard-broad target"

    return None


def _windows_destructive_reason(command: str) -> str | None:
    """Only the leading word of each chain segment is treated as the
    executable -- unlike Git, there's no common Windows command-line idiom
    that prefixes rd/del/format/Remove-Item the way `sudo` prefixes a POSIX
    command, so scanning every token position would only add false-positive
    surface (e.g. the bare word "format" appearing as an unrelated argument
    elsewhere in the line) without catching any real additional case.
    Plain whitespace split, not shlex -- shlex's POSIX backslash-escaping
    would mangle a literal Windows path (C:\\Users\\... -> CUsers...).
    """
    for segment in _shell_chain_segments(command):
        tokens = segment.split()
        if not tokens:
            continue
        executable = _executable_basename(tokens[0])
        if executable in _WINDOWS_DESTRUCTIVE_EXECUTABLES:
            reason = _windows_destructive_reason_for(executable, tokens[1:])
            if reason:
                return reason
    return None


def _sudo_effective_argv(argv):
    """Walk past sudo's own recognized options/env-assignment prefix and
    return the remaining argv starting at the real command's own argv[0]
    (i.e. an argv shaped exactly like check_argv's own top-level input,
    suitable for redispatch into another executable's catastrophic-form
    check) -- or None if the walk never reaches a bare command, using only
    the sudo grammar this parser already understood before CODING-06R.3.
    """
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
    return args[index:] if index < len(args) else None


def _sudo_command(argv) -> str | None:
    sub_argv = _sudo_effective_argv(argv)
    return _executable_basename(sub_argv[0]) if sub_argv else None


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

    if executable == "sudo":
        # CODING-06R.3: redispatch sudo's own effective sub-argv through
        # the normal per-executable checks below, narrowly, for the two
        # executables this module already has catastrophic-form detection
        # for (rm, git) -- not a generalized privilege-escalation policy,
        # and not a new sudo grammar: _sudo_effective_argv() is the exact
        # option walk this module already relied on for "sudo rm", now
        # also handing back the real command's own argv[0:] so a wrapped
        # "git" gets the identical _git_catastrophic_reason() decision a
        # bare (non-sudo) argv invocation would get. Closes the R.2-found
        # asymmetry where check_command("sudo git push -f ...") blocked
        # (its shlex scan finds "git" at any token position) while
        # check_argv(["sudo", "git", "push", "-f", ...]) did not (this
        # function never looked past "sudo" for anything but "rm").
        sub_argv = _sudo_effective_argv(argv)
        if sub_argv:
            sub_executable = _executable_basename(sub_argv[0])
            if sub_executable == "rm":
                return "sudo rm"
            if sub_executable == "git":
                reason = _git_catastrophic_reason(sub_argv)
                if reason:
                    return reason

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
        reason = _git_catastrophic_reason(argv)
        if reason:
            return reason

    if executable == "gh":
        lowered = [arg.casefold() for arg in args]
        if any(
            lowered[index:index + 2] == ["repo", "delete"]
            for index in range(max(0, len(lowered) - 1))
        ):
            return "gh repo delete"

    return None
