"""AGENT-BACKUP-RESTORE-PHASE-B S3: generated-image capture (export 2.2),
the in-line inventory and its owner-declared exclusions in the archive,
path rebinding inside the isolated restored database only, and the runtime
guard on image reads. Synthetic data only.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

import config
import core.agent_backup as ab
import core.agent_backup_export as abx
import core.agent_backup_inventory as inv
import core.agent_backup_references as refs
import core.agent_backup_restore as ar
import core.agent_backup_restore_intake as ri
import core.generation_artifact as ga
import core.generation_job as gj
from core import memory_backup
from core.capability_router import Capability, RoutingDecision
from test_agent_backup import _make_env, NO_IDENTITY_TRAILERS, NO_CREDENTIALS
from test_agent_backup_restore_02 import (  # noqa: F401  (fixtures)
    source, target, restore, entries, repack, files_of, _FakeConn)

GEN = "state/artifacts/generation/"


def _job():
    decision = RoutingDecision(capability=Capability.IMAGE_GENERATION.value, outcome="routed",
                               selected="fake_provider", classification="explicit", reason="test")
    job = gj.begin_generation_job(Capability.IMAGE_GENERATION, "fake_provider", "fake-model-1",
                                  decision, {"prompt": "synthetic"}, reference_assets=())
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    return gj.update_status(job.lumina_job_id, gj.STATUS_SUCCEEDED)


@contextlib.contextmanager
def _as_installation(data):
    """Point the app's config at one synthetic data root for the block."""
    saved = (config.DATA_DIR, config.DB_PATH)
    config.DATA_DIR, config.DB_PATH = str(data), str(Path(data) / "memory" / "lumina.db")
    try:
        yield
    finally:
        config.DATA_DIR, config.DB_PATH = saved


def _ingest(data, n=2, manifests=True):
    with _as_installation(data):
        job = _job()
        arts = [ga.ingest_artifact(job.lumina_job_id, f"\x89PNG synthetic image {i}".encode(), "image/png",
                                   provider_output_index=i) for i in range(n)]
        if manifests:
            for a in arts:
                Path(ga.artifact_manifest_path(a.artifact_id)).write_text(json.dumps(
                    {"artifact_id": a.artifact_id, "artifact_path": a.local_path, "prompt": "synthetic"}))
        with contextlib.closing(sqlite3.connect(config.DB_PATH)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return arts


@pytest.fixture
def env(tmp_path):
    data, base = map(Path, _make_env(tmp_path))
    (data / "memory").chmod(0o700)
    (data / "knowledge_packs_installed.json").write_text(json.dumps(
        {"lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"}}))
    return data, base, tmp_path


def _export(data, base, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    return abx.build_review_export_with_receipt(
        str(data), str(base), str(dest),
        identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)


def _gen_members(manifest):
    return {m["archive_path"]: m for m in manifest["members"] if m["archive_path"].startswith(GEN)}


# ------------------------------------------------------------ export 2.2

def test_export_captures_every_image_and_manifest_hash_bound(env):
    data, base, tmp = env
    arts = _ingest(data)
    manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    assert manifest["format_version"] == {"major": 2, "minor": 2}
    members = _gen_members(manifest)
    assert len(members) == 4
    archived = dict(entries(tmp / "out" / "export.zip"))
    for a in arts:
        blob = members[GEN + f"{a.artifact_id[:2]}/{a.artifact_id}"]
        assert blob["sha256"] == a.sha256 and blob["size"] == a.size_bytes
        assert hashlib.sha256(archived[blob["archive_path"]]).hexdigest() == a.sha256
        assert blob["logical_id"] == f"state.artifacts.generation.{a.artifact_id}"
        side = members[blob["archive_path"] + ".manifest.json"]
        source_side = data / "artifacts/generation" / a.artifact_id[:2] / (a.artifact_id + ".manifest.json")
        assert archived[side["archive_path"]] == source_side.read_bytes()
    gen = manifest["export_observations"]["generation_artifacts"]
    assert gen == {"status": "complete", "rows": 2, "captured_blobs": 2, "captured_manifests": 2,
                   "hashes_verified": True, "findings": {}}
    assert receipt["generation_artifacts"]["status"] == "complete"
    assert "generation_artifacts" not in receipt["coverage_blockers"]
    assert abx.verify_export(tmp / "out" / "export.zip")["valid"] is True


def _blob(data, art):
    return data / "artifacts" / "generation" / art.artifact_id[:2] / art.artifact_id


def test_a_missing_image_blocks_completeness_and_is_never_invented(env):
    data, base, tmp = env
    arts = _ingest(data)
    _blob(data, arts[0]).unlink()
    manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    gen = manifest["export_observations"]["generation_artifacts"]
    assert (gen["status"], gen["rows"], gen["captured_blobs"]) == ("blocked", 2, 1)
    assert gen["findings"] == {"missing_blob": 1}
    assert "generation_artifacts" in receipt["coverage_blockers"]
    assert receipt["full_agent_coverage"] is False
    assert len([p for p in _gen_members(manifest) if not p.endswith(".json")]) == 1


def test_a_mismatched_image_is_never_archived_under_its_rows_hash(env):
    data, base, tmp = env
    arts = _ingest(data)
    path = _blob(data, arts[1])
    path.write_bytes(b"X" * path.stat().st_size)          # same size, other bytes
    manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    gen = manifest["export_observations"]["generation_artifacts"]
    assert gen["status"] == "blocked" and gen["findings"] == {"hash_mismatch": 1}
    archived = dict(entries(tmp / "out" / "export.zip"))
    assert not any(v == path.read_bytes() for k, v in archived.items() if k.startswith(GEN))


def test_a_linked_image_is_never_followed_into_the_archive(env):
    data, base, tmp = env
    arts = _ingest(data)
    secret = tmp / "outside-secret.png"
    secret.write_bytes(b"OUTSIDE-SECRET-BYTES")
    path = _blob(data, arts[0])
    path.unlink()
    path.symlink_to(secret)
    manifest, _receipt = _export(data, base, tmp / "out" / "export.zip")
    assert manifest["export_observations"]["generation_artifacts"]["findings"] == {"not_a_file": 1}
    assert b"OUTSIDE-SECRET-BYTES" not in (tmp / "out" / "export.zip").read_bytes()


def test_an_image_changed_after_the_check_is_not_archived(env, monkeypatch):
    data, base, tmp = env
    arts = _ingest(data)
    real = refs.generation_references

    def check_then_change(*a, **kw):
        report = real(*a, **kw)
        _blob(data, arts[0]).write_bytes(b"Y" * arts[0].size_bytes)
        return report
    monkeypatch.setattr(refs, "generation_references", check_then_change)
    manifest, _receipt = _export(data, base, tmp / "out" / "export.zip")
    gen = manifest["export_observations"]["generation_artifacts"]
    assert gen["status"] == "blocked" and gen["findings"] == {"changed_during_capture": 1}
    assert gen["captured_blobs"] == 1 and gen["captured_manifests"] == 1


def test_an_invalid_manifest_sidecar_blocks_but_its_image_is_kept(env):
    data, base, tmp = env
    arts = _ingest(data)
    Path(str(_blob(data, arts[0])) + ".manifest.json").write_bytes(b"not json")
    manifest, _receipt = _export(data, base, tmp / "out" / "export.zip")
    gen = manifest["export_observations"]["generation_artifacts"]
    assert (gen["status"], gen["captured_blobs"], gen["captured_manifests"]) == ("blocked", 2, 1)
    assert gen["findings"] == {"manifest_invalid": 1}


def test_the_archive_carries_the_owner_declared_exclusions(env):
    data, base, tmp = env
    (data / "chrome_companion").mkdir()
    (data / "chrome_companion/install.json").write_text("{}")
    (data / "memory/prefs.json.bak_provider_routing").write_text('{"openai_api_key": "KEY-SENTINEL"}')
    q = data / "telemetry/flight-recorder-corrupt-20260906-144422"
    q.mkdir()
    (q / "flight_recorder.db").write_bytes(b"q" * 10)
    manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    declared = manifest["export_observations"]["declared_exclusions"]
    assert declared == inv.inventory(data).exclusion_receipt()
    assert {d["decision"]: d["status"] for d in declared["decisions"]} == {
        "D1": "excluded", "D2": "excluded", "D3": "absent", "D4": "excluded"}
    assert receipt["declared_exclusions"] == declared
    assert manifest["export_observations"]["inventory"] == {"status": "observed", "blocking_entries": 0}
    raw = (tmp / "out" / "export.zip").read_bytes()
    assert b"KEY-SENTINEL" not in raw and b"flight-recorder-corrupt" not in raw


def test_an_unobservable_inventory_is_declared_and_blocks(env, monkeypatch):
    data, base, tmp = env

    def unavailable(_root):
        raise inv.InventoryUnavailable("synthetic")
    monkeypatch.setattr(inv, "inventory", unavailable)
    manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    assert manifest["export_observations"]["inventory"] == {"status": "unavailable", "blocking_entries": 0}
    assert manifest["export_observations"]["declared_exclusions"] is None
    assert "inventory" in receipt["coverage_blockers"]


def test_installation_local_preferences_are_counted_in_the_archive(env):
    data, base, tmp = env
    prefs = json.loads((data / "memory/prefs.json").read_text())
    prefs.update({"wire_autonomy": True, "wire_wake_listener": True})
    (data / "memory/prefs.json").write_text(json.dumps(prefs))
    manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    assert manifest["export_observations"]["prefs_installation_local_count"] == 2
    assert receipt["installation_local_preferences_omitted"] == 2


# --------------------------------------------------- verifier (crafted)

def _rewrite(path, mutate_manifest=None, mutate_items=None):
    items = entries(path)
    if mutate_items:
        items = mutate_items(items)
    manifest = json.loads(dict(items)["manifest.json"])
    if mutate_manifest:
        mutate_manifest(manifest)
    # Keep the derived coverage block consistent, so a crafted archive fails
    # only for the reason under test.
    manifest["coverage"] = abx._coverage(manifest)
    repack(path, [(n, json.dumps(manifest).encode() if n == "manifest.json" else d) for n, d in items])
    return path


def _exported(env, n=2):
    data, base, tmp = env
    arts = _ingest(data, n)
    dest = tmp / "out" / "export.zip"
    _export(data, base, dest)
    return dest, arts


def _swap_blob(path, art, new_bytes):
    """Replace one blob's bytes and re-bind the member to them (hash, size)."""
    target = GEN + f"{art.artifact_id[:2]}/{art.artifact_id}"

    def items(xs):
        return [(n, new_bytes if n == target else d) for n, d in xs]

    def manifest(m):
        for x in m["members"]:
            if x["archive_path"] == target:
                x["sha256"], x["size"] = hashlib.sha256(new_bytes).hexdigest(), len(new_bytes)
    return _rewrite(path, manifest, items)


def test_a_blob_rebound_to_other_bytes_fails_the_row_cross_check(env):
    dest, arts = _exported(env)
    _swap_blob(dest, arts[0], b"\x89PNG substituted")
    report = abx.verify_export(dest)
    assert not report["valid"] and "EXPORT_GENERATION_ROW_MISMATCH" in report["errors"]
    intake = ri.intake_archive(dest)
    assert intake["kind"] == ri.REJECTED


def test_claiming_complete_without_every_image_is_refused(env):
    dest, arts = _exported(env)
    target = GEN + f"{arts[0].artifact_id[:2]}/{arts[0].artifact_id}"

    def drop(xs):
        return [(n, d) for n, d in xs if not n.startswith(target)]

    def manifest(m):
        m["members"] = [x for x in m["members"] if not x["archive_path"].startswith(target)]
        gen = m["export_observations"]["generation_artifacts"]
        gen["captured_blobs"] = gen["captured_manifests"] = gen["rows"] = 1
    _rewrite(dest, manifest, drop)
    report = abx.verify_export(dest)
    assert not report["valid"] and "EXPORT_GENERATION_OBSERVATION_MISMATCH" in report["errors"]


def _crosscheck(dest, **gen_update):
    """validate_export_archive alone, on a manifest whose observation was edited."""
    m = json.loads(dict(entries(dest))["manifest.json"])
    m["export_observations"]["generation_artifacts"].update(gen_update)
    with zipfile.ZipFile(dest) as z:
        return abx.validate_export_archive(m, z), m


def test_each_cross_check_holds_on_its_own(env):
    dest, _arts = _exported(env)
    errors, _ = _crosscheck(dest)
    assert errors == []
    # Truthful counts, but one row's image left out of a "complete" claim.
    m = json.loads(dict(entries(dest))["manifest.json"])
    gone = next(x for x in m["members"] if abx.generation_member_kind(x["logical_id"]) and
                abx.generation_member_kind(x["logical_id"])[0] == "blob")
    m["members"] = [x for x in m["members"] if not x["archive_path"].startswith(gone["archive_path"])]
    m["export_observations"]["generation_artifacts"].update(captured_blobs=1, captured_manifests=1)
    with zipfile.ZipFile(dest) as z:
        assert abx.validate_export_archive(m, z) == ["EXPORT_GENERATION_INCOMPLETE_CLAIM"]
    errors, _ = _crosscheck(dest, captured_blobs=1)
    assert errors == ["EXPORT_GENERATION_OBSERVATION_MISMATCH"]
    errors, _ = _crosscheck(dest, rows=3)
    assert errors == ["EXPORT_GENERATION_OBSERVATION_MISMATCH"]


@pytest.mark.parametrize("update", [
    {"captured_blobs": 1, "captured_manifests": 1},            # complete, yet a row has no image
    {"hashes_verified": False},
    {"findings": {"hash_mismatch": 1}},
    {"findings": {"missing_blob": 0}},
    {"findings": {"made_up": 1}},
    {"captured_manifests": 3},
])
def test_a_complete_observation_must_be_complete(env, update):
    dest, _arts = _exported(env)
    m = json.loads(dict(entries(dest))["manifest.json"])
    assert abx._valid_v22_observations(m["export_observations"]) is True
    m["export_observations"]["generation_artifacts"].update(update)
    assert abx._valid_v22_observations(m["export_observations"]) is False


def test_an_uncaptured_row_is_never_complete_even_without_a_finding(env, monkeypatch):
    data, base, tmp = env
    _ingest(data)
    real = refs.generation_references

    def drop_one_capture(*a, **kw):
        report = real(*a, **kw)
        keep = report.capture[2:]                       # one row's blob and manifest vanish silently
        return refs.ReferenceReport(report.rows, report.hashes_verified, report.findings, keep)
    monkeypatch.setattr(refs, "generation_references", drop_one_capture)
    manifest, _receipt = _export(data, base, tmp / "out" / "export.zip")
    gen = manifest["export_observations"]["generation_artifacts"]
    assert (gen["status"], gen["captured_blobs"], gen["findings"]) == ("blocked", 1, {})


def test_the_rebinder_refuses_ids_ingest_never_writes(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE generation_artifacts (artifact_id TEXT PRIMARY KEY, local_path TEXT)")
    conn.execute("INSERT INTO generation_artifacts VALUES ('../../escape', '/x')")
    with pytest.raises(ar.RestoreRefused) as exc:
        ar._rebind_generation(conn, str(tmp_path))
    assert exc.value.code == "GENERATION_REFERENCES_INVALID"


@pytest.mark.parametrize("mutate", [
    lambda m, a: m.update(archive_path=f"{GEN}zz/{a}"),
    lambda m, a: m.update(archive_path=f"{GEN}{a[:2]}/../{a}"),
    lambda m, a: m.update(logical_id=f"state.artifacts.generation.{a.upper()}"),
    lambda m, a: m.update(local_path="/etc/passwd"),
    lambda m, a: m.update(restore_policy="activate_immediately"),
    lambda m, a: m.update(rebind_policy="keep_archived_path"),
])
def test_malicious_generation_members_are_refused(env, mutate):
    dest, arts = _exported(env, 1)
    a = arts[0].artifact_id

    renamed = {}

    def manifest(m):
        for x in m["members"]:
            if x["logical_id"] == f"state.artifacts.generation.{a}":
                before = x["archive_path"]
                mutate(x, a)
                if x["archive_path"] != before:
                    renamed[before] = x["archive_path"]

    def items(xs):
        manifest({"members": [dict(x) for x in json.loads(dict(xs)["manifest.json"])["members"]]})
        return [(renamed.get(n, n), d) for n, d in xs]
    _rewrite(dest, manifest, items)
    assert abx.verify_export(dest)["valid"] is False
    assert ri.intake_archive(dest)["kind"] == ri.REJECTED


def test_a_manifest_sidecar_without_its_image_is_refused(env):
    dest, arts = _exported(env, 1)
    a = arts[0].artifact_id
    target = GEN + f"{a[:2]}/{a}"
    _rewrite(dest, lambda m: m.update(members=[x for x in m["members"] if x["archive_path"] != target]),
             lambda xs: [(n, d) for n, d in xs if n != target])
    report = abx.verify_export(dest)
    assert not report["valid"] and "EXPORT_GENERATION_MANIFEST_WITHOUT_BLOB" in report["errors"]


def test_v1_namespace_is_unchanged(source, tmp_path):
    data, base = source
    v1 = tmp_path / "archives" / "v1.zip"
    v1.parent.mkdir(parents=True, exist_ok=True)
    ab.build_agent_backup(str(data), str(base), str(v1),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    repack(v1, entries(v1) + [(GEN + "ab/" + "ab" * 16, b"img")])
    errors = ab.verify_agent_backup(str(v1))["errors"]
    assert any("outside the allowed v1 namespace" in e for e in errors)


def _as_v21(m):
    m["format_version"] = {"major": 2, "minor": 1}
    for key in ("min_supported_format_version", "max_known_format_version"):
        m["compatibility"][key] = dict(m["format_version"])
    for key in ("prefs_installation_local_count", "generation_artifacts", "inventory", "declared_exclusions"):
        m["export_observations"].pop(key)
    for key in ("generation_artifacts", "inventory", "declared_exclusions",
                "installation_local_preferences_omitted"):
        m["coverage"].pop(key)


def test_a_21_archive_cannot_carry_images(env):
    dest, _arts = _exported(env, 1)
    _rewrite(dest, _as_v21)
    assert abx.verify_export(dest)["valid"] is False


def test_historical_21_archives_still_verify_and_restore(source, target, tmp_path):
    data, _base = source
    arts = _ingest(data, 1)
    dest = tmp_path / "archives" / "v21.zip"
    abx.build_review_export_with_receipt(str(data), str(source[1]), str(dest),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)

    def strip(m):
        _as_v21(m)
        m["members"] = [x for x in m["members"] if not x["archive_path"].startswith(GEN)]
    _rewrite(dest, strip, lambda xs: [(n, d) for n, d in xs if not n.startswith(GEN)])
    _r = abx.verify_export(dest)
    assert _r["valid"] is True, _r["errors"]
    intake = ri.intake_archive(dest)
    assert intake["kind"] == ri.VERIFIED and intake["format"] == "agent_export_v2.1"
    assert intake["coverage"]["generated_images"] == "not_captured_by_format"
    data_dir, base = target
    _plan, receipt = restore(dest, data_dir, base)
    assert receipt["rebind"]["generation_artifacts"] == {"rebound": 1, "rows": 1, "recovered": 0,
                                                         "unrecoverable": 1}
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    _assert_reads_fail_closed(data_dir, arts[0], data)


# -------------------------------------------------------- restore + rebind

def _rows(db):
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        return dict(conn.execute("SELECT artifact_id, local_path FROM generation_artifacts"))


def _assert_reads_fail_closed(data_dir, art, source_data):
    """The restored installation never reads the source's copy."""
    with _as_installation(data_dir):
        with pytest.raises((ga.GenerationArtifactError, FileNotFoundError)):
            ga.get_artifact_bytes(art.artifact_id)
    assert _blob(source_data, art).exists()


def _restored_with_images(source, target, tmp_path, n=2):
    data, _base = source
    arts = _ingest(data, n)
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn:
        with conn:
            conn.execute("INSERT INTO chat_messages (chat_id, role, content, created_at) "
                         "VALUES (166, 'assistant', ?, 't')",
                         (f"![Generated image](file://{arts[0].local_path})",))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    archive = tmp_path / "archives" / "v22.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    abx.build_review_export_with_receipt(str(data), str(source[1]), str(archive),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    data_dir, base = target
    source_db = hashlib.sha256((data / "memory/lumina.db").read_bytes()).hexdigest()
    _plan, receipt = restore(archive, data_dir, base)
    return data, arts, archive, data_dir, receipt, source_db


def test_restore_rebinds_only_the_restored_copy_and_verifies_every_image(source, target, tmp_path):
    data, arts, archive, data_dir, receipt, source_db = _restored_with_images(source, target, tmp_path)
    assert receipt["rebind"]["generation_artifacts"] == {"rebound": 2, "rows": 2, "recovered": 2,
                                                         "unrecoverable": 0}
    restored = _rows(data_dir / "memory/lumina.db")
    for a in arts:
        assert restored[a.artifact_id] == str(_blob(data_dir, a))
        assert _blob(data_dir, a).read_bytes() == _blob(data, a).read_bytes()
        side = Path(str(_blob(data_dir, a)) + ".manifest.json")
        assert side.read_bytes() == Path(str(_blob(data, a)) + ".manifest.json").read_bytes()
        assert json.loads(side.read_text())["artifact_path"] == a.local_path   # provenance kept verbatim
    # The source is never touched: same database bytes, same rows, same files.
    assert hashlib.sha256((data / "memory/lumina.db").read_bytes()).hexdigest() == source_db
    assert _rows(data / "memory/lumina.db") == {a.artifact_id: a.local_path for a in arts}
    checks = ar.verify_restored_root(data_dir, receipt)
    assert checks["checks"]["generation_references"] is True and checks["passed"] is True


def test_chat_history_is_never_rewritten(source, target, tmp_path):
    data, arts, _archive, data_dir, _receipt, _db = _restored_with_images(source, target, tmp_path)
    sql = "SELECT content FROM chat_messages ORDER BY id"
    with contextlib.closing(sqlite3.connect(f"file:{data_dir}/memory/lumina.db?mode=ro", uri=True)) as r, \
            contextlib.closing(sqlite3.connect(f"file:{data}/memory/lumina.db?mode=ro", uri=True)) as s:
        assert r.execute(sql).fetchall() == s.execute(sql).fetchall()
        assert any(arts[0].local_path in c for (c,) in r.execute(sql))


def test_the_restored_installation_reads_its_own_images_never_the_sources(source, target, tmp_path):
    data, arts, _archive, data_dir, _receipt, _db = _restored_with_images(source, target, tmp_path)
    for a in arts:
        _blob(data, a).unlink()                      # the source copy is gone
    with _as_installation(data_dir):
        assert ga.get_artifact_bytes(arts[0].artifact_id) == f"\x89PNG synthetic image 0".encode()


def test_an_unrebound_row_cannot_read_the_source_installation(source, target, tmp_path):
    data, arts, _archive, data_dir, receipt, _db = _restored_with_images(source, target, tmp_path)
    with contextlib.closing(sqlite3.connect(data_dir / "memory/lumina.db")) as conn, conn:
        conn.execute("UPDATE generation_artifacts SET local_path=? WHERE artifact_id=?",
                     (arts[0].local_path, arts[0].artifact_id))
    with _as_installation(data_dir):
        with pytest.raises(ga.ArtifactPathRefused):
            ga.get_artifact_bytes(arts[0].artifact_id)
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["generation_references"] is False


@pytest.mark.parametrize("attack", ["blob_link", "shard_link", "tamper"])
def test_the_runtime_guard_refuses_links_and_tampering(source, target, tmp_path, attack):
    data, arts, _archive, data_dir, receipt, _db = _restored_with_images(source, target, tmp_path)
    a = arts[0]
    blob = _blob(data_dir, a)
    if attack == "blob_link":
        blob.unlink()
        blob.symlink_to(_blob(data, a))              # identical bytes, outside this root
    elif attack == "shard_link":
        shard = blob.parent
        moved = tmp_path / "moved-shard"
        shard.rename(moved)
        shard.symlink_to(moved)
    else:
        blob.write_bytes(b"Z" * blob.stat().st_size)
    with _as_installation(data_dir):
        with pytest.raises(ga.GenerationArtifactError):
            ga.get_artifact_bytes(a.artifact_id)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is False


def test_a_missing_image_restores_as_unrecoverable_and_fails_closed(source, target, tmp_path):
    data, _base = source
    arts = _ingest(data)
    _blob(data, arts[0]).unlink()
    archive = tmp_path / "archives" / "v22.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    abx.build_review_export_with_receipt(str(data), str(source[1]), str(archive),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    data_dir, base = target
    _plan, receipt = restore(archive, data_dir, base)
    assert receipt["rebind"]["generation_artifacts"] == {"rebound": 2, "rows": 2, "recovered": 1,
                                                         "unrecoverable": 1}
    assert _rows(data_dir / "memory/lumina.db")[arts[0].artifact_id] == str(_blob(data_dir, arts[0]))
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    with _as_installation(data_dir):
        with pytest.raises(FileNotFoundError):
            ga.get_artifact_bytes(arts[0].artifact_id)


def _restore_refused(archive, target, code="GENERATION_REFERENCES_INVALID"):
    data_dir, base = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(archive, data_dir, base)
    assert exc.value.code == code
    assert not data_dir.exists()
    return exc.value


@pytest.mark.parametrize("attack", ["row_mismatch", "orphan_image"])
def test_restore_rechecks_images_even_if_the_verifier_missed_it(source, target, tmp_path, monkeypatch, attack):
    data, _base = source
    arts = _ingest(data)
    archive = tmp_path / "archives" / "v22.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    abx.build_review_export_with_receipt(str(data), str(source[1]), str(archive),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    monkeypatch.setattr(abx, "validate_export_archive", lambda m, z: [])   # simulate a verifier gap
    if attack == "row_mismatch":
        _swap_blob(archive, arts[0], b"\x89PNG substituted")
    else:
        orphan = "ab" * 16
        path = GEN + f"{orphan[:2]}/{orphan}"

        def add(m):
            m["members"].append({"logical_id": f"state.artifacts.generation.{orphan}", "archive_path": path,
                                 **abx.GENERATION_BLOB_POLICY, "size": 3,
                                 "sha256": hashlib.sha256(b"img").hexdigest()})
            gen = m["export_observations"]["generation_artifacts"]
            gen["captured_blobs"] += 1
            gen["rows"] += 1
        _rewrite(archive, add, lambda xs: xs + [(path, b"img")])
    _intake = ri.intake_archive(archive)
    assert _intake["kind"] == ri.VERIFIED, _intake.get("refusal")
    _restore_refused(archive, target)


def test_an_unsafe_image_id_in_the_database_refuses_the_restore(source, target, tmp_path):
    data, _base = source
    _ingest(data, 1)
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn, conn:
        conn.execute("UPDATE generation_artifacts SET artifact_id='../../escape'")
    archive = tmp_path / "archives" / "v22.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest, _ = abx.build_review_export_with_receipt(str(data), str(source[1]), str(archive),
                                                       identity_trailers_path=NO_IDENTITY_TRAILERS,
                                                       credentials_path=NO_CREDENTIALS)
    assert manifest["export_observations"]["generation_artifacts"]["findings"].get("unsafe_id") == 1
    _restore_refused(archive, target)


@pytest.mark.parametrize("stage", ["data_staged", "generation_verified", "before_publish"])
def test_an_interrupted_image_restore_leaves_nothing_behind(source, target, tmp_path, stage):
    from test_agent_backup_restore_02 import _killed_restore, leftovers
    data, _base = source
    arts = _ingest(data)
    archive = tmp_path / "archives" / "v22.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    abx.build_review_export_with_receipt(str(data), str(source[1]), str(archive),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    source_files = files_of(data)
    data_dir, base = target
    _killed_restore(archive, data_dir, base, stage, tmp_path)
    assert not data_dir.exists() and leftovers(data_dir)
    report = ar.recover_interrupted_restore(data_dir)
    assert len(report["rolled_back"]) == 1 and leftovers(data_dir) == []
    assert files_of(data) == source_files
    _plan, receipt = restore(archive, data_dir, base)
    assert receipt["rebind"]["generation_artifacts"]["recovered"] == 2
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_legacy_recovery_rebinds_rows_and_never_reads_the_source(source, target, tmp_path, monkeypatch):
    data, _base = source
    arts = _ingest(data)
    monkeypatch.setattr(memory_backup, "_db", lambda: _FakeConn())
    archive = tmp_path / "archives" / "legacy.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    memory_backup.build_memory_backup(str(data), str(archive))
    intake = ri.intake_archive(archive)
    assert intake["kind"] == ri.LEGACY
    assert "generation_artifacts" not in {u["unit"] for u in intake["units"]}   # unchanged: not offered
    data_dir, base = target
    _plan, receipt = restore(archive, data_dir, base)
    assert receipt["rebind"]["generation_artifacts"] == {"rebound": 2, "rows": 2, "recovered": 0,
                                                         "unrecoverable": 2}
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    _assert_reads_fail_closed(data_dir, arts[0], data)


def test_the_guard_refuses_ids_ingest_never_writes(tmp_path):
    from types import SimpleNamespace
    root = tmp_path / "root"
    (root / "artifacts" / "generation" / "AB").mkdir(parents=True)
    (root / "artifacts" / "generation" / "AB" / ("AB" * 16)).write_bytes(b"x")
    (tmp_path / "outside").write_bytes(b"OUTSIDE")
    with _as_installation(root):
        # Each local_path is exactly what _artifact_path derives, so only the
        # id check stands between it and the open: "../../../outside" would
        # otherwise climb out of the data root.
        for bad in ("../../../outside", "AB" * 16, "ab" * 15):
            record = SimpleNamespace(artifact_id=bad, local_path=ga._artifact_path(bad))
            with pytest.raises(ga.ArtifactPathRefused):
                ga._open_owned_artifact(record)


def test_an_image_lost_after_restore_fails_acceptance(source, target, tmp_path):
    _data, arts, _archive, data_dir, receipt, _db = _restored_with_images(source, target, tmp_path)
    _blob(data_dir, arts[1]).unlink()
    checks = ar.verify_restored_root(data_dir, receipt)
    assert checks["checks"]["generation_references"] is False and checks["passed"] is False


# ------------------------------------------------- S3-R1 (review fixes)

def test_images_cannot_be_selected_without_their_database(source, target, tmp_path):
    _d, _arts, archive, _dir, _r, _db = _restored_with_images(source, target, tmp_path / "first")
    data_dir, base = target[0].parent / "second", target[1]
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(archive, data_dir, base,
                        selection={"generation_artifacts": "recover", "primary_database": "skip"})
    assert exc.value.code == "SELECTION_INVALID" and not data_dir.exists()


def test_duplicate_image_ids_are_refused_everywhere(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE generation_artifacts "
                 "(artifact_id TEXT, local_path TEXT, sha256 TEXT, size_bytes INTEGER)")
    conn.executemany("INSERT INTO generation_artifacts VALUES (?, '/x', ?, 3)",
                     [("ab" * 16, "0" * 64), ("ab" * 16, "1" * 64)])
    conn.commit()
    with pytest.raises(ar.RestoreRefused) as exc:
        ar._rebind_generation(conn, str(tmp_path))
    assert exc.value.code == "GENERATION_REFERENCES_INVALID"
    db = tmp_path / "dup.db"
    with contextlib.closing(sqlite3.connect(db)) as disk:
        conn.backup(disk)
    (tmp_path / "root").mkdir()
    with pytest.raises(refs.ReferenceUnavailable, match="duplicate"):
        refs.generation_references(str(db), str(tmp_path / "root"))


def test_a_sidecar_naming_another_image_is_not_archived(env):
    data, base, tmp = env
    arts = _ingest(data)
    side = Path(str(_blob(data, arts[0])) + ".manifest.json")
    side.write_text(json.dumps({"artifact_id": arts[1].artifact_id, "prompt": "swapped"}))
    manifest, _receipt = _export(data, base, tmp / "out" / "export.zip")
    gen = manifest["export_observations"]["generation_artifacts"]
    assert gen["findings"] == {"manifest_invalid": 1} and gen["status"] == "blocked"
    assert gen["captured_blobs"] == 2 and gen["captured_manifests"] == 1


def test_blocked_images_never_read_as_covered_and_verified_export_stays_closed(env):
    # Owner condition 3 as S3 implements it: a blocked image set is a
    # coverage blocker on an export that already says it is incomplete, and
    # the Verified export itself does not exist yet (it must require
    # generation_artifacts.status == "complete" when it is built).
    data, base, tmp = env
    arts = _ingest(data)
    _blob(data, arts[0]).unlink()
    _manifest, receipt = _export(data, base, tmp / "out" / "export.zip")
    assert "generation_artifacts" in receipt["coverage_blockers"]
    assert receipt["full_agent_coverage"] is False and receipt["restorable_verified"] is False
    with pytest.raises(abx.ExportUnavailable):
        abx.export_verified_agent_backup(str(data), str(base), str(tmp / "out" / "verified.zip"))
    assert not (tmp / "out" / "verified.zip").exists()
