"""BROWSER-COMPANION-01A -- installer, pairing CLI, Firefox non-interaction,
extension manifest least-privilege, and tracked-source hygiene."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from chrome_companion import installer, protocol, state
from chrome_companion_testkit import EXT_ID, INSTANCE, short_tmpdir

REPO = Path(__file__).resolve().parents[1]
EXTENSION_DIR = REPO / "chrome_companion" / "extension"
_spec = importlib.util.spec_from_file_location("chrome_companion_setup",
                                               REPO / "scripts" / "chrome_companion_setup.py")
companion_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(companion_cli)


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = os.lstat(path)
            out[str(path.relative_to(root))] = (stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns)
    return out


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mozilla" / "native-messaging-hosts").mkdir(parents=True)
    (home / ".mozilla" / "firefox" / "abcd.default-release").mkdir(parents=True)
    (home / ".mozilla" / "firefox" / "profiles.ini").write_text("[Profile0]\nPath=abcd.default-release\n")
    (home / ".librewolf" / "native-messaging-hosts").mkdir(parents=True)
    (home / ".config" / "google-chrome").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


def test_install_writes_only_owner_only_chrome_artifacts(fake_home, data_dir, monkeypatch):
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        result = installer.install(data_dir, extension_id=EXT_ID)
        manifest_path = fake_home / ".config" / "google-chrome" / "NativeMessagingHosts" / \
            f"{protocol.HOST_NAME}.json"
        assert Path(result["host_manifest"]) == manifest_path
        manifest = json.loads(manifest_path.read_text())
        assert manifest == {
            "name": protocol.HOST_NAME,
            "description": manifest["description"],
            "path": result["launcher"],
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXT_ID}/"],
        }
        assert _mode(manifest_path) == 0o600
        assert _mode(result["launcher"]) == 0o700 and os.path.isabs(result["launcher"])
        assert _mode(result["host_config"]) == 0o600
        assert _mode(state.companion_dir(data_dir)) == 0o700
        assert _mode(state.companion_dir(data_dir) / state.INSTALL_FILENAME) == 0o600
        assert result["socket_path"].startswith(os.path.join(runtime, "lumina") + os.sep)
        record = state.load_install(data_dir)
        assert record.extension_origin == f"chrome-extension://{EXT_ID}/"


def test_firefox_is_never_touched(fake_home, data_dir, monkeypatch):
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        before = _tree(fake_home)
        installer.install(data_dir, extension_id=EXT_ID)
        state.save_pairing(data_dir, instance_id=INSTANCE, extension_origin=f"chrome-extension://{EXT_ID}/")
        installer.uninstall(data_dir)
        after = _tree(fake_home)
        firefox_like = lambda tree: {k: v for k, v in tree.items()  # noqa: E731
                                     if k.startswith((".mozilla", ".librewolf"))}
        assert firefox_like(after) == firefox_like(before)
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        assert all(k.startswith(".config/google-chrome") for k in changed), changed


def test_nested_private_dirs_are_0700_under_a_permissive_umask(tmp_path):
    """Regression: Path.mkdir(parents=True) created intermediate levels at
    0775 under umask 002, so the installer's own private-dir check refused
    <data_dir>/chrome_companion and `install` failed on a normal desktop."""
    old = os.umask(0o002)
    try:
        leaf = state.ensure_private_dir(tmp_path / "a" / "b" / "c")
    finally:
        os.umask(old)
    for level in (tmp_path / "a", tmp_path / "a" / "b", leaf):
        assert _mode(level) == 0o700


def test_invalid_extension_ids_refused(data_dir):
    for bad in ("", "ABCDEFGHIJKLMNOPABCDEFGHIJKLMNOP", "abcdefghijklmnopabcdefghijklmnoz", "a" * 31,
                "moz-extension://whatever"):
        with pytest.raises(state.StateError):
            installer.install(data_dir, extension_id=bad)


def test_pairing_normalises_and_validates_instance_ids(data_dir):
    origin = f"chrome-extension://{EXT_ID}/"
    record = state.save_pairing(data_dir, instance_id=state.format_instance_id(INSTANCE).upper(),
                                extension_origin=origin)
    assert record.instance_id == INSTANCE
    assert state.load_pairing(data_dir).instance_id == INSTANCE
    assert _mode(state.companion_dir(data_dir) / state.PAIRING_FILENAME) == 0o600
    for bad in ("", "xyz", INSTANCE[:-1], INSTANCE + "0", "gmail:lumina@example.com"):
        with pytest.raises(state.StateError):
            state.save_pairing(data_dir, instance_id=bad, extension_origin=origin)
    assert state.clear_pairing(data_dir) is True
    assert state.load_pairing(data_dir) is None


def test_insecure_state_files_are_refused(data_dir):
    state.save_install(data_dir, extension_id=EXT_ID, socket_path="/tmp/x.sock")
    path = state.companion_dir(data_dir) / state.INSTALL_FILENAME
    os.chmod(path, 0o644)
    with pytest.raises(state.StateError):
        state.load_install(data_dir)


def test_uninstall_only_removes_our_own_manifest(fake_home, data_dir, monkeypatch):
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        result = installer.install(data_dir, extension_id=EXT_ID)
        manifest_path = Path(result["host_manifest"])
        foreign = json.loads(manifest_path.read_text())
        foreign["path"] = "/opt/someone-else/host"
        manifest_path.write_text(json.dumps(foreign))
        installer.uninstall(data_dir)
        assert manifest_path.exists()  # not ours any more -> left alone
        assert state.load_install(data_dir) is None


def test_cli_requires_an_explicit_data_dir(monkeypatch):
    monkeypatch.delenv("LUMINA_DATA_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        companion_cli.main(["status"])
    assert "--data-dir is required" in str(exc.value)


def test_cli_install_pair_status_round_trip(fake_home, data_dir, monkeypatch):
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert companion_cli.main(["--data-dir", str(data_dir), "install", "--extension-id", EXT_ID]) == 0
            assert companion_cli.main(["--data-dir", str(data_dir), "pair",
                                       state.format_instance_id(INSTANCE)]) == 0
            assert companion_cli.main(["--data-dir", str(data_dir), "status"]) == 0
        status = json.loads(buf.getvalue()[buf.getvalue().index("{"):])
        assert status["installed"] and status["paired_instance_fingerprint"] == state.instance_fingerprint(INSTANCE)
        assert INSTANCE not in buf.getvalue().split("Paired Chrome instance")[0]


def test_cli_pair_before_install_is_refused(data_dir):
    with pytest.raises(SystemExit) as exc:
        companion_cli.main(["--data-dir", str(data_dir), "pair", INSTANCE])
    assert "run install first" in str(exc.value)


def test_generated_launcher_really_starts_the_host(fake_home, data_dir, monkeypatch):
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        result = installer.install(data_dir, extension_id=EXT_ID)
        hello = protocol.encode_frame({"v": 1, "type": "hello", "extension_id": EXT_ID,
                                       "instance_id": INSTANCE, "extension_version": "0.1.0"},
                                      protocol.MAX_FROM_EXTENSION_BYTES)
        proc = subprocess.run([result["launcher"], f"chrome-extension://{EXT_ID}/"], input=hello,
                              capture_output=True, timeout=10, check=False)
        assert proc.returncode == 0, proc.stderr
        assert protocol.decode_payload(proc.stdout[4:]) == {"v": 1, "type": "host_status",
                                                            "state": "hub_unavailable"}
        wrong = subprocess.run([result["launcher"], "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba/"],
                               input=hello, capture_output=True, timeout=10, check=False)
        assert wrong.returncode == 2 and wrong.stdout == b""


# ── Extension manifest least privilege & Firefox exclusion ───────────────

def test_extension_manifest_is_least_privilege():
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    # webNavigation (R1/F3): only webNavigation.getFrame, for Chrome's document
    # identity; Chrome shows the same install warning as "tabs" ("Read your
    # browsing history"), so it adds no new warning. No listeners are added.
    assert sorted(manifest["permissions"]) == sorted(["nativeMessaging", "tabs", "scripting", "storage", "alarms",
                                                      "webNavigation"])
    assert manifest["optional_host_permissions"] == ["http://*/*", "https://*/*"]
    assert manifest["incognito"] == "not_allowed"
    for forbidden in ("host_permissions", "content_scripts", "externally_connectable",
                      "web_accessible_resources", "browser_specific_settings", "key", "update_url"):
        assert forbidden not in manifest, forbidden
    for forbidden_perm in ("debugger", "activeTab", "cookies", "webRequest", "history", "<all_urls>",
                           "declarativeNetRequest", "downloads", "management", "proxy"):
        assert forbidden_perm not in manifest["permissions"]
    assert manifest["background"] == {"service_worker": "worker.js"}
    code = "\n".join(re.sub(r"(?m)^\s*//.*$", "", p.read_text()) for p in EXTENSION_DIR.glob("*.js"))
    assert set(re.findall(r"webNavigation\.(\w+)", code)) == {"getFrame"}, "webNavigation is used for getFrame only"


def test_extension_has_no_page_bridge_or_remote_code():
    # Full-line // comments may name what is forbidden; only code is scanned.
    sources = {p.name: re.sub(r"(?m)^\s*//.*$", "", p.read_text()) for p in EXTENSION_DIR.glob("*.js")}
    sources["popup.html"] = (EXTENSION_DIR / "popup.html").read_text()
    for name, text in sources.items():
        for forbidden in ("postMessage(window", "window.postMessage", "addEventListener(\"message\"",
                          "onMessageExternal", "onConnectExternal", "eval(", "new Function",
                          "chrome.debugger", "captureVisibleTab", "chrome.cookies", "fetch(",
                          "XMLHttpRequest", "WebSocket", "http://localhost", "127.0.0.1"):
            assert forbidden not in text, f"{forbidden!r} in {name}"
    assert "<script>" not in sources["popup.html"]  # MV3 CSP: no inline script


def test_tracked_companion_sources_hold_no_machine_paths_or_secrets():
    roots = [REPO / "chrome_companion", REPO / "core" / "chrome_companion_hub.py",
             REPO / "tools" / "chrome_companion.py", REPO / "scripts" / "chrome_companion_setup.py"]
    files = [p for root in roots for p in ([root] if root.is_file() else root.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in (str(Path.home()) + "/", "gmail.com/mail", "Bearer ", "sk-", "BEGIN PRIVATE KEY"):
            assert needle not in text, f"{needle!r} in {path}"
    assert not (REPO / "chrome_companion" / "native_host" / "host_config.json").exists()
