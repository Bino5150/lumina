"""BROWSER-COMPANION-01A-R3 / B3-R3 -- the installer trusts directory OBJECTS,
never pathnames.

Goblin's AR3: the R2 installer validated realpath(profile) and then reopened
the ORIGINAL path; O_NOFOLLOW guarded only the last component, so a profile
symlink swapped in between (its parent was a 0777 directory) redirected the
write and clobbered an unrelated 0600 file. Law now: every directory is
reached by a walk from "/" that opens one component at a time relative to the
descriptor before it (O_DIRECTORY|O_NOFOLLOW) and checks the object opened;
the installer writes only through the descriptor it holds. A component
replaced after it was opened cannot move a write; a symlink is followed only
where nobody else can replace it; a hostile chain is refused.

Every test holds an unrelated canary -- a 0600 file named exactly like the
host manifest, in a Chrome-profile-shaped directory -- and proves its bytes,
mode AND inode unchanged (fixture teardown). Everything runs in pytest's tmp
dir; ownership by other users is simulated (stat results), never real."""
from __future__ import annotations

import io
import json
import os
import stat
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from chrome_companion import installer, native_host, protocol, state
from chrome_companion_testkit import EXT_ID, INSTANCE, short_tmpdir
from test_chrome_companion_installer import companion_cli  # the real CLI module

MANIFEST_NAME = f"{protocol.HOST_NAME}.json"
ORIGIN = f"chrome-extension://{EXT_ID}/"
CANARY = b"CANARY-AR3-UNRELATED\n"
REAL_OPEN = os.open


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class Lab:
    pass


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Goblin's layout: a private synthetic data dir; a SAFE private Chrome dir
    (where a write may land); and an UNRELATED private directory shaped like a
    Chrome profile whose NativeMessagingHosts holds a 0600 canary under the
    manifest's own name (where a redirected write would land)."""
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    lab = Lab()
    lab.tmp = tmp_path
    lab.data_dir = tmp_path / "data"
    lab.data_dir.mkdir(mode=0o700)
    lab.safe = tmp_path / "safe-chrome"
    lab.safe.mkdir(mode=0o700)
    lab.unrelated = tmp_path / "unrelated"
    (lab.unrelated / "NativeMessagingHosts").mkdir(parents=True)
    os.chmod(lab.unrelated, 0o700)
    os.chmod(lab.unrelated / "NativeMessagingHosts", 0o700)
    lab.canary = lab.unrelated / "NativeMessagingHosts" / MANIFEST_NAME
    lab.canary.write_bytes(CANARY)
    os.chmod(lab.canary, 0o600)
    inode = os.lstat(lab.canary).st_ino
    lab.install = lambda chrome_dir: installer.install(lab.data_dir, extension_id=EXT_ID, chrome_dir=chrome_dir)
    with short_tmpdir() as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        yield lab
    assert lab.canary.read_bytes() == CANARY, "the unrelated canary's bytes changed"
    assert _mode(lab.canary) == 0o600, "the unrelated canary's mode changed"
    assert os.lstat(lab.canary).st_ino == inode, "the unrelated canary was replaced"
    assert sorted(os.listdir(lab.canary.parent)) == [MANIFEST_NAME], "something was written next to the canary"


def _manifest_in(directory: Path) -> dict:
    return json.loads((directory / "NativeMessagingHosts" / MANIFEST_NAME).read_text())


def _assert_landed(chrome_dir: Path, result: dict) -> None:
    manifest = chrome_dir / "NativeMessagingHosts" / MANIFEST_NAME
    assert stat.S_ISREG(os.lstat(manifest).st_mode) and _mode(manifest) == 0o600
    assert json.loads(manifest.read_text())["path"] == result["launcher"]
    assert not [n for n in os.listdir(manifest.parent) if n.endswith(".tmp")]


# ── Goblin's AR3 reproduction, now blocking ──────────────────────────────

def test_goblin_ar3_parent_symlink_swapped_after_validation_in_a_0777_parent(lab, monkeypatch):
    """AR3 verbatim: the profile is a symlink inside a mode-0777 parent (any
    local user could replace it), initially pointing at a private, safe Chrome
    dir. The directory-validation step runs for real and then the link is
    swapped to the unrelated profile before it returns. R2 reported success
    and overwrote the canary; R3 refuses the chain before anything is written,
    because the directory holding the link is writable by other users."""
    loose = lab.tmp / "loose"
    loose.mkdir()
    os.chmod(loose, 0o777)
    link = loose / "profile"
    link.symlink_to(lab.safe)
    real = state.open_trusted_dir
    swapped = []

    def validate_then_swap(path, **kwargs):
        try:
            return real(path, **kwargs)
        finally:
            if Path(path) == link / "NativeMessagingHosts" and not swapped:
                os.unlink(link)
                link.symlink_to(lab.unrelated)
                swapped.append(True)
    monkeypatch.setattr(state, "open_trusted_dir", validate_then_swap)
    with pytest.raises(state.StateError, match="writable by other users"):
        lab.install(link)
    assert swapped, "the swap ran at the validation point"
    assert not (lab.safe / "NativeMessagingHosts").exists(), "refused before creating anything"


def test_ar3_the_same_swap_in_a_trusted_parent_cannot_move_the_write(lab, monkeypatch):
    """The legitimate shape -- a relocated profile, linked from a private
    directory -- is supported, and anchored: swapping that link right after the
    registry directory was opened does not move the write. The manifest lands
    in the directory object the installer opened and checked."""
    home = lab.tmp / "home"
    home.mkdir(mode=0o700)
    link = home / "google-chrome"
    link.symlink_to(lab.safe)
    real = state.open_trusted_dir

    def open_then_swap(path, **kwargs):
        fd = real(path, **kwargs)
        if Path(path) == link / "NativeMessagingHosts":
            os.unlink(link)
            link.symlink_to(lab.unrelated)
        return fd
    monkeypatch.setattr(state, "open_trusted_dir", open_then_swap)
    result = lab.install(link)
    _assert_landed(lab.safe, result)


# ── A component replaced after the walk opened it ────────────────────────

def _assert_registry(registry: Path, result: dict) -> None:
    manifest = registry / MANIFEST_NAME
    assert stat.S_ISREG(os.lstat(manifest).st_mode) and _mode(manifest) == 0o600
    assert json.loads(manifest.read_text())["path"] == result["launcher"]
    assert not [n for n in os.listdir(registry) if n.endswith(".tmp")]


@pytest.mark.parametrize("which", ["first", "middle", "final", "registry"])
def test_a_component_swapped_right_after_it_was_opened_cannot_redirect_the_write(lab, monkeypatch, which):
    """Chain <tmp>/p1/p2/google-chrome/NativeMessagingHosts, all private. The
    moment the walk opens the chosen component, that component is renamed away
    and replaced by a link whose path leads to the canary's directory (for an
    ancestor: through a decoy tree ending in a link to the unrelated profile).
    A path-based reopen would now land on the canary; the walk continues from
    the descriptor it holds, so the manifest lands in the ORIGINAL object."""
    names = ["p1", "p2", "google-chrome", "NativeMessagingHosts"]
    level = lab.tmp
    for name in names:
        level = level / name
        level.mkdir(mode=0o700)
    target = {"first": "p1", "middle": "p2", "final": "google-chrome", "registry": "NativeMessagingHosts"}[which]
    index = names.index(target)
    swapped = lab.tmp.joinpath(*names[:index + 1])
    if target == "NativeMessagingHosts":
        replacement = lab.unrelated / "NativeMessagingHosts"
    elif target == "google-chrome":
        replacement = lab.unrelated
    else:
        replacement = lab.tmp / "decoy"
        level = replacement
        level.mkdir(mode=0o700)
        for name in names[index + 1:2]:
            level = level / name
            level.mkdir(mode=0o700)
        (level / "google-chrome").symlink_to(lab.unrelated)
    assert (swapped.parent / target).exists()
    done = []

    def open_then_swap(path, flags, *args, **kwargs):
        fd = REAL_OPEN(path, flags, *args, **kwargs)
        if not done and path == target and kwargs.get("dir_fd") is not None and flags & os.O_DIRECTORY:
            os.rename(swapped, swapped.with_name(target + ".orig"))
            swapped.symlink_to(replacement)
            done.append(True)
        return fd
    monkeypatch.setattr(os, "open", open_then_swap)
    try:
        result = lab.install(lab.tmp.joinpath(*names[:3]))
    finally:
        monkeypatch.setattr(os, "open", REAL_OPEN)
    assert done, f"the {which} component was swapped mid-walk"
    original = lab.tmp.joinpath(*names[:index], target + ".orig", *names[index + 1:])
    _assert_registry(original, result)
    # ...and the path as it reads NOW really would have led to the canary:
    now = lab.tmp.joinpath(*names)
    assert os.path.samefile(now, lab.unrelated / "NativeMessagingHosts")


def test_replacing_the_profile_link_while_the_install_is_paused_changes_nothing(lab, monkeypatch):
    """The install is held (a real second thread) after it opened the registry
    directory; meanwhile the relocated-profile link is re-pointed at the
    unrelated profile; then the install resumes and writes. It writes where it
    opened, not where the path now leads."""
    home = lab.tmp / "home"
    home.mkdir(mode=0o700)
    link = home / "google-chrome"
    link.symlink_to(lab.safe)
    real = state.open_trusted_dir
    held, resume = threading.Event(), threading.Event()

    def open_then_hold(path, **kwargs):
        fd = real(path, **kwargs)
        if Path(path) == link / "NativeMessagingHosts":
            held.set()
            assert resume.wait(10)
        return fd
    monkeypatch.setattr(state, "open_trusted_dir", open_then_hold)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("result", lab.install(link)))
    worker.start()
    assert held.wait(10)
    os.unlink(link)
    link.symlink_to(lab.unrelated)
    resume.set()
    worker.join(10)
    _assert_landed(lab.safe, outcome["result"])


# ── Which chains are trusted ─────────────────────────────────────────────

def _chain(lab, modes: list[int]) -> Path:
    level = lab.tmp / "chain"
    level.mkdir(mode=0o700)
    for i, mode in enumerate(modes):
        level = level / f"d{i}"
        level.mkdir()
        os.chmod(level, mode)
    return level


@pytest.mark.parametrize("chrome_ran", [False, True])
@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("mode", [0o777, 0o773, 0o757, 0o770])
def test_a_directory_others_can_write_anywhere_on_the_chain_is_refused(lab, monkeypatch, position, mode, chrome_ran):
    """First, middle or last ancestor of the Chrome dir: world- or (non-private)
    group-writable and not sticky means someone else could replace what is
    inside it. Refused -- whether the levels below already exist (Chrome has
    run: only the per-step check of each opened object can see it) or have
    to be created (never created inside such a directory)."""
    monkeypatch.setattr(state, "_private_group", lambda gid: False)
    modes = [0o700, 0o700, 0o700]
    modes[position] = mode
    top = _chain(lab, modes)
    if chrome_ran:
        (top / "google-chrome" / "NativeMessagingHosts").mkdir(parents=True)
        os.chmod(top / "google-chrome", 0o700)
        os.chmod(top / "google-chrome" / "NativeMessagingHosts", 0o700)
    with pytest.raises(state.StateError, match="writable by other users"):
        lab.install(top / "google-chrome")
    if chrome_ran:
        assert os.listdir(top / "google-chrome" / "NativeMessagingHosts") == []
    else:
        assert not (top / "google-chrome").exists()
    assert not (lab.data_dir / "chrome_companion" / "install.json").exists()


def _disguise_owner(monkeypatch, path: Path, owner: int) -> None:
    inode = os.lstat(path).st_ino
    real_fstat, real_stat = os.fstat, os.stat

    def disguise(result):
        if result.st_ino != inode:
            return result
        fields = list(result[:10])
        fields[4] = owner
        return os.stat_result(fields)
    monkeypatch.setattr(os, "fstat", lambda fd: disguise(real_fstat(fd)))
    monkeypatch.setattr(os, "stat", lambda *a, **k: disguise(real_stat(*a, **k)))


def test_the_directory_written_into_must_be_our_own_not_just_roots(lab, monkeypatch):
    """Root may own the directories on the way (/, /home), but the directory
    the installer writes into must be this user's. (Root ownership simulated.)"""
    registry = lab.safe / "NativeMessagingHosts"
    registry.mkdir(mode=0o700)
    _disguise_owner(monkeypatch, registry, 0)
    with pytest.raises(state.StateError, match="not owned by current user"):
        lab.install(lab.safe)
    assert os.listdir(registry) == []


@pytest.mark.parametrize("level", ["chrome_companion", "native_host"])
@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755])
def test_lumina_private_dirs_open_to_anyone_else_are_refused_not_repaired(lab, level, mode):
    companion = lab.data_dir / "chrome_companion"
    (companion / "native_host").mkdir(parents=True)
    os.chmod(companion, 0o700)
    os.chmod(companion / "native_host", 0o700)
    target = companion if level == "chrome_companion" else companion / "native_host"
    os.chmod(target, mode)
    with pytest.raises(state.StateError, match="group/world accessible"):
        lab.install(lab.safe)
    assert _mode(target) == mode


def test_a_sticky_world_writable_parent_is_accepted_for_entries_root_or_we_own(lab):
    sticky = _chain(lab, [0o1777])
    (sticky / "mine").mkdir(mode=0o700)
    result = lab.install(sticky / "mine" / "google-chrome")
    _assert_landed(sticky / "mine" / "google-chrome", result)


@pytest.mark.parametrize("who", ["entry", "sticky_dir"])
def test_a_sticky_parent_does_not_protect_an_entry_someone_else_owns(lab, monkeypatch, who):
    """In a sticky directory other users can create -- and keep -- their own
    entries; and the sticky directory's own owner can replace anything in it.
    So a sticky parent is trusted only if IT is ours or root's, and only for
    entries that are ours or root's. (Foreign ownership simulated.)"""
    sticky = _chain(lab, [0o1777])
    (sticky / "theirs").mkdir(mode=0o755)
    _disguise_owner(monkeypatch, sticky / "theirs" if who == "entry" else sticky, os.getuid() + 1)
    with pytest.raises(state.StateError, match="owned by another user|writable by other users"):
        lab.install(sticky / "theirs" / "google-chrome")


def test_a_private_owner_chain_is_accepted(lab):
    top = _chain(lab, [0o700, 0o755, 0o750])
    _assert_landed(top / "google-chrome", lab.install(top / "google-chrome"))


def test_a_symlink_is_followed_only_where_nobody_else_can_replace_it(lab):
    """A link owned by us, in a directory only we can write, is followed (and
    anchored); the identical link in a directory others can write is not."""
    safe_home = _chain(lab, [0o700])
    (safe_home / "google-chrome").symlink_to(lab.safe)
    _assert_landed(lab.safe, lab.install(safe_home / "google-chrome"))
    loose = lab.tmp / "loose-home"
    loose.mkdir()
    os.chmod(loose, 0o777)
    (loose / "google-chrome").symlink_to(lab.unrelated)
    with pytest.raises(state.StateError, match="writable by other users"):
        lab.install(loose / "google-chrome")


def test_relative_links_and_dot_dot_resolve_physically(lab):
    top = _chain(lab, [0o700, 0o700])
    (top / "real").mkdir(mode=0o700)
    (top / "link").symlink_to("../d1/real")  # relative, with ..
    _assert_landed(top / "real", lab.install(top / "link"))


def test_a_link_loop_is_refused(lab):
    top = _chain(lab, [0o700])
    (top / "a").symlink_to("b")
    (top / "b").symlink_to("a")
    with pytest.raises(state.StateError, match="too many symbolic links"):
        lab.install(top / "a" / "google-chrome")


# ── The Debian user-private-group exception, reviewed ────────────────────

class _Group:
    def __init__(self, gid, members):
        self.gr_gid, self.gr_mem, self.gr_name = gid, members, f"g{gid}"


class _User:
    def __init__(self, name, uid, gid):
        self.pw_name, self.pw_uid, self.pw_gid = name, uid, gid


@pytest.mark.parametrize("case, expected", [
    ("primary_member_is_only_me", True),
    ("listed_member_is_only_me", True),
    ("no_members_at_all", False),          # setgid-program groups: R2 accepted these
    ("another_user_has_it_as_primary", False),
    ("another_user_is_listed", False),
    ("unknown_group", False),
])
def test_private_group_rule(monkeypatch, case, expected):
    """Group write counts as ours only if the group PROVABLY has exactly one
    member, this user (Debian OpenSSH user-group-modes). A group with no
    members is not private: setgid programs run by anyone hold it."""
    import grp
    import pwd
    uid, gid = os.getuid(), 54321
    me = _User("me", uid, 1000 if case != "primary_member_is_only_me" else gid)
    users = [me, _User("other", uid + 1, gid if case == "another_user_has_it_as_primary" else 7)]
    members = {"listed_member_is_only_me": ["me"], "another_user_is_listed": ["me", "other"]}.get(case, [])

    def getgrgid(g):
        if case == "unknown_group" or g != gid:
            raise KeyError(g)
        return _Group(gid, members)
    monkeypatch.setattr(grp, "getgrgid", getgrgid)
    monkeypatch.setattr(pwd, "getpwall", lambda: users)
    monkeypatch.setattr(pwd, "getpwuid", lambda u: me if u == uid else (_ for _ in ()).throw(KeyError(u)))
    assert state._private_group(gid) is expected


def test_a_group_writable_parent_is_trusted_only_through_a_proven_private_group(lab, monkeypatch):
    top = _chain(lab, [0o775])
    monkeypatch.setattr(state, "_private_group", lambda gid: True)
    _assert_landed(top / "google-chrome", lab.install(top / "google-chrome"))
    monkeypatch.setattr(state, "_private_group", lambda gid: False)
    with pytest.raises(state.StateError, match="writable by other users"):
        lab.install(top / "google-chrome")


# ── Reads, pairing and removal are anchored too ──────────────────────────

def test_state_reads_through_an_untrusted_chain_are_refused(lab):
    """A data dir reached through a link in a directory others can write is
    not read -- an attacker there could hand Lumina a forged install record,
    pairing or host config (socket path, extension origin)."""
    lab.install(lab.safe)
    loose = lab.tmp / "loose-data"
    loose.mkdir()
    os.chmod(loose, 0o777)
    (loose / "via").symlink_to(lab.tmp)  # an ANCESTOR link, in a directory others can write
    for load in (state.load_install, state.load_pairing):
        with pytest.raises(state.StateError, match="writable by other users"):
            load(loose / "via" / "data")
    config = loose / "via" / "data" / "chrome_companion" / "native_host" / installer.HOST_CONFIG_FILENAME
    with pytest.raises(state.StateError, match="writable by other users"):
        native_host.load_config(config)
    assert state.load_install(lab.data_dir).extension_origin == ORIGIN, "the real path still reads"
    relocated = lab.tmp / "home"
    relocated.mkdir(mode=0o700)
    (relocated / "lumina-data").symlink_to(lab.data_dir)  # a relocated data dir, linked from a safe place
    assert state.load_install(relocated / "lumina-data").extension_origin == ORIGIN


def test_a_state_file_swapped_for_a_link_between_check_and_read_is_never_read(lab, monkeypatch):
    """R2 checked the record with lstat, then reopened it BY NAME. Now it is
    opened once (O_NOFOLLOW) and checked on its own descriptor."""
    lab.install(lab.safe)
    record = lab.data_dir / "chrome_companion" / state.INSTALL_FILENAME
    forged = lab.tmp / "forged.json"
    forged.write_text(json.dumps({"version": 1, "extension_origin": "chrome-extension://"
                                  + "p" * 32 + "/", "socket_path": "/tmp/evil.sock", "installed_at": 0}))
    os.chmod(forged, 0o600)
    real_lstat = os.lstat

    def lstat_then_swap(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == record:
            os.unlink(record)
            record.symlink_to(forged)
        return result
    monkeypatch.setattr(os, "lstat", lstat_then_swap)
    record.unlink()
    record.symlink_to(forged)
    with pytest.raises(state.StateError, match="not a regular file"):
        state.load_install(lab.data_dir)


def test_uninstall_never_removes_through_a_linked_private_dir(lab):
    """R2 removed the launcher by PATH, so a native_host dir replaced by a link
    let uninstall delete a same-named file wherever the link pointed."""
    lab.install(lab.safe)
    host_dir = installer.native_host_dir(lab.data_dir)
    elsewhere = lab.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    bystander = elsewhere / installer.LAUNCHER_FILENAME
    bystander.write_bytes(CANARY)
    os.chmod(bystander, 0o600)
    moved = host_dir.with_name("native_host.moved")
    os.rename(host_dir, moved)
    host_dir.symlink_to(elsewhere)
    removed = installer.uninstall(lab.data_dir, chrome_dir=lab.safe)
    assert bystander.read_bytes() == CANARY
    assert str(host_dir / installer.LAUNCHER_FILENAME) not in removed
    assert str(lab.safe / "NativeMessagingHosts" / MANIFEST_NAME) in removed


def test_pair_and_unpair_go_through_the_anchored_companion_dir(lab):
    lab.install(lab.safe)
    state.save_pairing(lab.data_dir, instance_id=INSTANCE, extension_origin=ORIGIN)
    assert state.load_pairing(lab.data_dir).instance_id == INSTANCE
    pairing = lab.data_dir / "chrome_companion" / state.PAIRING_FILENAME
    pairing.unlink()
    pairing.symlink_to(lab.canary)
    assert state.clear_pairing(lab.data_dir) is True, "the link itself is removed"
    assert not os.path.lexists(pairing)


# ── Ordinary life ────────────────────────────────────────────────────────

def test_parallel_installs_against_a_link_the_owner_keeps_repointing(lab):
    """The owner re-points a relocated-profile link between two safe profiles
    while 8 installs run. Each install lands wholly in ONE of them (the one it
    opened); none fails, leaks a temp, or touches the canary."""
    other = lab.tmp / "safe-chrome-2"
    other.mkdir(mode=0o700)
    home = _chain(lab, [0o700])
    link = home / "google-chrome"
    link.symlink_to(lab.safe)
    stop = threading.Event()

    def flip():
        targets = [other, lab.safe]
        i = 0
        while not stop.is_set():
            tmp = home / f".link{i}"
            tmp.symlink_to(targets[i % 2])
            os.replace(tmp, link)
            i += 1
    flipper = threading.Thread(target=flip)
    flipper.start()
    errors, results = [], []

    def run():
        try:
            results.append(lab.install(link))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    stop.set()
    flipper.join(10)
    assert errors == [] and len(results) == 8
    landed = [d for d in (lab.safe, other) if (d / "NativeMessagingHosts" / MANIFEST_NAME).exists()]
    assert landed, "every install landed in a safe profile"
    for d in landed:
        _assert_landed(d, results[0])


@pytest.mark.parametrize("failing", ["write", "fsync", "replace"])
def test_a_failed_write_leaves_the_old_file_and_no_temp(lab, monkeypatch, failing):
    first = lab.install(lab.safe)
    manifest = lab.safe / "NativeMessagingHosts" / MANIFEST_NAME
    before = (manifest.read_bytes(), os.lstat(manifest).st_ino)
    real = getattr(os, failing)

    def boom(*args, **kwargs):
        if failing == "replace" and args[1] != MANIFEST_NAME:
            return real(*args, **kwargs)
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, failing, boom)
    with pytest.raises(OSError):
        lab.install(lab.safe)
    monkeypatch.setattr(os, failing, real)
    assert (manifest.read_bytes(), os.lstat(manifest).st_ino) == before
    for directory in (manifest.parent, installer.native_host_dir(lab.data_dir),
                      lab.data_dir / "chrome_companion"):
        assert not [n for n in os.listdir(directory) if n.endswith(".tmp")], directory
    del first


def test_reinstall_pair_status_uninstall_cycle(lab):
    first = lab.install(lab.safe)
    second = lab.install(lab.safe)
    assert first["launcher"] == second["launcher"]
    state.save_pairing(lab.data_dir, instance_id=INSTANCE, extension_origin=ORIGIN)
    removed = installer.uninstall(lab.data_dir, chrome_dir=lab.safe)
    assert len(removed) == 5
    assert state.load_install(lab.data_dir) is None and state.load_pairing(lab.data_dir) is None


def test_cli_refusal_names_no_link_target_and_no_file_contents(lab, monkeypatch):
    loose = lab.tmp / "loose-config"
    loose.mkdir()
    os.chmod(loose, 0o777)
    (loose / "google-chrome").symlink_to(lab.unrelated)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(loose))
    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        code = companion_cli.main(["--data-dir", str(lab.data_dir), "install", "--extension-id", EXT_ID])
    assert code == 1
    text = err.getvalue() + out.getvalue()
    assert "writable by other users" in text
    assert "CANARY" not in text and "unrelated" not in text
