"""Upgrade launch selection; every HOME, state folder and process is synthetic."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path):
    env = dict(os.environ)
    env.pop("LUMINA_DATA_DIR", None)
    env["HOME"] = str(tmp_path / "home")
    env["USERPROFILE"] = env["HOME"]
    env["XDG_DATA_HOME"] = str(tmp_path / "native-data")
    env["LUMINA_SECRETS_PATH"] = str(tmp_path / "credentials.json")
    env["LUMINA_CHATGPT_AUTH_DIR"] = str(tmp_path / "auth")
    env["LUMINA_RUNTIME_DIR"] = str(tmp_path / "runtime")
    return env


def _shell(tmp_path, override=None):
    install = tmp_path / "installation with spaces"
    install.mkdir()
    shutil.copyfile(ROOT / "start_lumina.sh", install / "start_lumina.sh")
    if (ROOT / "data_directory.py").exists():
        shutil.copyfile(ROOT / "data_directory.py", install / "data_directory.py")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Intercept main.py and dev-only local-mode writes. The real selector and
    # prefs reader run in Python. Stub service commands too, so the preserved
    # dev launcher cannot start a backend or contact a live service.
    fake = bindir / "python"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json,os,subprocess,sys\n"
        "if os.path.basename(sys.argv[1]) == 'main.py':\n"
        " print(json.dumps({'data_dir':os.environ.get('LUMINA_DATA_DIR'), 'cwd':os.getcwd()}))\n"
        "elif sys.argv[1] == '-':\n"
        " sys.stdin.read()  # local-mode preference write is outside this selection probe\n"
        "else:\n"
        f" sys.exit(subprocess.call([{sys.executable!r},*sys.argv[1:]]))\n"
    )
    fake.chmod(0o700)
    for name in ("curl", "docker"):
        command = bindir / name
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o700)
    env = _env(tmp_path)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    if override is not None:
        env["LUMINA_DATA_DIR"] = override
    # These harmless JSON files also let the old launcher reach our probe.
    folders = [Path(env["HOME"]) / ".local/share/lumina-release",
               tmp_path / "owner's existing data", tmp_path / "relative-state"]
    if override and os.path.isabs(override):
        folders.append(Path(override))
    for folder in set(folders):
        (folder / "memory").mkdir(parents=True)
        (folder / "memory/prefs.json").write_text('{"tts_backend":"piper"}')
    result = subprocess.run(
        ["bash", str(install / "start_lumina.sh")], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=10,
    )
    return result, install


@pytest.mark.parametrize("name", ["owner's existing data", "data ending in newline\n"])
def test_shell_respects_explicit_existing_folder(tmp_path, name):
    selected = str(tmp_path / name)
    result, install = _shell(tmp_path, selected)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt == {"data_dir": selected, "cwd": str(install)}
    assert "Piper backend" in result.stdout  # quoted folder read correctly


def test_shell_pins_relative_override_before_changing_directory(tmp_path):
    result, _ = _shell(tmp_path, "relative-state")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["data_dir"] == str(tmp_path / "relative-state")


@pytest.fixture
def selector(tmp_path, monkeypatch):
    import data_directory as paths
    env = _env(tmp_path)
    for key, value in env.items():
        if key in {"HOME", "USERPROFILE", "XDG_DATA_HOME"}:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("LUMINA_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "DEFAULT_PROFILE", "release")
    # This is the platformdirs return value on any OS. Native macOS/Windows
    # discovery remains platformdirs' job, not a hardcoded Linux fallback.
    native = tmp_path / "native-data/lumina"
    monkeypatch.setattr(paths, "user_data_dir", lambda *a, **kw: str(native))
    return paths, Path(env["HOME"]) / ".local/share/lumina-release", native


@pytest.mark.parametrize("profile", ["release", "dev"])
def test_default_profile_never_adopts_the_other_installation(selector, profile):
    paths, release, native = selector
    expected, other = (release, native) if profile == "release" else (native, release)
    assert paths.select_data_directory(profile=profile) == str(expected)
    other.mkdir(parents=True)
    (other / "history-canary").write_bytes(b"preserve")
    with pytest.raises(paths.DataDirectoryError, match="LUMINA_DATA_DIR"):
        paths.select_data_directory(profile=profile)
    assert not expected.exists()
    assert (other / "history-canary").read_bytes() == b"preserve"


def test_both_existing_folders_require_explicit_choice(selector, monkeypatch):
    paths, release, native = selector
    for folder in [release, native]:
        folder.mkdir(parents=True)
    with pytest.raises(paths.DataDirectoryError, match="LUMINA_DATA_DIR"):
        paths.select_data_directory()
    monkeypatch.setenv("LUMINA_DATA_DIR", str(native))
    assert paths.select_data_directory() == str(native)


def test_preferred_existing_history_is_unchanged(selector):
    paths, release, _ = selector
    (release / "memory").mkdir(parents=True)
    (release / "memory/lumina.db").write_bytes(b"synthetic history, not opened")
    assert paths.select_data_directory() == str(release)
    assert (release / "memory/lumina.db").read_bytes() == b"synthetic history, not opened"


def test_old_linux_default_is_not_lost_when_xdg_base_changes(selector):
    paths, release, _ = selector
    old = release.parent / "lumina"
    old.mkdir(parents=True)
    with pytest.raises(paths.DataDirectoryError, match="LUMINA_DATA_DIR"):
        paths.select_data_directory()


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_empty_override_stops_instead_of_falling_back(selector, monkeypatch, value):
    paths, release, native = selector
    monkeypatch.setenv("LUMINA_DATA_DIR", value)
    with pytest.raises(paths.DataDirectoryError, match="empty"):
        paths.select_data_directory()
    assert not release.exists() and not native.exists()


def test_unobservable_alternate_is_not_mistaken_for_absence(selector, monkeypatch):
    paths, _, native = selector
    lstat = paths.os.lstat
    def denied(path, *args, **kwargs):
        if str(path) == str(native):
            raise PermissionError("synthetic denied")
        return lstat(path, *args, **kwargs)
    monkeypatch.setattr(paths.os, "lstat", denied)
    with pytest.raises(paths.DataDirectoryError, match="inspect"):
        paths.select_data_directory()


def test_legacy_source_requires_review_and_is_never_moved(selector, tmp_path):
    paths, release, _ = selector
    source = tmp_path / "old-install"
    (source / "memory").mkdir(parents=True)
    history = source / "memory/lumina.db"
    history.write_bytes(b"old history")
    with pytest.raises(paths.DataDirectoryError, match="legacy"):
        paths.configure_data_directory(base_dir=source)
    assert history.read_bytes() == b"old history"
    assert not release.exists()
    assert "LUMINA_DATA_DIR" not in os.environ


@pytest.mark.parametrize("entry", ["desktop", "cli", "telegram", "discord", "config"])
def test_all_entrypoints_stop_before_runtime_on_ambiguous_folders(tmp_path, entry):
    env = _env(tmp_path)
    for folder in [Path(env["HOME"]) / ".local/share/lumina-release",
                   Path(env["XDG_DATA_HOME"]) / "lumina"]:
        folder.mkdir(parents=True)
        (folder / "history-canary").write_bytes(b"never open or modify")
    commands = {
        "desktop": [str(ROOT / "main.py")],
        "cli": [str(ROOT / "main.py"), "--cli"],
        "telegram": ["-m", "comms.telegram_bridge"],
        "discord": ["-m", "comms.discord_bridge"],
        "config": ["-c", "import config"],
    }
    result = subprocess.run([sys.executable, *commands[entry]], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "LUMINA_DATA_DIR" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    for folder in [Path(env["HOME"]) / ".local/share/lumina-release",
                   Path(env["XDG_DATA_HOME"]) / "lumina"]:
        assert list(folder.iterdir()) == [folder / "history-canary"]
        assert (folder / "history-canary").read_bytes() == b"never open or modify"


def test_shell_stops_before_tts_or_main_when_folder_is_ambiguous(tmp_path):
    # _shell creates the preferred release folder; supply an alternate too.
    alternate = tmp_path / "native-data/lumina"
    alternate.mkdir(parents=True)
    result, _ = _shell(tmp_path)
    assert result.returncode != 0
    assert "LUMINA_DATA_DIR" in result.stderr
    assert "TTS backend" not in result.stdout
    assert '"data_dir"' not in result.stdout


def test_explicit_upgrade_preserves_existing_history_and_suspended_c1(tmp_path):
    from core import operator_ingress as oi, suspension as safety
    env = _env(tmp_path)
    selected = tmp_path / "established data"
    (selected / "memory").mkdir(parents=True, mode=0o700)
    with sqlite3.connect(selected / "memory/lumina.db") as db:
        db.execute("CREATE TABLE upgrade_history (message TEXT)")
        db.execute("INSERT INTO upgrade_history VALUES ('established conversation')")
    with safety.initialize(selected, ingress=oi.local_suspension_control("test")) as store:
        token = store.capture(safety.OWNER_SCOPE)
        incident = store.trip(token, trigger=safety.Trigger.BREAKER_TRIPPED)
        identity = (store.soul_id, store.incarnation)
    before = {p.name: p.read_bytes() for p in (selected / "memory").iterdir() if p.is_file()}
    # Simulate an upgrade where both plausible roots exist; explicit history
    # selection must win without migrating or initializing any safety record.
    (Path(env["HOME"]) / ".local/share/lumina-release").mkdir(parents=True)
    env["LUMINA_DATA_DIR"] = str(selected)
    probe = (
        "import config,json,sqlite3; "
        "db=sqlite3.connect('file:'+config.DB_PATH+'?mode=ro',uri=True); "
        "print(json.dumps([config.DATA_DIR,db.execute('SELECT message FROM upgrade_history').fetchone()[0]])); "
        "db.close()"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(selected), "established conversation"]
    assert {p.name: p.read_bytes() for p in (selected / "memory").iterdir() if p.is_file()} == before
    with safety.open_store(selected) as store:
        assert (store.soul_id, store.incarnation) == identity
        assert [v.suspension_id for v in store.operator_view()] == [incident.suspension_id]


def test_selected_existing_history_survives_no_trace_desktop_recovery(selector, tmp_path, monkeypatch):
    from core import operator_ingress as oi, suspension as safety, suspension_runtime as runtime
    paths, release, _ = selector
    selected = tmp_path / "established data"
    (selected / "memory").mkdir(parents=True, mode=0o700)
    history = selected / "memory/lumina.db"
    with sqlite3.connect(history) as db:
        db.execute("CREATE TABLE upgrade_history (message TEXT)")
        db.execute("INSERT INTO upgrade_history VALUES ('established conversation')")
    history_before = history.read_bytes()
    with safety.initialize(selected, ingress=oi.local_suspension_control("test")) as store:
        old_token = store.capture(safety.OWNER_SCOPE)
        store.trip(old_token, trigger=safety.Trigger.BREAKER_TRIPPED)
    # Synthetic loss of all safety records, while ordinary history survives.
    for name in safety._STORE_FILES + (safety.IDENTITY_NAME,):
        (selected / "memory" / name).unlink(missing_ok=True)
    release.mkdir(parents=True)
    monkeypatch.setenv("LUMINA_DATA_DIR", str(selected))
    chosen = paths.configure_data_directory(base_dir=tmp_path / "installation")
    assert chosen == str(selected)
    runtime.start(chosen, role="desktop", heartbeat=False)
    try:
        assert runtime.status().probe_status is safety.StoreStatus.ABSENT_NO_TRACE
        with pytest.raises(safety.RecoveryBlocked):
            runtime.recover(ingress=oi.local_suspension_control("recover"))
        outcome = runtime.recover(ingress=oi.local_suspension_control("recover"),
                                  acknowledge_unknown_prior_state=True)
        assert outcome.evidence_survived is False
        with pytest.raises(runtime.SuspensionRefused):
            runtime.capture(safety.OWNER_SCOPE)
        with safety.open_store(chosen) as store:
            with pytest.raises(safety.AdmissionRefused) as exc:
                store.verify(old_token)
            assert exc.value.refusal is safety.Refusal.FOREIGN_INCARNATION
            with pytest.raises(safety.AdmissionRefused):
                store.capture(safety.channel_scope("discord", "synthetic"))
    finally:
        runtime.stop()
    assert history.read_bytes() == history_before
    assert list(release.iterdir()) == []
    with sqlite3.connect(f"file:{history}?mode=ro", uri=True) as db:
        assert db.execute("SELECT message FROM upgrade_history").fetchone()[0] == "established conversation"
