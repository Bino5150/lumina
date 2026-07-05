"""
Terminal Tool — execute shell commands, return output.

F-37 (terminal half) hardening: subprocess.run's timeout only kills the
immediate shell process, not any children the command spawned (a pipeline,
a background job, `command &`) — those survived the old timeout and kept
running after the tool call "finished." Switched to Popen + start_new_session
so a timeout kills the whole process group. Also added a process-count
rlimit as fork-bomb protection.

Deliberately NOT doing the same hardening as sandbox.py here (no env scrub,
no memory cap, no cwd lockdown): this tool's entire purpose is running real
shell commands with real project access and a real environment (conda,
git, pip) — sandboxing that away would break the tool, not secure it. The
actual risk — owner-tier prompt injection reaching this tool — is the same
chain F-37 closes on run_python/create_tool; "it's just a terminal" isn't
a gap further sandboxing here would meaningfully close.

CORRECTION (post-deploy, S40 live test): the first version of this fix used
preexec_fn to set the rlimit. Python's own docs explicitly warn preexec_fn
is unsafe in a multithreaded process — fork() only clones the calling
thread, so if another thread holds a libc-level lock (malloc, NSS, dlopen)
at that instant, the child can deadlock on it forever. Lumina is a Qt app
with multiple threads, so this wasn't theoretical — it hung run_command
for real on first live use. Specifying preexec_fn also forces CPython's
subprocess module off its safer posix_spawn fast path and onto raw
fork()+exec(), which is the exact path the threading warning is about.
Fixed by setting the rlimit via a shell `ulimit` builtin instead — that
runs inside the already-exec'd shell, so no Python code ever runs in the
fork-to-exec gap.
"""

import subprocess
import os
import signal

MAX_PROCESSES = 128  # fork-bomb ceiling, not a workflow limit — real builds/test
                      # suites need headroom, this just stops runaway forking


def register_terminal_tools(registry):

    def run_command(command: str, cwd: str = None, timeout: int = 30) -> str:
        """Execute a shell command and return stdout/stderr."""
        cwd = os.path.expanduser(cwd) if cwd else os.path.expanduser("~")

        # ulimit is a shell builtin — it applies to this shell and everything
        # it subsequently runs, with no Python code executing between fork
        # and exec (see CORRECTION above for why that matters).
        wrapped_command = f"ulimit -u {MAX_PROCESSES}; {command}"

        try:
            proc = subprocess.Popen(
                wrapped_command,
                shell=True,
                executable="/bin/bash",   # /bin/sh may be dash, whose ulimit
                                           # builtin doesn't reliably support
                                           # every flag used here
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,   # own process group -> killpg reaches children too
            )
        except Exception as e:
            return f"[Error: {e}]"

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            finally:
                try:
                    proc.kill()   # fallback: guarantees the direct child dies even
                except OSError:   # if the group-kill couldn't reach it for any reason
                    pass
            proc.communicate()
            return f"[Error: command timed out after {timeout}s — process group killed]"
        except Exception as e:
            return f"[Error: {e}]"

        parts = []
        if stdout and stdout.strip():
            parts.append(f"[stdout]\n{stdout.strip()}")
        if stderr and stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        if returncode != 0:
            parts.append(f"[exit code: {returncode}]")
        return "\n".join(parts) if parts else "[No output]"

    registry.register(
        name="run_command",
        fn=run_command,
        description="Execute a shell command and return stdout and stderr.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "cwd": {"type": "string", "description": "Working directory. Default home dir."},
                "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."}
            },
            "required": ["command"]
        }
    )

