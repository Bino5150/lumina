"""
MB-23 -- eval scratch-directory reset.

Split into its own tiny module, separate from run_eval.py, specifically so
it's unit-testable in isolation: run_eval.py's module-level code has real
side effects (setting os.environ["LUMINA_DATA_DIR"] before any project
import, per its own CRITICAL comment) that make importing the whole module
from inside a test process unsafe -- it would leak into the rest of the
test session's environment. This file defines a pure function with no
import-time side effects of its own.

Historical bug this fixes (LUMINA_HANDOFF_S46.md): a stale
eval/_scratch_data/memory/prefs.json left over from an old S30 debugging
session silently contaminated every subsequent eval run -- including two
full runs that were already being cited as real results -- until someone
noticed "Bino" appearing unprompted in a T03 response mid-run. A one-time
`rm -rf` was applied that session as a stopgap ("this is the actual bug --
the manual rm was a one-time patch, not a fix"). This makes the guarantee
structural: every run starts from a genuinely empty directory by
construction, not by assumption that a prior run cleaned up after itself.
"""
import os
import shutil


def reset_scratch_dir(path: str) -> None:
    """Wipe `path` if it exists, then recreate it empty.

    Must be called before LUMINA_DATA_DIR is read by anything downstream
    (config.py resolves it at import time) and before any project import --
    same ordering constraint run_eval.py already documents for setting the
    env var itself.
    """
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
