"""BROWSER-COMPANION-01A-R2 / B3 -- the installer in a hostile filesystem.

Law: INSTALLATION MUST NEVER FOLLOW AN ATTACKER-PLANTED FILE TARGET. Every
installer write (host manifest, launcher, host config, install record,
pairing) may affect only its own intended name: no planted symlink, no
pre-existing temp name, no swapped parent directory can redirect it, and a
directory other users can write is refused, not repaired. A canary file
outside the install must stay byte-identical through every attack.

Everything runs under a fake HOME / XDG_CONFIG_HOME in pytest's tmp dir."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from chrome_companion import installer, protocol, state
from chrome_companion_testkit import EXT_ID, INSTANCE, short_tmpdir
from test_chrome_companion_installer import companion_cli  # the real CLI module

CANARY = b"CANARY-B3 unrelated file: must stay byte-identical\n"
ORIGIN = f"chrome-extension://{EXT_ID}/"
MANIFEST_NAME = f"{protocol.HOST_NAME}.json"


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "google-chrome").mkdir(parents=True)
    os.chmod(home / ".config" / "google-chrome", 0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir(mode=0o700)
    victim = victim_dir / "CANARY-victim.txt"
    victim.write_bytes(CANARY)
    os.chmod(victim, 0o600)
    victim_inode = os.lstat(victim).st_ino
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)

        class World:
            pass
        w = World()
        w.home, w.data_dir, w.victim, w.victim_dir = home, data_dir, victim, victim_dir
        w.chrome = home / ".config" / "google-chrome"
        w.nmh = w.chrome / "NativeMessagingHosts"
        w.host_dir = installer.native_host_dir(data_dir)
        w.install = lambda: installer.install(data_dir, extension_id=EXT_ID)
        yield w
        assert victim.read_bytes() == CANARY, "the unrelated canary file was modified"
        assert _mode(victim) == 0o600
        assert os.lstat(victim).st_ino == victim_inode, "the unrelated canary file was replaced"


def _assert_installed(w, result):
    manifest = w.nmh / MANIFEST_NAME
    assert stat.S_ISREG(os.lstat(manifest).st_mode) and _mode(manifest) == 0o600
    assert json.loads(manifest.read_text())["path"] == result["launcher"]
    launcher = Path(result["launcher"])
    assert stat.S_ISREG(os.lstat(launcher).st_mode) and _mode(launcher) == 0o700
    assert _mode(result["host_config"]) == 0o600
    assert state.load_install(w.data_dir).extension_origin == ORIGIN
    for directory in (w.nmh, w.host_dir, state.companion_dir(w.data_dir)):
        assert not [p for p in os.listdir(directory) if p.endswith(".tmp")], f"temp left in {directory}"


# ── Goblin's AR2 reproduction ────────────────────────────────────────────

def test_goblin_r2_3_world_writable_registration_dir_with_planted_temp_symlink(world):
    world.nmh.mkdir()
    os.chmod(world.nmh, 0o777)
    planted = world.nmh / f".{MANIFEST_NAME}.tmp"  # the pre-R2 fixed temp name
    planted.symlink_to(world.victim)
    with pytest.raises(state.StateError, match="writable by other users"):
        world.install()
    assert os.path.islink(planted) and not (world.nmh / MANIFEST_NAME).exists()


@pytest.mark.parametrize("mode", [0o777, 0o773, 0o757, 0o1777])
def test_registration_dir_writable_by_others_is_refused_not_repaired(world, mode):
    world.nmh.mkdir()
    os.chmod(world.nmh, mode)
    with pytest.raises(state.StateError, match="writable by other users"):
        world.install()
    assert _mode(world.nmh) == mode, "a hostile directory is never silently 'fixed'"
    assert os.listdir(world.nmh) == []


def test_group_writable_registration_dir_is_refused_unless_the_group_is_private(world, monkeypatch):
    world.nmh.mkdir()
    os.chmod(world.nmh, 0o770)
    monkeypatch.setattr(state, "_private_group", lambda gid: False)
    with pytest.raises(state.StateError, match="writable by other users"):
        world.install()
    monkeypatch.setattr(state, "_private_group", lambda gid: True)  # Debian/Ubuntu user-private group
    _assert_installed(world, world.install())


@pytest.mark.skipif(os.getuid() == 0, reason="root is a member of group 0")
def test_a_group_with_another_member_is_never_treated_as_private():
    """Group 0 always has root (uid 0) as a primary member, so for any other
    user it is a shared group: group write there is write by someone else."""
    assert state._private_group(0) is False


# ── Planted temp names ───────────────────────────────────────────────────

def test_old_predictable_temp_names_planted_as_symlinks_are_never_followed(world):
    world.nmh.mkdir(mode=0o700)
    state.ensure_private_dir(world.host_dir)
    planted = [world.nmh / f".{MANIFEST_NAME}.tmp", world.host_dir / f".{installer.LAUNCHER_FILENAME}.tmp",
               world.host_dir / f".{installer.HOST_CONFIG_FILENAME}.{os.getpid()}.tmp",
               state.companion_dir(world.data_dir) / f".{state.INSTALL_FILENAME}.{os.getpid()}.tmp",
               state.companion_dir(world.data_dir) / f".{state.PAIRING_FILENAME}.{os.getpid()}.tmp"]
    for link in planted:
        link.symlink_to(world.victim)
    result = world.install()
    state.save_pairing(world.data_dir, instance_id=INSTANCE, extension_origin=ORIGIN)
    for link in planted:
        assert os.path.islink(link) and os.readlink(link) == str(world.victim)
    manifest = world.nmh / MANIFEST_NAME
    assert stat.S_ISREG(os.lstat(manifest).st_mode) and json.loads(manifest.read_text())["path"] == result["launcher"]
    assert state.load_pairing(world.data_dir).instance_id == INSTANCE


@pytest.mark.parametrize("target", ["manifest", "launcher", "config", "pairing"])
def test_a_colliding_temp_name_is_skipped_never_followed(world, monkeypatch, target):
    """Even a temp name an attacker managed to predict is created with
    O_EXCL|O_NOFOLLOW: the collision is skipped for a fresh name; if every
    candidate collides the write is refused -- never redirected."""
    world.nmh.mkdir(mode=0o700)
    state.ensure_private_dir(world.host_dir)
    directory, name = {
        "manifest": (world.nmh, MANIFEST_NAME),
        "launcher": (world.host_dir, installer.LAUNCHER_FILENAME),
        "config": (world.host_dir, installer.HOST_CONFIG_FILENAME),
        "pairing": (state.companion_dir(world.data_dir), state.PAIRING_FILENAME),
    }[target]
    planted = directory / f".{name}.predicted.tmp"
    planted.symlink_to(world.victim)
    tokens = iter(["predicted", "fresh1", "fresh2", "fresh3"])
    monkeypatch.setattr(state, "_temp_token", lambda: next(tokens, "fresh-last"))
    if target == "pairing":
        world.install()
        tokens = iter(["predicted", "fresh9"])
        monkeypatch.setattr(state, "_temp_token", lambda: next(tokens, "fresh-last"))
        state.save_pairing(world.data_dir, instance_id=INSTANCE, extension_origin=ORIGIN)
    else:
        world.install()
    assert os.path.islink(planted)
    monkeypatch.setattr(state, "_temp_token", lambda: "predicted")
    dir_fd = state.open_trusted_dir(directory, private=directory != world.nmh)
    try:
        with pytest.raises(state.StateError, match="temporary file"):
            state.write_file_at(dir_fd, name, b"x", mode=0o600, path=directory / name)
    finally:
        os.close(dir_fd)
    assert os.path.islink(planted)


# ── The final target ─────────────────────────────────────────────────────

def test_final_manifest_planted_as_symlink_is_refused(world):
    world.nmh.mkdir(mode=0o700)
    link = world.nmh / MANIFEST_NAME
    link.symlink_to(world.victim)
    with pytest.raises(state.StateError, match="refusing to replace"):
        world.install()
    assert os.path.islink(link)


@pytest.mark.parametrize("kind", ["directory", "fifo", "dangling_symlink"])
def test_final_target_that_is_not_a_regular_file_is_refused(world, kind):
    world.nmh.mkdir(mode=0o700)
    target = world.nmh / MANIFEST_NAME
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(world.victim_dir / "not-there")
    with pytest.raises(state.StateError, match="refusing to replace"):
        world.install()
    assert not (world.victim_dir / "not-there").exists()


def test_final_launcher_and_record_symlinks_are_refused(world):
    state.ensure_private_dir(world.host_dir)
    (world.host_dir / installer.LAUNCHER_FILENAME).symlink_to(world.victim)
    with pytest.raises(state.StateError, match="refusing to replace"):
        world.install()
    os.unlink(world.host_dir / installer.LAUNCHER_FILENAME)
    (state.companion_dir(world.data_dir) / state.INSTALL_FILENAME).symlink_to(world.victim)
    with pytest.raises(state.StateError, match="refusing to replace"):
        world.install()


def test_final_file_owned_by_another_user_is_refused(world, monkeypatch):
    world.nmh.mkdir(mode=0o700)
    (world.nmh / MANIFEST_NAME).write_text("{}")
    real_stat = os.stat

    def foreign_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == MANIFEST_NAME and "dir_fd" in kwargs:
            fields = list(result[:10])
            fields[4] = os.getuid() + 1  # st_uid
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(state.os, "stat", foreign_stat)
    with pytest.raises(state.StateError, match="refusing to replace"):
        world.install()


@pytest.mark.parametrize("target", ["launcher", "manifest"])
def test_a_name_swapped_for_a_symlink_right_after_the_rename_is_never_chmod_ed_through(world, monkeypatch, target):
    """The mode is set on the new file's own descriptor BEFORE it is renamed
    into place -- never by path afterwards, which would follow a link swapped
    in at that instant -- and the post-rename re-verification refuses the
    swapped name."""
    real_replace = os.replace
    name = installer.LAUNCHER_FILENAME if target == "launcher" else MANIFEST_NAME

    def replace_then_swap(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        if dst == name:
            os.unlink(dst, dir_fd=kwargs["dst_dir_fd"])
            os.symlink(str(world.victim), dst, dir_fd=kwargs["dst_dir_fd"])
    monkeypatch.setattr(state.os, "replace", replace_then_swap)
    with pytest.raises(state.StateError, match="changed while it was being written"):
        world.install()
    # The fixture then proves the victim kept its bytes AND its 0600 mode.


# ── Parent components ────────────────────────────────────────────────────

def test_registration_dir_that_is_a_symlink_is_refused(world):
    world.nmh.symlink_to(world.victim_dir)  # a private dir of ours: every other check would pass
    with pytest.raises(state.StateError, match="without following a link"):
        world.install()
    assert os.listdir(world.victim_dir) == [world.victim.name]


def test_chrome_dir_redirected_into_an_attacker_writable_place_is_refused(world, tmp_path):
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    os.chmod(attacker, 0o777)
    os.rmdir(world.chrome)
    world.chrome.symlink_to(attacker)
    with pytest.raises(state.StateError, match="writable by other users"):
        world.install()


def test_relocated_chrome_profile_in_a_safe_place_is_supported(world, tmp_path):
    """A legitimately symlinked (relocated) Chrome user data dir works: the
    chain it resolves to is checked, and the write lands there."""
    relocated = tmp_path / "disk2" / "google-chrome"
    relocated.mkdir(parents=True)
    os.chmod(relocated, 0o700)
    os.rmdir(world.chrome)
    world.chrome.symlink_to(relocated)
    result = world.install()
    assert json.loads((relocated / "NativeMessagingHosts" / MANIFEST_NAME).read_text())["path"] == result["launcher"]


def test_private_dirs_that_are_symlinks_are_refused(world):
    state.ensure_private_dir(state.companion_dir(world.data_dir))
    world.host_dir.symlink_to(world.victim_dir)
    with pytest.raises(state.StateError):
        world.install()
    assert os.listdir(world.victim_dir) == [world.victim.name]


def test_directories_owned_by_someone_else_are_refused(world, monkeypatch):
    world.nmh.mkdir(mode=0o700)
    monkeypatch.setattr(state.os, "getuid", lambda: 4242)
    with pytest.raises(state.StateError, match="owned by another user"):
        state.open_trusted_dir(world.nmh, private=False)
    with pytest.raises(state.StateError, match="owned by another user"):
        world.install()


def test_trusted_chain_allows_root_and_sticky_ancestors_only(tmp_path):
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    mine = sticky / "mine"
    mine.mkdir(mode=0o700)
    fd = state.open_trusted_dir(mine / "a" / "b", create=True)  # /tmp-like parent: fine
    try:
        assert os.path.samestat(os.fstat(fd), os.stat(mine / "a" / "b"))
    finally:
        os.close(fd)
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o777)  # world-writable, NOT sticky: anyone could swap what is inside
    (loose / "mine").mkdir(mode=0o700)
    with pytest.raises(state.StateError, match="writable by other users"):
        state.open_trusted_dir(loose / "mine" / "x", create=True)
    assert not (loose / "mine" / "x").exists()


# ── Ordinary life under hostile conditions ───────────────────────────────

def test_permissive_umask_yields_exact_modes(world):
    old = os.umask(0)
    try:
        result = world.install()
        state.save_pairing(world.data_dir, instance_id=INSTANCE, extension_origin=ORIGIN)
    finally:
        os.umask(old)
    _assert_installed(world, result)
    assert _mode(world.nmh) == 0o700
    assert _mode(state.companion_dir(world.data_dir) / state.PAIRING_FILENAME) == 0o600


def test_reinstall_and_pair_rewrite_replace_atomically(world):
    first = world.install()
    manifest = world.nmh / MANIFEST_NAME
    inode = os.lstat(manifest).st_ino
    second = world.install()
    assert os.lstat(manifest).st_ino != inode, "replaced by rename, not rewritten in place"
    _assert_installed(world, second)
    assert first["launcher"] == second["launcher"]
    for instance in (INSTANCE, "fedcba9876543210fedcba9876543210", INSTANCE):
        state.save_pairing(world.data_dir, instance_id=instance, extension_origin=ORIGIN)
        assert state.load_pairing(world.data_dir).instance_id == instance


def test_partial_prior_install_is_completed_without_following_leftovers(world):
    world.nmh.mkdir(mode=0o700)
    state.ensure_private_dir(world.host_dir)
    leftover = world.nmh / f".{MANIFEST_NAME}.deadbeefdeadbeef.tmp"
    leftover.write_text("half-written")
    (world.host_dir / installer.HOST_CONFIG_FILENAME).write_text("{")  # torn from a crash
    os.chmod(world.host_dir / installer.HOST_CONFIG_FILENAME, 0o600)
    result = world.install()
    assert json.loads(Path(result["host_config"]).read_text())["extension_origin"] == ORIGIN
    assert leftover.read_text() == "half-written"


def test_concurrent_installs_never_corrupt_or_leak_temp_files(world):
    errors = []

    def run():
        try:
            world.install()
        except Exception as exc:  # any failure is reported below
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from chrome_companion import installer; "
            "installer.install(sys.argv[2], extension_id=sys.argv[3])")
    repo = str(Path(__file__).resolve().parents[1])
    procs = [subprocess.Popen([sys.executable, "-c", code, repo, str(world.data_dir), EXT_ID]) for _ in range(3)]
    for t in threads:
        t.join(30)
    assert all(p.wait(30) == 0 for p in procs)
    assert errors == []
    _assert_installed(world, installer.install(world.data_dir, extension_id=EXT_ID))


def test_paths_with_spaces_install_and_launch(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    config = home / "my config"
    (config / "google-chrome").mkdir(parents=True)
    os.chmod(config / "google-chrome", 0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    data_dir = tmp_path / "lumina data dir"
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        result = installer.install(data_dir, extension_id=EXT_ID)
        assert " " in result["launcher"] and " " in result["host_manifest"]
        proc = subprocess.run([result["launcher"], "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba/"],
                              input=b"", capture_output=True, timeout=10, check=False)
        assert proc.returncode == 2 and b"rejected" in proc.stderr, "the quoted launcher really runs the host"


# ── Uninstall ────────────────────────────────────────────────────────────

def test_uninstall_never_follows_or_blocks(world):
    result = world.install()
    manifest = world.nmh / MANIFEST_NAME
    ours = manifest.read_text()
    os.unlink(manifest)
    decoy = world.victim_dir / "decoy.json"
    decoy.write_text(ours)  # names OUR launcher -- would pass the content check
    manifest.symlink_to(decoy)
    installer.uninstall(world.data_dir)
    assert os.path.islink(manifest) and decoy.read_text() == ours, "a symlinked manifest is left alone"
    os.unlink(manifest)
    os.mkfifo(manifest)
    done = threading.Event()
    threading.Thread(target=lambda: (installer.uninstall(world.data_dir), done.set()), daemon=True).start()
    assert done.wait(5), "uninstall blocked opening a planted FIFO"
    assert stat.S_ISFIFO(os.lstat(manifest).st_mode)
    del result


def test_uninstall_leaves_a_manifest_in_a_hostile_directory(world):
    result = world.install()
    os.chmod(world.nmh, 0o777)
    removed = installer.uninstall(world.data_dir)
    assert (world.nmh / MANIFEST_NAME).exists() and str(world.nmh / MANIFEST_NAME) not in removed
    assert result["launcher"] in removed


def test_uninstall_removes_only_our_own_regular_manifest(world):
    result = world.install()
    removed = installer.uninstall(world.data_dir)
    assert result["host_manifest"] in removed and not (world.nmh / MANIFEST_NAME).exists()


# ── The owner-facing error ───────────────────────────────────────────────

def test_cli_reports_a_hostile_directory_cleanly_without_leaking_link_targets(world, tmp_path):
    world.nmh.mkdir(mode=0o700)
    (world.nmh / MANIFEST_NAME).symlink_to(world.victim)
    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err):
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            code = companion_cli.main(["--data-dir", str(world.data_dir), "install", "--extension-id", EXT_ID])
    assert code == 1
    assert err.getvalue().startswith("error: refusing to replace")
    assert "CANARY" not in err.getvalue() + out.getvalue(), "the planted link's target is never echoed"
