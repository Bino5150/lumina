"""AGENT-BACKUP-RESTORE-PHASE-B S1: generation-artifact referential integrity.

Synthetic data only. Rows and blobs are made through the real ingestion path
(core.generation_job / core.generation_artifact) into a tmp data root; the
check then reads a SQLite backup-API snapshot of that database, never the
database the app writes.
"""
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import config
import core.generation_artifact as ga
import core.generation_job as gj
from core import agent_backup_references as ref
from core.capability_router import Capability, RoutingDecision


def _job():
    decision = RoutingDecision(capability=Capability.IMAGE_GENERATION.value, outcome="routed",
                               selected="fake_provider", classification="explicit", reason="test")
    job = gj.begin_generation_job(Capability.IMAGE_GENERATION, "fake_provider", "fake-model-1",
                                  decision, {"prompt": "synthetic"}, reference_assets=())
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    return gj.update_status(job.lumina_job_id, gj.STATUS_SUCCEEDED)


def _snapshot(db, dest):
    src = sqlite3.connect(db)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest


@pytest.fixture
def gen(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    monkeypatch.setattr(config, "DB_PATH", str(data / "memory" / "lumina.db"))
    job = _job()
    arts = [ga.ingest_artifact(job.lumina_job_id, f"synthetic-image-{i}".encode(), "image/png",
                               provider_output_index=i) for i in range(2)]
    return data, arts, tmp_path


def _snap(gen, name="snap.db"):
    data, _arts, tmp = gen
    return _snapshot(str(data / "memory" / "lumina.db"), str(tmp / name))


def _codes(report):
    return sorted((f.code, f.relpath) for f in report.findings)


def _rel(data, art):
    return os.path.relpath(art.local_path, data)


def test_clean_root_captures_every_blob_at_the_real_ingest_path(gen):
    data, arts, _tmp = gen
    report = ref.generation_references(_snap(gen), str(data))
    assert report.rows == 2 and report.ok and report.findings == ()
    assert sorted(c.relpath for c in report.capture) == sorted(_rel(data, a) for a in arts)
    for item, art in zip(sorted(report.capture, key=lambda c: c.relpath),
                         sorted(arts, key=lambda a: _rel(data, a))):
        assert item.archive_path == "state/" + item.relpath
        assert item.sha256 == art.sha256 and item.size == art.size_bytes and item.role == "blob"
        assert ref.blob_relpath(art.artifact_id) == _rel(data, art)


def test_manifest_sidecar_is_captured_with_its_blob(gen):
    data, arts, _tmp = gen
    Path(ga.artifact_manifest_path(arts[0].artifact_id)).write_text("{}")
    report = ref.generation_references(_snap(gen), str(data))
    roles = {c.relpath: c.role for c in report.capture}
    assert roles[_rel(data, arts[0]) + ".manifest.json"] == "manifest" and report.ok


def test_same_size_tamper_is_caught_only_when_hashes_are_verified(gen):
    data, arts, _tmp = gen
    blob = Path(arts[0].local_path)
    blob.write_bytes(b"X" * blob.stat().st_size)
    snap = _snap(gen)
    assert ref.generation_references(snap, str(data)).ok
    report = ref.generation_references(snap, str(data), verify_hashes=True)
    assert report.hashes_verified
    assert _codes(report) == [(ref.HASH_MISMATCH, _rel(data, arts[0]))]
    assert [c.relpath for c in report.capture] == [_rel(data, arts[1])]


def test_missing_blob_blocks_and_its_manifest_is_not_called_an_orphan(gen):
    data, arts, _tmp = gen
    Path(ga.artifact_manifest_path(arts[0].artifact_id)).write_text("{}")
    os.unlink(arts[0].local_path)
    report = ref.generation_references(_snap(gen), str(data))
    assert _codes(report) == [(ref.MISSING_BLOB, _rel(data, arts[0]))]
    assert not report.ok
    assert [c.relpath for c in report.capture] == [_rel(data, arts[1])], "a manifest without its blob was captured"


def test_size_mismatch_blocks(gen):
    data, arts, _tmp = gen
    with open(arts[1].local_path, "ab") as fh:
        fh.write(b"+")
    report = ref.generation_references(_snap(gen), str(data))
    assert _codes(report) == [(ref.SIZE_MISMATCH, _rel(data, arts[1]))]


def test_a_blob_replaced_by_a_link_is_never_followed(gen, monkeypatch):
    data, arts, tmp = gen
    outside = tmp / "outside.bin"
    outside.write_bytes(b"synthetic-image-0")
    os.unlink(arts[0].local_path)
    os.symlink(outside, arts[0].local_path)
    snap = _snap(gen)
    real_open = os.open
    opened = []
    monkeypatch.setattr(os, "open", lambda p, f, *a, **k: opened.append((p, f)) or real_open(p, f, *a, **k))
    report = ref.generation_references(snap, str(data), verify_hashes=True)
    assert (ref.NOT_A_FILE, _rel(data, arts[0])) in _codes(report)
    assert all(f & os.O_NOFOLLOW for _p, f in opened)
    assert not any(str(p).endswith("outside.bin") for p, _f in opened)


def test_a_restored_copy_flags_foreign_local_paths_without_blocking(gen):
    data, arts, tmp = gen
    restored = tmp / "restored"
    shutil.copytree(data, restored, symlinks=True)
    snap = _snapshot(str(restored / "memory" / "lumina.db"), str(tmp / "restored-snap.db"))
    report = ref.generation_references(snap, str(restored))
    assert {f.code for f in report.findings} == {ref.FOREIGN_LOCAL_PATH}
    assert report.ok and len(report.capture) == 2


def test_orphans_and_tmp_leftovers_are_reported_but_do_not_block(gen):
    data, arts, _tmp = gen
    shard = Path(arts[0].local_path).parent
    (shard / ("f" * 32)).write_bytes(b"unreferenced")
    (shard / (arts[0].artifact_id + ".tmp")).write_bytes(b"half")
    report = ref.generation_references(_snap(gen), str(data))
    assert sorted(f.code for f in report.findings) == [ref.ORPHAN_FILE, ref.TMP_LEFTOVER]
    assert report.ok and len(report.capture) == 2


def test_unexpected_layout_blocks_and_is_never_walked(gen, tmp_path):
    data, arts, _tmp = gen
    gen_root = data / "artifacts" / "generation"
    (gen_root / "not-a-shard").mkdir()
    (gen_root / "not-a-shard" / "x").write_bytes(b"x")
    nested = Path(arts[0].local_path).parent / "nested"
    nested.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    os.symlink(outside, gen_root / "ee")
    report = ref.generation_references(_snap(gen), str(data))
    codes = _codes(report)
    assert (ref.UNEXPECTED_ENTRY, "artifacts/generation/not-a-shard") in codes
    assert (ref.UNEXPECTED_ENTRY, "artifacts/generation/ee") in codes
    assert (ref.UNEXPECTED_ENTRY, os.path.relpath(nested, data)) in codes
    assert not any(r and r.startswith("artifacts/generation/not-a-shard/") for _c, r in codes)
    assert not report.ok


def test_a_linked_generation_tree_blocks_every_row(gen, tmp_path):
    data, arts, _tmp = gen
    gen_root = data / "artifacts" / "generation"
    moved = tmp_path / "moved_generation"
    gen_root.rename(moved)
    os.symlink(moved, gen_root)
    report = ref.generation_references(_snap(gen), str(data))
    assert (ref.UNEXPECTED_ENTRY, "artifacts/generation") in _codes(report)
    assert sum(f.code == ref.MISSING_BLOB for f in report.findings) == 2
    assert report.capture == ()


def test_an_unsafe_artifact_id_is_never_turned_into_a_path(gen, monkeypatch):
    data, _arts, tmp = gen
    snap = _snap(gen)
    conn = sqlite3.connect(snap)
    with conn:
        conn.execute("INSERT INTO generation_artifacts (artifact_id, lumina_job_id, local_path, sha256, "
                     "mime_type, size_bytes, metadata_json, provenance_json, created_at) "
                     "SELECT '../../../../etc', lumina_job_id, '/etc/passwd', sha256, mime_type, 1, "
                     "'{}', '{}', created_at FROM generation_artifacts LIMIT 1")
    conn.close()
    report = ref.generation_references(snap, str(data))
    assert [f.code for f in report.findings] == [ref.UNSAFE_ID]
    assert all(f.relpath is None for f in report.findings) and not report.ok


def test_no_table_and_no_tree_is_an_honest_empty_result(tmp_path):
    data = tmp_path / "empty"
    data.mkdir()
    snap = tmp_path / "empty.db"
    conn = sqlite3.connect(snap)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.close()
    report = ref.generation_references(str(snap), str(data))
    assert report.rows == 0 and report.ok and report.capture == ()


def test_rows_without_a_tree_are_missing(gen):
    data, arts, _tmp = gen
    shutil.rmtree(data / "artifacts")
    report = ref.generation_references(_snap(gen), str(data))
    assert [f.code for f in report.findings] == [ref.MISSING_BLOB, ref.MISSING_BLOB]


def test_snapshot_is_opened_immutably_and_left_byte_identical(gen):
    data, _arts, tmp = gen
    snap = _snap(gen)
    before = hashlib.sha256(Path(snap).read_bytes()).hexdigest()
    listing = sorted(os.listdir(tmp))
    ref.generation_references(snap, str(data), verify_hashes=True)
    assert hashlib.sha256(Path(snap).read_bytes()).hexdigest() == before
    assert sorted(os.listdir(tmp)) == listing, "a sidecar appeared beside the snapshot"


def test_snapshot_must_be_a_regular_file(gen, tmp_path):
    data, _arts, _tmp = gen
    link = tmp_path / "snap-link.db"
    os.symlink(_snap(gen), link)
    with pytest.raises(ref.ReferenceUnavailable):
        ref.generation_references(str(link), str(data))
    with pytest.raises(ref.ReferenceUnavailable):
        ref.generation_references(str(tmp_path / "absent.db"), str(data))


def test_data_root_must_be_exact(gen):
    data, _arts, _tmp = gen
    snap = _snap(gen)
    with pytest.raises(ref.ReferenceUnavailable):
        ref.generation_references(snap, str(data) + "/")
    with pytest.raises(ref.ReferenceUnavailable):
        ref.generation_references(snap, os.fsencode(str(data)))


def test_hashing_reads_only_referenced_blobs(gen, monkeypatch):
    data, arts, _tmp = gen
    shard = Path(arts[0].local_path).parent
    (shard / ("f" * 32)).write_bytes(b"unreferenced")
    snap = _snap(gen)
    real_open = os.open
    files = []

    def tracking(p, f, *a, **k):
        if not f & os.O_DIRECTORY:
            files.append(p)
        return real_open(p, f, *a, **k)
    monkeypatch.setattr(os, "open", tracking)
    ref.generation_references(snap, str(data), verify_hashes=True)
    assert sorted(files) == sorted(a.artifact_id for a in arts)
    files.clear()
    ref.generation_references(snap, str(data))
    assert files == [], "content was read without verify_hashes"


def test_bounds_refuse_rather_than_truncate(gen, monkeypatch):
    data, _arts, _tmp = gen
    snap = _snap(gen)
    monkeypatch.setattr(ref, "MAX_ROWS", 1)
    with pytest.raises(ref.ReferenceUnavailable, match="bounds"):
        ref.generation_references(snap, str(data))
    monkeypatch.setattr(ref, "MAX_ROWS", 100)
    monkeypatch.setattr(ref, "MAX_BLOB_BYTES", 4)
    with pytest.raises(ref.ReferenceUnavailable, match="bounds"):
        ref.generation_references(snap, str(data), verify_hashes=True)
    monkeypatch.setattr(ref, "MAX_BLOB_BYTES", 1 << 20)
    monkeypatch.setattr(ref, "MAX_ENTRIES", 1)
    with pytest.raises(ref.ReferenceUnavailable, match="bounds"):
        ref.generation_references(snap, str(data))


def test_a_blob_swapped_between_lstat_and_hash_is_refused(gen, monkeypatch):
    data, arts, tmp = gen
    snap = _snap(gen)
    target = Path(arts[0].local_path)
    real_lstat = os.lstat

    def swapping(p, *a, **k):
        st = real_lstat(p, *a, **k)
        if p == arts[0].artifact_id and k.get("dir_fd") is not None:
            target.rename(tmp / "moved-blob")
            target.write_bytes(b"synthetic-image-0")
        return st
    monkeypatch.setattr(os, "lstat", swapping)
    with pytest.raises(ref.ReferenceUnavailable, match="changed"):
        ref.generation_references(snap, str(data), verify_hashes=True)


def test_a_malformed_row_hash_blocks(gen):
    data, arts, _tmp = gen
    snap = _snap(gen)
    conn = sqlite3.connect(snap)
    with conn:
        conn.execute("UPDATE generation_artifacts SET sha256 = 'not-a-hash' WHERE artifact_id = ?",
                     (arts[1].artifact_id,))
    conn.close()
    report = ref.generation_references(snap, str(data))
    assert _codes(report) == [(ref.HASH_MISMATCH, _rel(data, arts[1]))]


def test_descriptors_are_closed(gen, monkeypatch):
    data, _arts, _tmp = gen
    snap = _snap(gen)
    real_open, real_close = os.open, os.close
    opened, closed = [], []
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(real_open(*a, **k)) or opened[-1])
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd) or real_close(fd))
    ref.generation_references(snap, str(data), verify_hashes=True)
    assert opened and sorted(opened) == sorted(closed)


def test_a_snapshot_with_a_pending_log_is_refused_not_read_stale(gen):
    data, arts, _tmp = gen
    snap = _snap(gen)
    holder = sqlite3.connect(snap)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("DELETE FROM generation_artifacts")
    holder.commit()                       # held open: the delete lives only in snap-wal
    try:
        assert os.path.exists(snap + "-wal")
        with pytest.raises(ref.ReferenceUnavailable, match="pending wal"):
            ref.generation_references(snap, str(data))
    finally:
        holder.close()
    assert ref.generation_references(snap, str(data)).rows == 0


def test_a_linked_manifest_blocks_and_is_not_captured(gen, tmp_path):
    data, arts, _tmp = gen
    outside = tmp_path / "outside-manifest.json"
    outside.write_text("{}")
    os.symlink(outside, ga.artifact_manifest_path(arts[0].artifact_id))
    report = ref.generation_references(_snap(gen), str(data))
    assert _codes(report) == [(ref.NOT_A_FILE, _rel(data, arts[0]) + ".manifest.json")]
    assert all(c.role == "blob" for c in report.capture)


def test_a_two_letter_non_hex_directory_is_not_a_shard(gen):
    data, _arts, _tmp = gen
    odd = data / "artifacts" / "generation" / "zz"
    odd.mkdir()
    (odd / ("a" * 32)).write_bytes(b"x")
    report = ref.generation_references(_snap(gen), str(data))
    assert _codes(report) == [(ref.UNEXPECTED_ENTRY, "artifacts/generation/zz")]


# --- R4: fixes for the R3/S1 review (F1, F2, F3, F5, F6) -------------------

def test_r4_nul_in_either_path_is_refused_as_unavailable(gen, monkeypatch):
    data, _arts, _tmp = gen
    snap = _snap(gen)
    real_open, real_close = os.open, os.close
    opened, closed = [], []
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(real_open(*a, **k)) or opened[-1])
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd) or real_close(fd))
    for args in ((snap, str(data) + "\0x"), (snap + "\0x", str(data))):
        with pytest.raises(ref.ReferenceUnavailable, match="NUL"):
            ref.generation_references(*args)
    assert sorted(opened) == sorted(closed), "a descriptor leaked"


def test_r4_an_empty_snapshot_is_unknown_not_clean(gen, tmp_path):
    data, _arts, _tmp = gen
    blank = tmp_path / "blank.db"
    blank.write_bytes(b"")
    with pytest.raises(ref.ReferenceUnavailable, match="empty"):
        ref.generation_references(str(blank), str(data))


def test_r4_a_snapshot_without_the_table_cannot_hide_blobs_as_orphans(gen, tmp_path):
    data, _arts, _tmp = gen
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.close()
    with pytest.raises(ref.ReferenceUnavailable, match="no generation_artifacts table"):
        ref.generation_references(str(foreign), str(data))


def test_r4_a_tableless_snapshot_with_only_junk_layout_is_still_unknown(tmp_path):
    data = tmp_path / "d"
    (data / "artifacts" / "generation" / "not-a-shard").mkdir(parents=True)
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.close()
    with pytest.raises(ref.ReferenceUnavailable, match="no generation_artifacts table"):
        ref.generation_references(str(foreign), str(data))


def test_r4_a_tableless_snapshot_with_an_empty_tree_is_honestly_empty(tmp_path):
    data = tmp_path / "d"
    (data / "artifacts" / "generation" / "ab").mkdir(parents=True)
    snap = tmp_path / "s.db"
    conn = sqlite3.connect(snap)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.close()
    report = ref.generation_references(str(snap), str(data))
    assert report.rows == 0 and report.ok and report.findings == ()


def test_r4_a_view_named_generation_artifacts_is_refused(gen, tmp_path):
    data, _arts, _tmp = gen
    snap = _snap(gen)
    conn = sqlite3.connect(snap)
    with conn:
        conn.execute("ALTER TABLE generation_artifacts RENAME TO real_rows")
        conn.execute("CREATE VIEW generation_artifacts AS SELECT * FROM real_rows WHERE 0")
    conn.close()
    with pytest.raises(ref.ReferenceUnavailable, match="not a table"):
        ref.generation_references(snap, str(data))


def test_r4_a_fifo_in_place_of_a_blob_is_refused_without_hanging(tmp_path):
    import signal
    shard = tmp_path / "shard"
    shard.mkdir()
    fifo = shard / ("a" * 32)
    os.mkfifo(fifo)
    expected = os.lstat(fifo)          # the stat the walk would have bound
    dfd = os.open(shard, os.O_RDONLY | os.O_DIRECTORY)

    def hung(*_a):
        raise TimeoutError("the blob open blocked on a FIFO")
    old = signal.signal(signal.SIGALRM, hung)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        assert ref._FILE_FLAGS & os.O_NONBLOCK
        with pytest.raises(ref.ReferenceUnavailable, match="changed"):
            ref._hash_entry(fifo.name, dfd, expected)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        os.close(dfd)


def test_r4_verified_ok_requires_hashes(gen):
    data, _arts, _tmp = gen
    snap = _snap(gen)
    sizes_only = ref.generation_references(snap, str(data))
    hashed = ref.generation_references(snap, str(data), verify_hashes=True)
    assert sizes_only.ok and not sizes_only.verified_ok
    assert hashed.ok and hashed.verified_ok


def test_r4_verified_ok_is_false_when_anything_blocks(gen):
    data, arts, _tmp = gen
    os.unlink(arts[0].local_path)
    report = ref.generation_references(_snap(gen), str(data), verify_hashes=True)
    assert report.hashes_verified and not report.ok and not report.verified_ok


def test_r4_top_level_names_count_against_the_entry_bound(tmp_path, monkeypatch):
    data = tmp_path / "d"
    top = data / "artifacts" / "generation"
    top.mkdir(parents=True)
    for i in range(5):
        (top / f"junk{i}").write_bytes(b"")
    snap = tmp_path / "s.db"
    conn = sqlite3.connect(snap)
    conn.execute("CREATE TABLE generation_artifacts (artifact_id, local_path, sha256, size_bytes)")
    conn.close()
    monkeypatch.setattr(ref, "MAX_ENTRIES", 3)
    seen = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda fd: seen.append(fd) or real_scandir(fd))
    with pytest.raises(ref.ReferenceUnavailable, match="bounds"):
        ref.generation_references(str(snap), str(data))
    assert len(seen) == 1, "shards were walked after the top level exceeded the bound"
