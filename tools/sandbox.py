"""
Code Execution Sandbox — run Python snippets, return stdout/stderr.

F-37 fix: previously bare in-process `exec(compile(code, "<sandbox>", "exec"), {})`.
Empty globals still auto-injects __builtins__, so __import__/open/os/eval were all
reachable in the app's OWN interpreter — full access to config (API keys), prefs.json,
lumina.db, network. The `timeout` param wasn't even in the tool schema, so it was
unenforced and unreachable — an infinite loop hung the whole app with no ceiling.
Combined with create_tool (writes+hot-loads arbitrary Python, no review gate), this
closed a prompt-injection -> full-exec chain for any owner-tier session that reads
untrusted content (a document, a scraped page, a Palace entry poisoned earlier).

Fix: run in a genuinely separate OS process (not just a thread), with:
  - a wall-clock timeout that's actually enforced and actually reachable from the
    tool schema, and that kills the WHOLE process group on expiry (not just the
    immediate child — a snippet that forks or spawns children doesn't survive)
  - a pinned working directory (not $HOME, not the repo root)
  - a scrubbed environment (no inherited API keys/tokens/secrets)
  - resource limits (CPU seconds, address space, open files, process count) via
    resource.setrlimit in the child, so a fork-bomb or memory-bomb snippet can't
    take the whole machine down
  - `-I` isolated interpreter mode (ignores PYTHONPATH/PYTHONHOME, no user site-packages)

This is NOT a hard security boundary — it's still the same OS user, same filesystem
permissions, so a determined attacker with disk access isn't stopped. Real containment
would mean a container or VM. Given Lumina's actual threat model (owner-tier trust,
reached via prompt injection rather than a hostile local user), this closes the
cheapest and most severe gap: a single crafted string reaching full in-process exec.

CORRECTION (post-deploy, S40 live test): the first version of this fix used
preexec_fn to set the rlimits. Python's own docs explicitly warn preexec_fn is
unsafe in a multithreaded process — fork() only clones the calling thread, so if
another thread holds a libc-level lock (malloc, NSS, dlopen) at that instant, the
child can deadlock on it forever. Lumina is a Qt app with multiple threads, so
this wasn't theoretical — it hung on first live use. Specifying preexec_fn also
forces CPython's subprocess module off its safer posix_spawn fast path and onto
raw fork()+exec(), which is the exact path the threading warning is about. Fixed
by setting rlimits via shell `ulimit` builtins instead — those run inside the
already-exec'd shell, so no Python code ever runs in the fork-to-exec gap.
"""

import subprocess
import sys
import os
import signal
import shlex
import tempfile
import config

SANDBOX_CWD = os.path.join(config.BASE_DIR, "memory", "sandbox_tmp")
DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 30
MAX_CPU_SECONDS = 30
MAX_MEMORY_BYTES = 512 * 1024 * 1024   # 512MB address space
MAX_OPEN_FILES = 64
PROCESS_HEADROOM = 32   # FE-01: added to the CURRENT user's live process
                        # count at spawn time, not a fixed absolute cap.
                        # RLIMIT_NPROC caps the whole user's total process
                        # count, not "children of this sandboxed script" — a
                        # fixed MAX_PROCESSES=16 is already exceeded before
                        # the tool even runs on any real desktop (100-300+
                        # processes from a browser session alone), so
                        # sandboxed code could never spawn a subprocess at
                        # all. Racy (process count can shift between the
                        # check and the fork) but sufficient against an
                        # actual fork bomb, and never starts underwater.


def register_sandbox_tools(registry):

    def run_python(code: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """Execute a Python code snippet in an isolated subprocess and return stdout/stderr."""
        os.makedirs(SANDBOX_CWD, exist_ok=True)

        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, MAX_TIMEOUT))  # never unbounded, never absurd

        clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

        script_path = None
        proc = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", dir=SANDBOX_CWD, delete=False
            ) as f:
                f.write(code)
                script_path = f.name

            # ulimit builtins, then the real interpreter — no preexec_fn,
            # see CORRECTION above. Nothing user-controlled reaches this
            # shell string (the code itself lives in script_path, executed
            # by Python, not interpreted by the shell), but quoted anyway
            # as standard hygiene.
            wrapped_command = (
                f"ulimit -t {MAX_CPU_SECONDS}; "
                f"ulimit -v {MAX_MEMORY_BYTES // 1024}; "
                f"ulimit -n {MAX_OPEN_FILES}; "
                f'ulimit -u $(( $(ps -u "$(id -un)" --no-headers | wc -l) + {PROCESS_HEADROOM} )); '
                f"{shlex.quote(sys.executable)} -I {shlex.quote(script_path)}"
            )

            proc = subprocess.Popen(
                wrapped_command,
                shell=True,
                executable="/bin/bash",   # /bin/sh may be dash; not all of the
                                           # ulimit flags above are guaranteed there
                cwd=SANDBOX_CWD,
                env=clean_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,   # own process group -> killpg can reach children too
            )
            stdout, stderr = proc.communicate(timeout=timeout)
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
            stdout, stderr = proc.communicate()
            stderr = (stderr or "") + f"\n[Execution timed out after {timeout}s — process killed]"
        except Exception as e:
            stdout, stderr = "", f"[Sandbox error: {e}]"
        finally:
            if script_path:
                try:
                    os.remove(script_path)
                except OSError:
                    pass

        parts = []
        if stdout:
            parts.append(f"[stdout]\n{stdout}")
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if not parts:
            return "[No output]"
        return "\n".join(parts)

    registry.register(
        name="run_python",
        fn=run_python,
        description="Execute a Python code snippet in an isolated subprocess and return stdout and stderr.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute."},
                "timeout": {
                    "type": "integer",
                    "description": f"Max seconds before the process is killed (default {DEFAULT_TIMEOUT}, hard cap {MAX_TIMEOUT}).",
                },
            },
            "required": ["code"],
        },
    )
