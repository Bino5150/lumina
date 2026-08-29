"""
core/memory_backup.py -- Qt-independent implementation of the "Backup
Memory" action.

CI-CORRECTIVE (Flight Recorder backup test collection): this logic was
previously defined inside ui/settings/memory_tab.py. It never touched Qt
itself (pure os/zipfile/sqlite), but living inside the `ui.settings`
package meant importing it at all executed ui/settings/__init__.py as a
side effect of Python's package-import machinery -- and that file
eagerly imports every settings tab, starting with `.panel`'s
SettingsPanel, which imports PySide6 directly. The CI job deliberately
never installs PySide6 (see .github/workflows/tests.yml's own comment:
system Qt libs would make the "core-logic" tier slow and flaky) -- so
tests/test_memory_backup_flight_recorder.py's plain module-level
`from ui.settings.memory_tab import _build_memory_backup` failed
collection outright, not gracefully skipped.

Moving the actual implementation here (a core/ module, same tier as
core/flight_recorder.py, core/db.py, core/persistence.py -- all of which
this function already depends on) fixes the dependency boundary at its
source, so CI now genuinely exercises this logic (real coverage) instead
of needing a pytest.importorskip("PySide6") guard that would have only
skipped it. ui/settings/memory_tab.py still re-exports build_memory_backup
for its own UI call site and any other existing importer -- pure
extraction, no behavior change.
"""
import os
import zipfile


def build_memory_backup(data_dir: str, dest_path: str) -> None:
    """Checkpoint the WAL, then zip everything under data_dir into
    dest_path. Deliberately does NOT touch ~/.config/lumina/credentials.json
    — that lives outside config.DATA_DIR entirely, so this can't leak an
    API key even though it grabs everything else without exceptions.

    AGENT-FLIGHT-RECORDER-01A1 -- no separate backup button, per that
    mission's explicit requirement: the recorder's SQLite db already lives
    under DATA_DIR/telemetry/ (see core/flight_recorder.py), so the
    os.walk() below already sweeps it in with zero changes needed here.
    The only real addition is the second checkpoint call below, for the
    same reason the main db gets one two lines up -- an un-checkpointed
    WAL file would still be a coherent recover-from-crash state, but
    zipping a stale/un-flushed one is worse than a deliberate flush costs.
    Best-effort: a recorder that failed to init (or was never touched this
    process) has nothing to checkpoint, and checkpoint() itself never
    raises -- backup must not fail because telemetry happened to be
    unavailable."""
    conn = _db()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    try:
        from core import flight_recorder
        flight_recorder.checkpoint()
    except Exception:
        pass
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(data_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, data_dir)
                zf.write(fpath, arcname)


def _db():
    from core.db import connect
    return connect()
