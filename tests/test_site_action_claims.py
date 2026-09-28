"""Real-filesystem immutable claim storage: races, crashes and hostile paths.

No kernel, grant, browser action or owner decision is manufactured here.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest

from chrome_companion.site_actions import claims as c
from chrome_companion.site_actions.model import Runtime


ROOT = Path(__file__).resolve().parents[1]
KEY = c.EventKey.from_ingress("desktop/owner/session/chat", "stable-owner-event-123")
ADMISSION = c.AdmissionMetadata("a" * 64, "b" * 64, 100, 300_000_000_100)
BINDING = c.BindingMetadata("c" * 64, "d" * 64, "e" * 64, "f" * 64, Runtime.CHROME_COMPANION)


def _ledger(data):
    return data / "chrome_companion" / c.STORE_DIR


def _path(data, kind=c.RecordKind.SPENT):
    return _ledger(data) / (KEY.digest + "." + kind.value + ".json")


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    identity = c.initialize_store(data)
    with c.ClaimStore(data, identity) as store:
        assert store.admit(KEY, ADMISSION)
        assert store.bind(KEY, BINDING)
    return data, identity


def _process_contender(data, identity, barrier, output):
    try:
        with c.ClaimStore(data, identity) as store:
            barrier.wait(timeout=20)
            output.put(store.consume(KEY, BINDING))
    except Exception as exc:
        output.put(type(exc).__name__ + ": " + str(exc))


def test_explicit_bootstrap_identity_and_modes(world):
    data, identity = world
    assert (identity.store_device, identity.store_inode) == (
        _ledger(data).stat().st_dev, _ledger(data).stat().st_ino)
    reference = data / "chrome_companion" / c.REFERENCE_FILE
    marker = _ledger(data) / c.IDENTITY_FILE
    assert reference.read_bytes() == marker.read_bytes() == c.canonical_json(identity.to_data())
    for path in (data / "chrome_companion", _ledger(data)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (reference, marker, _path(data, c.RecordKind.ADMISSION), _path(data, c.RecordKind.BINDING)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_nlink == 1
    with pytest.raises(FrozenInstanceError):
        identity.epoch = "0" * 64
    with pytest.raises(c.ClaimStoreError):
        c.initialize_store(data)


def test_opening_missing_store_or_missing_identity_never_creates(tmp_path, world):
    data, identity = world
    missing = tmp_path / "missing"
    with pytest.raises(c.ClaimStoreError):
        c.ClaimStore(missing, identity)
    assert not missing.exists()
    with pytest.raises(c.ClaimStoreError):
        c.ClaimStore(data, None)
    with pytest.raises(c.ClaimStoreError):
        c.ClaimStore(data, replace(identity, epoch="0" * 64))


def test_admission_binding_consumption_are_immutable_and_linked(world):
    data, identity = world
    before = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in _ledger(data).iterdir()}
    with c.ClaimStore(data, identity) as store:
        assert store.admit(KEY, ADMISSION) is False
        assert store.bind(KEY, BINDING) is False
        admission = store.inspect(KEY, c.RecordKind.ADMISSION)
        binding = store.inspect(KEY, c.RecordKind.BINDING)
        assert binding.parent_digest == admission.digest
        assert store.consume(KEY, BINDING) is True
        spent = store.inspect(KEY, c.RecordKind.SPENT)
        assert spent.parent_digest == binding.digest and spent.metadata is None
        assert store.consume(KEY, BINDING) is False
    for name, receipt in before.items():
        p = _ledger(data) / name
        assert (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) == receipt
    original_spent = _path(data).read_bytes()
    with c.ClaimStore(data, identity) as reopened:
        assert reopened.consume(KEY, BINDING) is False
    assert _path(data).read_bytes() == original_spent


@pytest.mark.parametrize("change", ["intent", "boot", "expiry", "specification", "manifest", "principal", "context"])
def test_event_slot_cannot_be_reminted_by_changing_metadata(world, change):
    data, identity = world
    original = {p.name: p.read_bytes() for p in _ledger(data).iterdir()}
    with c.ClaimStore(data, identity) as store:
        with pytest.raises(c.ClaimStoreError):
            if change == "intent":
                store.admit(KEY, replace(ADMISSION, intent_digest="0" * 64))
            elif change == "boot":
                store.admit(KEY, replace(ADMISSION, boot_digest="0" * 64))
            elif change == "expiry":
                store.admit(KEY, replace(ADMISSION, expires_ns=ADMISSION.expires_ns + 1))
            else:
                fields = {"specification": "specification_digest", "manifest": "manifest_fingerprint",
                          "principal": "principal_digest", "context": "context_digest"}
                store.bind(KEY, replace(BINDING, **{fields[change]: "0" * 64}))
    assert {p.name: p.read_bytes() for p in _ledger(data).iterdir()} == original


def test_no_expiry_or_generic_idempotency_operation_revives_consumption(world, monkeypatch, tmp_path):
    data, identity = world
    with c.ClaimStore(data, identity) as store:
        assert store.consume(KEY, BINDING)
    spent = _path(data)
    before = (spent.read_bytes(), spent.stat().st_ino)
    os.utime(spent, (1, 1))  # arbitrarily old file timestamps are not a revival policy
    from core import idempotency
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "generic-ledger.db"))
    assert idempotency.claim(KEY.digest)
    idempotency.release(KEY.digest)
    idempotency.record(KEY.digest, "unrelated cached result")
    assert idempotency.check(KEY.digest, ttl_hours=0) is None
    with c.ClaimStore(data, identity) as store:
        assert store.consume(KEY, BINDING) is False
    assert (spent.read_bytes(), spent.stat().st_ino) == before
    assert not any(hasattr(c.ClaimStore, name) for name in (
        "release", "delete", "reset", "repair", "prune", "refresh", "cleanup"))


def test_raw_owner_content_and_authority_are_not_persisted(tmp_path):
    data = tmp_path / "private"
    data.mkdir(mode=0o700)
    identity = c.initialize_store(data)
    raw = {"event": "owner-event-secret-7920", "text": "private exact body secret",
           "url": "https://example.test/private?secret=7920", "account": "private-account-7920",
           "bearer": "private-authority-handle-7920"}
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    key = c.EventKey.from_ingress("private-ingress-7920", raw["event"])
    admission = replace(ADMISSION, intent_digest=digest(raw["text"]))
    binding = replace(BINDING, specification_digest=digest(raw["url"] + raw["text"]),
                      principal_digest=digest(raw["account"]), context_digest=digest(raw["bearer"]))
    with c.ClaimStore(data, identity) as store:
        assert store.admit(key, admission) and store.bind(key, binding) and store.consume(key, binding)
    stored = b"".join(p.read_bytes() for p in data.rglob("*") if p.is_file())
    for value in (*raw.values(), "private-ingress-7920"):
        assert value.encode() not in stored
    assert not any("ledger.db" in str(p) for p in data.rglob("*"))


def test_threads_using_independent_handles_have_one_winner(world):
    data, identity = world
    barrier = threading.Barrier(8)
    def contender(_):
        with c.ClaimStore(data, identity) as store:
            barrier.wait(timeout=20)
            return store.consume(KEY, BINDING)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(contender, range(8)))
    assert results.count(True) == 1 and results.count(False) == 7


def test_spawned_processes_have_one_durable_winner(world):
    data, identity = world
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    output = context.Queue()
    processes = [context.Process(target=_process_contender, args=(data, identity, barrier, output)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        results = [output.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert results.count(True) == 1 and results.count(False) == 3, results
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        output.close()
    with c.ClaimStore(data, identity) as store:
        assert store.consume(KEY, BINDING) is False


def test_result_cannot_precede_file_then_directory_sync(world, monkeypatch):
    data, identity = world
    reached, release = threading.Event(), threading.Event()
    real_sync = os.fsync
    events = []
    def syncing(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            assert json.loads(_path(data).read_bytes())["kind"] == "spent"
            events.append("file")
        else:
            events.append("directory")
            reached.set()
            assert release.wait(timeout=20)
        real_sync(fd)
    monkeypatch.setattr(c.os, "fsync", syncing)
    with c.ClaimStore(data, identity) as store, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.consume, KEY, BINDING)
        try:
            assert reached.wait(timeout=20)
            assert not future.done()
        finally:
            release.set()
        assert future.result(timeout=20) is True
    assert events == ["file", "directory"]


@pytest.mark.parametrize("phase", ["zero_write", "partial_write", "file_sync", "directory_sync"])
def test_write_and_sync_uncertainty_retains_obstruction_across_reopen(world, monkeypatch, phase):
    data, identity = world
    real_write, real_sync = os.write, os.fsync
    calls = 0
    def write(fd, buffer):
        nonlocal calls
        calls += 1
        if phase == "zero_write":
            return 0
        if phase == "partial_write":
            if calls == 1:
                return real_write(fd, buffer[:17])
            raise OSError("injected partial-write failure")
        return real_write(fd, buffer)
    def sync(fd):
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        if (phase == "file_sync" and regular) or (phase == "directory_sync" and not regular):
            raise OSError("injected required-sync failure")
        real_sync(fd)
    with c.ClaimStore(data, identity) as store:
        with monkeypatch.context() as patch:
            patch.setattr(c.os, "write", write)
            patch.setattr(c.os, "fsync", sync)
            with pytest.raises(c.ClaimStoreError):
                store.consume(KEY, BINDING)
        assert _path(data).exists()
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)  # same handle remains uncertain
    before = (_path(data).read_bytes(), _path(data).stat().st_ino)
    with c.ClaimStore(data, identity) as reopened:
        assert reopened.consume(KEY, BINDING) is False
    assert (_path(data).read_bytes(), _path(data).stat().st_ino) == before


def test_short_and_interrupted_writes_finish_exactly(world, monkeypatch):
    data, identity = world
    real_write = os.write
    calls = 0
    def write(fd, buffer):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError()
        return real_write(fd, buffer[:7])
    monkeypatch.setattr(c.os, "write", write)
    with c.ClaimStore(data, identity) as store:
        assert store.consume(KEY, BINDING)
        assert _path(data).read_bytes() == store.inspect(KEY, c.RecordKind.SPENT).canonical_bytes
    assert calls > 2


@pytest.mark.parametrize("phase,exit_code", [("created", 71), ("partial", 72), ("file_synced", 73), ("directory_synced", 74)])
def test_native_crash_after_possible_creation_never_reopens_commit_slot(world, phase, exit_code):
    data, identity = world
    script = r'''
import json, os, stat, sys
sys.path.insert(0, sys.argv[1])
from chrome_companion.site_actions import claims as c
from chrome_companion.site_actions.model import Runtime
data, identity, phase, exit_code = sys.argv[2], c.StoreIdentity.from_data(json.loads(sys.argv[3])), sys.argv[4], int(sys.argv[5])
key=c.EventKey.from_ingress("desktop/owner/session/chat", "stable-owner-event-123")
binding=c.BindingMetadata("c"*64,"d"*64,"e"*64,"f"*64,Runtime.CHROME_COMPANION)
real_open, real_write, real_sync=os.open,os.write,os.fsync
target_fd=-1
def opening(name,flags,*args,**kwargs):
    global target_fd
    fd=real_open(name,flags,*args,**kwargs)
    if str(name).endswith(".spent.json") and flags & os.O_CREAT:
        target_fd=fd
        if phase=="created": os._exit(exit_code)
    return fd
def writing(fd,buffer):
    if fd==target_fd and phase=="partial":
        real_write(fd,buffer[:19])
        os._exit(exit_code)
    return real_write(fd,buffer)
def syncing(fd):
    real_sync(fd)
    if phase=="file_synced" and fd==target_fd: os._exit(exit_code)
    if phase=="directory_synced" and stat.S_ISDIR(os.fstat(fd).st_mode): os._exit(exit_code)
with c.ClaimStore(data,identity) as store:
    os.open,os.write,os.fsync=opening,writing,syncing
    store.consume(key,binding)
raise AssertionError("crash injection did not run")
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT), str(data),
                             json.dumps(identity.to_data()), phase, str(exit_code)], capture_output=True, text=True, timeout=30)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert _path(data).exists()
    with c.ClaimStore(data, identity) as store:
        assert store.consume(KEY, BINDING) is False


@pytest.mark.parametrize("kind", [c.RecordKind.ADMISSION, c.RecordKind.BINDING, c.RecordKind.SPENT])
@pytest.mark.parametrize("attack", ["empty", "corrupt", "duplicate", "unknown", "oversize", "symlink", "hardlink", "fifo", "mode"])
def test_hostile_record_objects_never_enable_consumption(world, tmp_path, kind, attack):
    data, identity = world
    target = _path(data, kind)
    canary = tmp_path / "canary"
    canary.write_bytes(b"unchanged unrelated inode")
    canary.chmod(0o600)
    before = (canary.read_bytes(), canary.stat().st_ino, stat.S_IMODE(canary.stat().st_mode))
    if target.exists():
        target.unlink()  # hostile fixture only, not a production cleanup path
    if attack == "symlink":
        target.symlink_to(canary)
    elif attack == "hardlink":
        os.link(canary, target)
    elif attack == "fifo":
        os.mkfifo(target, 0o600)
    else:
        raw = {"empty": b"", "corrupt": b"{bad", "duplicate": b'{"v":1,"v":1}',
               "unknown": b'{"v":1,"payload":"not permitted"}', "oversize": b"x" * (c.MAX_RECORD_BYTES + 1),
               "mode": b"{}"}[attack]
        target.write_bytes(raw)
        target.chmod(0o644 if attack == "mode" else 0o600)
    with c.ClaimStore(data, identity) as store:
        if kind is c.RecordKind.SPENT:
            assert store.consume(KEY, BINDING) is False
        else:
            with pytest.raises(c.ClaimStoreError):
                store.consume(KEY, BINDING)
    assert os.path.lexists(target)
    assert (canary.read_bytes(), canary.stat().st_ino, stat.S_IMODE(canary.stat().st_mode)) == before


@pytest.mark.parametrize("location", ["reference", "identity"])
@pytest.mark.parametrize("attack", ["missing", "symlink", "hardlink", "fifo", "mode", "corrupt"])
def test_hostile_or_missing_initialization_metadata_fails_closed(world, tmp_path, location, attack):
    data, identity = world
    target = data / "chrome_companion" / c.REFERENCE_FILE if location == "reference" else _ledger(data) / c.IDENTITY_FILE
    canary = tmp_path / "marker-canary"
    canary.write_bytes(target.read_bytes())
    canary.chmod(0o600)
    before = canary.read_bytes()
    target.unlink()
    if attack == "symlink":
        target.symlink_to(canary)
    elif attack == "hardlink":
        os.link(canary, target)
    elif attack == "fifo":
        os.mkfifo(target, 0o600)
    elif attack != "missing":
        target.write_bytes(b"{bad" if attack == "corrupt" else before)
        target.chmod(0o644 if attack == "mode" else 0o600)
    with pytest.raises(c.ClaimStoreError):
        c.ClaimStore(data, identity)
    assert canary.read_bytes() == before
    with pytest.raises(c.ClaimStoreError):
        c.initialize_store(data)


@pytest.mark.parametrize("location", ["store", "companion"])
@pytest.mark.parametrize("attack", ["symlink", "mode", "owner"])
def test_private_directory_objects_are_checked(world, tmp_path, monkeypatch, location, attack):
    data, identity = world
    directory = _ledger(data) if location == "store" else data / "chrome_companion"
    if attack == "symlink":
        retired = tmp_path / "retired"
        directory.rename(retired)
        directory.symlink_to(retired)
    elif attack == "mode":
        directory.chmod(0o755)
    else:
        inode = directory.stat().st_ino
        real_stat = os.fstat
        def fstat(fd):
            info = real_stat(fd)
            if info.st_ino == inode:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info
        monkeypatch.setattr(c.os, "fstat", fstat)
    with pytest.raises(c.ClaimStoreError):
        c.ClaimStore(data, identity)


def test_file_owner_checked_on_opened_descriptor(world, monkeypatch):
    data, identity = world
    inode = _path(data, c.RecordKind.BINDING).stat().st_ino
    real_stat = os.fstat
    def fstat(fd):
        info = real_stat(fd)
        if info.st_ino == inode:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(c.os, "fstat", fstat)
    with c.ClaimStore(data, identity) as store:
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)
    assert not _path(data).exists()


@pytest.mark.parametrize("location", ["store", "companion", "data"])
def test_path_swap_at_creation_writes_only_held_directory_and_refuses_winner(world, tmp_path, monkeypatch, location):
    data, identity = world
    directory = {"store": _ledger(data), "companion": data / "chrome_companion", "data": data}[location]
    retired = tmp_path / "retired"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    canary = outside / "canary"
    canary.write_bytes(b"untouched")
    canary.chmod(0o600)
    before = (canary.read_bytes(), canary.stat().st_ino, canary.stat().st_mode)
    write = c._exclusive_write
    def swap(fd, name, raw):
        if name.endswith(".spent.json"):
            directory.rename(retired)
            directory.symlink_to(outside)
        return write(fd, name, raw)
    with c.ClaimStore(data, identity) as store:
        monkeypatch.setattr(c, "_exclusive_write", swap)
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)
    assert list(outside.iterdir()) == [canary]
    assert (canary.read_bytes(), canary.stat().st_ino, canary.stat().st_mode) == before
    directory.unlink()
    retired.rename(directory)
    monkeypatch.setattr(c, "_exclusive_write", write)
    with c.ClaimStore(data, identity) as store:
        assert store.consume(KEY, BINDING) is False


def test_record_name_swap_after_open_cannot_clobber_canary(world, tmp_path, monkeypatch):
    data, identity = world
    canary = tmp_path / "canary"
    canary.write_bytes(b"private unrelated data")
    canary.chmod(0o600)
    before = (canary.read_bytes(), canary.stat().st_ino, canary.stat().st_mode)
    real_open = os.open
    def opening(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if str(name).endswith(".spent.json") and flags & os.O_CREAT:
            _path(data).unlink()
            _path(data).symlink_to(canary)
        return fd
    with c.ClaimStore(data, identity) as store:
        monkeypatch.setattr(c.os, "open", opening)
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)
    assert (canary.read_bytes(), canary.stat().st_ino, canary.stat().st_mode) == before
    assert _path(data).is_symlink()


def test_read_name_swap_is_detected_on_same_descriptor(world, tmp_path, monkeypatch):
    data, identity = world
    target = _path(data, c.RecordKind.ADMISSION)
    inode = target.stat().st_ino
    canary = tmp_path / "canary"
    canary.write_bytes(b"unchanged")
    real_read = os.read
    swapped = False
    def reading(fd, size):
        nonlocal swapped
        result = real_read(fd, size)
        if not swapped and os.fstat(fd).st_ino == inode:
            swapped = True
            target.rename(tmp_path / "retired-admission")
            target.symlink_to(canary)
        return result
    with c.ClaimStore(data, identity) as store:
        monkeypatch.setattr(c.os, "read", reading)
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)
    assert swapped and canary.read_bytes() == b"unchanged" and not _path(data).exists()


@pytest.mark.parametrize("fault", ["mkdir", "file_sync", "directory_sync"])
def test_partial_bootstrap_is_not_automatically_repaired(tmp_path, monkeypatch, fault):
    data = tmp_path / "initializing"
    data.mkdir(mode=0o700)
    real_mkdir, real_sync = os.mkdir, os.fsync
    def mkdir(name, *args, **kwargs):
        real_mkdir(name, *args, **kwargs)
        if name == c.STORE_DIR and fault == "mkdir":
            raise OSError("injected post-mkdir uncertainty")
    def sync(fd):
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        store_directory = _ledger(data)
        inside_store = store_directory.exists() and os.fstat(fd).st_ino == store_directory.stat().st_ino
        if (fault == "file_sync" and regular) or (fault == "directory_sync" and inside_store):
            raise OSError("injected setup-sync uncertainty")
        real_sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(c.os, "mkdir", mkdir)
        patch.setattr(c.os, "fsync", sync)
        with pytest.raises(c.ClaimStoreError):
            c.initialize_store(data)
    assert _ledger(data).exists()
    before = sorted((p.relative_to(data).as_posix(), p.read_bytes()) for p in data.rglob("*") if p.is_file())
    with pytest.raises(c.ClaimStoreError):
        c.initialize_store(data)
    assert sorted((p.relative_to(data).as_posix(), p.read_bytes()) for p in data.rglob("*") if p.is_file()) == before


@pytest.mark.parametrize("value", ["../outside", "g" * 64, "A" * 64, True, None, b"a" * 64])
def test_event_digest_cannot_be_used_as_a_path_or_coerced(value):
    with pytest.raises(c.ClaimStoreError):
        c.EventKey(value)


@pytest.mark.parametrize("value", [True, 1.0, -1, 2**63, "100"])
def test_original_admission_times_are_strict(value):
    with pytest.raises(c.ClaimStoreError):
        replace(ADMISSION, admitted_ns=value)


def test_record_serialization_is_closed_and_runtime_cannot_migrate(world):
    data, identity = world
    with c.ClaimStore(data, identity) as store:
        record = store.inspect(KEY, c.RecordKind.BINDING)
    for field in ("raw_event", "payload", "url", "account", "bearer", "callback"):
        exported = record.to_data()
        exported["metadata"][field] = "sensitive"
        with pytest.raises(c.ClaimStoreError):
            c.ClaimRecord.from_data(exported)
    for runtime in ("chrome_companion", "playwright", "api"):
        with pytest.raises(c.ClaimStoreError):
            replace(BINDING, runtime=runtime)
    exported = record.to_data()
    exported["v"] = True
    with pytest.raises(c.ClaimStoreError):
        c.ClaimRecord.from_data(exported)


def test_closed_handle_cannot_create_or_inspect(world):
    data, identity = world
    store = c.ClaimStore(data, identity)
    store.close()
    store.close()
    with pytest.raises(c.ClaimStoreError):
        store.consume(KEY, BINDING)
    assert not _path(data).exists()


@pytest.mark.parametrize("name", ["../canary", "/absolute", "nested/identity.json", "unknown.json", None])
def test_storage_helpers_refuse_non_leaf_names(world, name):
    data, identity = world
    fd = os.open(_ledger(data), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(c.ClaimStoreError):
            c._exclusive_write(fd, name, b"{}")
        with pytest.raises(c.ClaimStoreError):
            c._read(fd, name)
    finally:
        os.close(fd)


def test_bootstrap_requires_existing_owner_data_directory_and_syncs_parent(tmp_path, monkeypatch):
    missing = tmp_path / "not-created"
    with pytest.raises(c.ClaimStoreError):
        c.initialize_store(missing)
    assert not missing.exists()
    data = tmp_path / "existing"
    data.mkdir(mode=0o700)
    events = []
    real_sync = os.fsync
    def sync(fd):
        info = os.fstat(fd)
        if info.st_ino == data.stat().st_ino:
            events.append("data-directory")
        elif stat.S_ISDIR(info.st_mode):
            events.append("directory")
        else:
            events.append("file")
        real_sync(fd)
    monkeypatch.setattr(c.os, "fsync", sync)
    c.initialize_store(data)
    assert events == ["data-directory", "file", "directory", "file", "directory"]


def test_hostile_ancestor_is_not_canonicalized_away_or_repaired(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    (unsafe / "relocation").symlink_to(real)
    with pytest.raises(c.ClaimStoreError):
        c.initialize_store(unsafe / "relocation")
    assert list(real.iterdir()) == [] and stat.S_IMODE(unsafe.stat().st_mode) == 0o777


@pytest.mark.parametrize("change", ["epoch", "event", "parent", "runtime", "boot", "specification"])
def test_well_formed_but_changed_record_binding_fails_closed(world, change):
    data, identity = world
    kind = c.RecordKind.ADMISSION if change == "boot" else c.RecordKind.BINDING
    target = _path(data, kind)
    value = json.loads(target.read_bytes())
    if change == "epoch":
        value["epoch"] = "0" * 64
    elif change == "event":
        value["event_key"] = "0" * 64
    elif change == "parent":
        value["parent_digest"] = "0" * 64
    elif change == "runtime":
        value["metadata"]["runtime"] = "api"
    elif change == "boot":
        value["metadata"]["boot_digest"] = "0" * 64
    else:
        value["metadata"]["specification_digest"] = "0" * 64
    target.write_bytes(c.canonical_json(value))  # scratch corruption, never a store API
    with c.ClaimStore(data, identity) as store:
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)
    assert not _path(data).exists()


@pytest.mark.parametrize("location", ["reference", "identity"])
def test_live_handle_rejects_marker_replacement_even_with_identical_bytes(world, location):
    data, identity = world
    target = data / "chrome_companion" / c.REFERENCE_FILE if location == "reference" else _ledger(data) / c.IDENTITY_FILE
    with c.ClaimStore(data, identity) as store:
        raw = target.read_bytes()
        target.rename(target.with_name("retired-marker"))
        target.write_bytes(raw)
        target.chmod(0o600)
        with pytest.raises(c.ClaimStoreError):
            store.consume(KEY, BINDING)
    assert not _path(data).exists()
