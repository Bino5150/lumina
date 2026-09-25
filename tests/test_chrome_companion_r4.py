"""BROWSER-COMPANION-01A-R4 / AR4 -- the data dir reaches the walk AS GIVEN.

Goblin's AR4: R3 anchored every installer write to the directory objects its
walk opened, but installer.install()/uninstall() and the owner CLI first ran
Path(data_dir).resolve(). Resolving swaps a link -- and the directory holding
it -- for whatever the link pointed at in that instant, so the walk vetted only
the target's safe-looking ancestry. A data-dir link in a mode-0777
(non-sticky) directory, re-aimed by anyone at an unrelated private data dir,
let install replace that dir's 0600 host_config.json (bytes and inode changed).

Law now: nothing canonicalizes the data dir before the walk. It is made
absolute (state.absolute_path) and walked exactly as supplied, so the link
and the directory holding it are checked like every other component, and the
paths recorded in the launcher and Chrome's manifest are the ones the walk
vetted. Every entry point -- install, pair, status, unpair, uninstall, via the
CLI and the library -- refuses the replaceable chain.

Every test that points at the hostile link holds a fully installed, paired,
UNRELATED data dir and proves each of its files unchanged in bytes, mode and
inode (fixture teardown). Everything runs in pytest's tmp dir."""
from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from chrome_companion import installer, protocol, state
from chrome_companion_testkit import EXT_ID, INSTANCE, OTHER_EXT_ID, OTHER_INSTANCE, short_tmpdir
from test_chrome_companion_installer import REPO, companion_cli  # the real CLI module

MANIFEST_NAME = f"{protocol.HOST_NAME}.json"
ORIGIN = protocol.extension_origin(EXT_ID)


def _snapshot(root: Path) -> dict:
    """Every entry under ``root``: type, mode, inode, and a regular file's bytes."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = os.lstat(path)
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            out[str(path.relative_to(root))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                                                info.st_ino, data)
    return out


class World:
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Goblin's AR4 layout. ``intended``: the owner's private data dir.
    ``unrelated``: another private data dir, fully installed (host config,
    launcher, install record) and paired -- the canary. ``link``: the data-dir
    link the owner passes, inside ``loose``, a mode-0777 NON-sticky directory,
    so any local user can replace it. Chrome's own dir is safe and private."""
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    monkeypatch.delenv("LUMINA_DATA_DIR", raising=False)
    w = World()
    w.tmp = tmp_path
    w.config = tmp_path / "config"
    w.config.mkdir(mode=0o700)
    w.profile = w.config / "google-chrome"
    w.profile.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(w.config))
    w.intended = tmp_path / "intended-data"
    w.intended.mkdir(mode=0o700)
    w.unrelated = tmp_path / "unrelated-data"
    w.unrelated.mkdir(mode=0o700)
    unrelated_profile = tmp_path / "unrelated-profile"
    unrelated_profile.mkdir(mode=0o700)
    w.loose = tmp_path / "loose"
    w.loose.mkdir()
    os.chmod(w.loose, 0o777)
    assert not os.lstat(w.loose).st_mode & stat.S_ISVTX
    w.link = w.loose / "lumina-data"
    w.link.symlink_to(w.intended)
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        installer.install(w.unrelated, extension_id=OTHER_EXT_ID, chrome_dir=unrelated_profile)
        state.save_pairing(w.unrelated, instance_id=OTHER_INSTANCE,
                           extension_origin=protocol.extension_origin(OTHER_EXT_ID))
        w.host_config = w.unrelated / "chrome_companion" / "native_host" / installer.HOST_CONFIG_FILENAME
        before = _snapshot(w.unrelated)
        assert w.host_config.exists() and len(before) == 6  # dirs + launcher, config, install, pairing
        profile_before = _snapshot(w.profile)
        yield w
    assert _snapshot(w.unrelated) == before, "the unrelated data dir changed (bytes, mode, inode or entries)"
    if not getattr(w, "may_write_profile", False):
        assert _snapshot(w.profile) == profile_before, "Chrome's dir was written for a refused data dir"


def _aim(world, target: Path) -> None:
    """Anyone can do this: the link's directory is 0777 and not sticky."""
    tmp = world.loose / ".swap"
    tmp.symlink_to(target)
    os.replace(tmp, world.link)


def _cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = companion_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# ── Goblin's AR4 reproduction, now blocking ──────────────────────────────

@pytest.mark.parametrize("aimed_at", ["unrelated", "intended"])
def test_goblin_ar4_data_dir_link_in_a_0777_parent_is_refused_before_any_write(world, aimed_at):
    """AR4 verbatim: the link first aims at the intended dir, then is replaced
    by a link to the unrelated one. R3 resolved it and replaced the unrelated
    host_config.json (fixture teardown would fail). R4 walks the link itself:
    the directory holding it is writable by other users, so it is refused --
    whichever way it points, because anyone could re-aim it at any moment."""
    if aimed_at == "unrelated":
        _aim(world, world.unrelated)
    with pytest.raises(state.StateError, match="writable by other users"):
        installer.install(world.link, extension_id=EXT_ID, chrome_dir=world.profile)
    assert os.listdir(world.intended) == [], "refused before creating anything"


def test_uninstall_through_the_replaceable_link_removes_nothing(world):
    """R3's uninstall resolved the link too: aimed at the unrelated dir, it
    would unlink that dir's launcher, host config, install and pairing."""
    _aim(world, world.unrelated)
    removed = installer.uninstall(world.link, chrome_dir=world.profile)
    assert removed == []


@pytest.mark.parametrize("call", ["save_pairing", "clear_pairing", "load_install", "load_pairing", "save_install"])
def test_every_state_entry_point_refuses_the_replaceable_link(world, call):
    _aim(world, world.unrelated)
    fn = {
        "save_pairing": lambda: state.save_pairing(world.link, instance_id=INSTANCE, extension_origin=ORIGIN),
        "clear_pairing": lambda: state.clear_pairing(world.link),
        "load_install": lambda: state.load_install(world.link),
        "load_pairing": lambda: state.load_pairing(world.link),
        "save_install": lambda: state.save_install(world.link, extension_id=EXT_ID, socket_path="/x/hub.sock"),
    }[call]
    with pytest.raises(state.StateError, match="writable by other users"):
        fn()


@pytest.mark.parametrize("command", [
    ["install", "--extension-id", EXT_ID],
    ["pair", INSTANCE],
    ["status"],
    ["unpair"],
    ["uninstall"],
], ids=["install", "pair", "status", "unpair", "uninstall"])
def test_goblin_ar4_every_cli_command_refuses_the_replaceable_data_dir(world, command):
    """The canary proof Goblin asked for, per command, through the real CLI:
    with --data-dir the 0777-held link (aimed at the unrelated dir), every
    command exits 1 naming the refusal, and neither reads nor changes the
    unrelated dir (teardown: bytes, mode, inode). R3's _data_dir() resolved
    first: install/pair overwrote, unpair/uninstall deleted, status reported
    the unrelated dir's state."""
    _aim(world, world.unrelated)
    code, out, err = _cli("--data-dir", str(world.link), *command)
    assert code == 1
    assert "writable by other users" in err
    assert "unrelated" not in out + err, "nothing about the link's target is disclosed"
    assert out == ""


def test_cli_via_lumina_data_dir_env_is_checked_the_same_way(world, monkeypatch):
    _aim(world, world.unrelated)
    monkeypatch.setenv("LUMINA_DATA_DIR", str(world.link))
    code, out, err = _cli("status")
    assert (code, out) == (1, "") and "writable by other users" in err


def test_cli_missing_data_dir_is_still_reported_as_missing(world):
    with pytest.raises(SystemExit, match="data dir does not exist"):
        _cli("--data-dir", str(world.tmp / "no-such-dir"), "status")


def test_cli_a_replaceable_link_to_nowhere_is_a_refusal_not_missing(world):
    """The refusal is decided on the chain, before existence: a hostile link
    whose target is gone must not be reported as merely missing."""
    _aim(world, world.tmp / "gone")
    code, _, err = _cli("--data-dir", str(world.link), "status")
    assert code == 1 and "writable by other users" in err


# ── Ordinary life: a relocated data dir is still supported ───────────────

@pytest.fixture
def relocated(world):
    """The legitimate shape: the data dir is a link in a PRIVATE directory
    (only the owner can re-aim it) to a private dir elsewhere."""
    home = world.tmp / "home"
    home.mkdir(mode=0o700)
    link = home / "lumina-data"
    link.symlink_to(world.intended)
    world.may_write_profile = True
    return link


def test_relocated_data_dir_full_cli_cycle_through_the_link(world, relocated):
    code, out, err = _cli("--data-dir", str(relocated), "install", "--extension-id", EXT_ID)
    assert code == 0, err
    manifest = json.loads((world.profile / "NativeMessagingHosts" / MANIFEST_NAME).read_text())
    launcher = relocated / "chrome_companion" / "native_host" / installer.LAUNCHER_FILENAME
    assert manifest["path"] == str(launcher), "the recorded path is the one the walk vetted, not a resolved one"
    assert os.path.samefile(launcher, world.intended / "chrome_companion" / "native_host" / installer.LAUNCHER_FILENAME)
    assert f"--config {relocated}/chrome_companion/native_host/host_config.json" in launcher.read_text()
    assert _cli("--data-dir", str(relocated), "pair", state.format_instance_id(INSTANCE))[0] == 0
    code, out, _ = _cli("--data-dir", str(relocated), "status")
    status = json.loads(out)
    assert code == 0 and status["installed"] and status["data_dir"] == str(relocated)
    assert status["paired_instance_fingerprint"] == state.instance_fingerprint(INSTANCE)
    assert _cli("--data-dir", str(relocated), "unpair")[0] == 0
    assert state.load_pairing(world.intended) is None
    code, out, _ = _cli("--data-dir", str(relocated), "uninstall")
    assert code == 0 and str(world.profile / "NativeMessagingHosts" / MANIFEST_NAME) in out
    assert state.load_install(world.intended) is None


def test_relocated_data_dir_launcher_really_starts_the_host_through_the_link(world, relocated):
    """The launcher and host config are used BY PATH at runtime (Chrome runs
    the launcher; the host walks --config). Through the unresolved link path
    both still reach the files the installer wrote."""
    result = installer.install(relocated, extension_id=EXT_ID, chrome_dir=world.profile)
    hello = protocol.encode_frame({"v": 1, "type": "hello", "extension_id": EXT_ID, "instance_id": INSTANCE,
                                   "extension_version": "0.1.0"}, protocol.MAX_FROM_EXTENSION_BYTES)
    proc = subprocess.run([result["launcher"], ORIGIN], input=hello, capture_output=True, timeout=10, check=False)
    assert proc.returncode == 0, proc.stderr
    assert protocol.decode_payload(proc.stdout[4:]) == {"v": 1, "type": "host_status", "state": "hub_unavailable"}


def test_the_owner_re_aiming_the_link_mid_install_cannot_split_the_install(world, relocated, monkeypatch):
    """Anchoring for the data dir itself: once the companion directory is
    open, every write goes through it. The owner re-aims the (trusted) link at
    the unrelated dir right after it was opened; every artifact still lands in
    the directory object the walk opened."""
    real = state.open_companion_dir

    def open_then_reaim(data_dir, **kwargs):
        fd = real(data_dir, **kwargs)
        if Path(data_dir) == relocated:
            tmp = relocated.with_name(".reaim")
            tmp.symlink_to(world.unrelated)
            os.replace(tmp, relocated)
        return fd
    monkeypatch.setattr(state, "open_companion_dir", open_then_reaim)
    installer.install(relocated, extension_id=EXT_ID, chrome_dir=world.profile)
    landed = world.intended / "chrome_companion"
    assert state.load_install(world.intended).extension_origin == ORIGIN
    assert (landed / "native_host" / installer.HOST_CONFIG_FILENAME).exists()
    assert (landed / "native_host" / installer.LAUNCHER_FILENAME).exists()


def test_dot_dot_after_a_link_is_taken_physically_never_folded(world):
    """"<link>/../x" names x beside the link's TARGET (the kernel, and the walk,
    step back from where the link led). Folding the text first -- abspath,
    normpath -- would name x beside the LINK instead: a different directory
    than the one written. The recorded launcher path must lead the kernel to
    the very file the installer wrote."""
    far = world.tmp / "far"
    far.mkdir(mode=0o700)
    (far / "inner").mkdir(mode=0o700)
    (far / "data").mkdir(mode=0o700)
    home = world.tmp / "home2"
    home.mkdir(mode=0o700)
    (home / "data").mkdir(mode=0o700)  # the decoy a textual fold would pick
    (home / "hop").symlink_to(far / "inner")
    world.may_write_profile = True
    result = installer.install(home / "hop" / ".." / "data", extension_id=EXT_ID, chrome_dir=world.profile)
    assert os.listdir(home / "data") == [], "nothing landed beside the link"
    written = far / "data" / "chrome_companion" / "native_host" / installer.LAUNCHER_FILENAME
    assert os.path.samefile(result["launcher"], written)
    assert "/../" in result["launcher"], "the recorded path was not folded"


def test_relative_data_dir_is_made_absolute_against_the_cwd_without_resolving(world, relocated, monkeypatch):
    monkeypatch.chdir(relocated.parent)
    result = installer.install(Path("lumina-data"), extension_id=EXT_ID, chrome_dir=world.profile)
    assert result["launcher"] == str(relocated / "chrome_companion" / "native_host" / installer.LAUNCHER_FILENAME)
    assert state.absolute_path("a/../b") == Path(os.getcwd()) / "a" / ".." / "b"


def test_socket_scope_is_the_opened_directory_not_a_pathname(world, relocated):
    """The hub socket name is scoped by the companion directory OBJECT the
    installer opened: the same data dir through two spellings shares one
    socket; two data dirs never share one."""
    world.may_write_profile = True
    via_link = installer.install(relocated, extension_id=EXT_ID, chrome_dir=world.profile)["socket_path"]
    direct = installer.install(world.intended, extension_id=EXT_ID, chrome_dir=world.profile)["socket_path"]
    assert via_link == direct
    assert state.load_install(world.unrelated).socket_path != direct
    assert state.load_install(world.intended).socket_path == direct


def test_no_pathname_canonicalization_before_the_walk_in_installer_sources():
    """Source guard, alongside the behavioural tests above: the installer, its
    state module and the owner CLI never resolve/realpath/abspath/normpath a
    data dir. (The only resolve() left is each file locating its OWN code.)"""
    for rel in ("chrome_companion/installer.py", "chrome_companion/state.py", "scripts/chrome_companion_setup.py"):
        code = "\n".join(line for line in (REPO / rel).read_text().splitlines() if not line.lstrip().startswith("#"))
        code = re.sub(r'"""(.|\n)*?"""', "", code)
        hits = re.findall(r".*(?:\.resolve\(|realpath|abspath|normpath|samefile).*", code)
        allowed = [h for h in hits if "__file__" in h]
        assert hits == allowed, f"{rel}: {[h.strip() for h in hits if h not in allowed]}"


# ── R4-B: the popup revoke contract, as the owner reads it ───────────────
# (Behaviour: tests/chrome_companion_js/worker_test.js, "R4 ..." cases, run by
# test_chrome_companion_extension.py against the real popup.js + worker.js.)

EXTENSION = REPO / "chrome_companion" / "extension"


def test_readme_states_the_acknowledged_contract_not_a_click_time_one():
    text = " ".join((REPO / "chrome_companion" / "README.md").read_text().split())
    assert "nothing read crosses to Lumina after you revoke" not in text, "the AR4-refuted click-time promise"
    assert "*Revoking…* until Lumina's companion (the extension's background worker) confirms the revoke" in text
    assert "says *Revoked* only once it has" in text
    assert "From the moment the companion receives your revoke" in text
    assert "a read that finished in the instant between your click and the companion receiving it may already have reached Lumina" in text
    assert "says the companion did not confirm" in text and "only as strong as removing access in Chrome's settings" in text


def test_every_element_the_popup_script_uses_exists_in_the_popup_page():
    """The Node harness creates elements on demand, so it cannot notice one
    missing from popup.html -- where getElementById would return null and the
    revoke handler would throw before any acknowledgement is shown."""
    script = (EXTENSION / "popup.js").read_text()
    page = (EXTENSION / "popup.html").read_text()
    used = set(re.findall(r'\$\("([\w-]+)"\)', script))
    assert {"revoke", "revoke-note", "tab-state", "grant"} <= used
    missing = sorted(i for i in used if f'id="{i}"' not in page)
    assert missing == []


def test_extension_version_bumped_for_the_r4_popup_contract():
    """A live harness gates on extension_version: a Chrome still running the
    R3 worker/popup (0.1.3) must not pass for this candidate."""
    version = json.loads((EXTENSION / "manifest.json").read_text())["version"]
    assert tuple(int(part) for part in version.split(".")) >= (0, 1, 4)
