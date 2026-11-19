"""AGENT-BACKUP-RESTORE-PHASE-B S2: owner-declared exclusions (D1-D4), the
installation-local Wire settings class (W-A) and side-copy refusal on
restore. Synthetic data only.

Pins that each owner decision is matched narrowly (anything else still
blocks), never modifies or deletes the original, is listed in the receipt
with counts and constants only, and that recovery leaves Wire autonomy and
the wake listener off. Legacy Recovery keeps its existing handling.
"""
import ast
import contextlib
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

import core.agent_backup as ab
import core.agent_backup_export as abx
import core.agent_backup_inventory as inv
import core.agent_backup_restore as ar
import core.agent_backup_restore_intake as ri
from core import memory_backup
from test_agent_backup import _make_env, NO_IDENTITY_TRAILERS, NO_CREDENTIALS
from test_agent_backup_restore_02 import (  # noqa: F401  (fixtures)
    source, target, restore, entries, repack, _build_v2, _FakeConn)

REPO = Path(__file__).resolve().parents[1]
WIRE = {"wire_autonomy": True, "wire_autonomy_watermark": 389, "wire_autonomy_spend": [1, 2],
        "wire_autonomy_tokens_per_hour": 30000, "wire_autonomy_tokens_per_day": 120000,
        "wire_wake_listener": True}


@pytest.fixture
def env(tmp_path):
    data, base = map(Path, _make_env(tmp_path))
    (data / "memory").chmod(0o700)
    (data / "knowledge_packs_installed.json").write_text(json.dumps(
        {"lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"}}))
    return data, base, tmp_path


def _classes(report):
    return {e.relpath: e.cls for e in report.entries}


def _entry(report, rel):
    return next(e for e in report.entries if e.relpath == rel)


def _decision(report, decision):
    return next(d for d in report.exclusion_receipt()["decisions"] if d["decision"] == decision)


def _tree(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            out[os.path.relpath(path, root)] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode)
    return out


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _stray_db(data, mode=0o600, content=b""):
    path = _write(data / "lumina.db", content)
    path.chmod(mode)
    return path


def _prime_shaped(data):
    """D1-D4 laid out the way the Prime inventory saw them (names synthetic)."""
    _write(data / "chrome_companion/install.json", b"{}")
    _write(data / "chrome_companion/pairing/state.json", b"{\"paired\": true}")
    _write(data / "memory/lumina.db.bak_aaa_20260827", b"A" * 300)
    _write(data / "memory/lumina.db.bak_bbb_20260910", b"B" * 200)
    _write(data / "memory/lumina.db.bak_ccc_20260910", b"C" * 100)
    _write(data / "memory/prefs.json.bak_provider_routing", b"{\"openai_api_key\": \"KEY-SENTINEL\"}")
    _stray_db(data)
    for name, size in (("flight_recorder.db", 4096), ("flight_recorder.db-wal", 64), ("note.txt", 7)):
        _write(data / "telemetry/flight-recorder-corrupt-20260906-144422" / name, b"q" * size)


# ------------------------------------------------------------------- D1

def test_d1_companion_directory_is_declared_and_counted(env):
    data = env[0]
    _write(data / "chrome_companion/install.json", b"{}")
    _write(data / "chrome_companion/sub/state.json", b"12345")
    report = inv.inventory(data)
    e = _entry(report, "chrome_companion")
    assert (e.cls, e.decision, e.items, e.item_bytes) == (inv.EXCLUDED_DECLARED, "D1", 3, 7)
    assert report.no_known_blockers, report.blockers
    assert not any(x.relpath.startswith("chrome_companion/") for x in report.entries)
    d1 = _decision(report, "D1")
    assert (d1["status"], d1["entries"], d1["items"], d1["bytes"]) == ("excluded", 1, 3, 7)
    assert d1["repair_required"] == "chrome_companion_pairing"


@pytest.mark.parametrize("make", ["file", "link"])
def test_d1_companion_that_is_not_a_real_directory_blocks(env, make):
    data, _base, tmp = env
    if make == "file":
        _write(data / "chrome_companion")
    else:
        (tmp / "elsewhere").mkdir()
        (data / "chrome_companion").symlink_to(tmp / "elsewhere")
    report = inv.inventory(data)
    e = _entry(report, "chrome_companion")
    assert (e.cls, e.decision) == (inv.UNSAFE, "D1")
    assert "chrome_companion" in report.blockers
    assert _decision(report, "D1")["status"] == "blocked"


def test_d1_count_never_follows_a_link_inside(env):
    data, _base, tmp = env
    outside = tmp / "outside"
    for i in range(5):
        _write(outside / f"f{i}", b"zz")
    _write(data / "chrome_companion/install.json", b"{}")
    (data / "chrome_companion/escape").symlink_to(outside)
    report = inv.inventory(data)
    e = _entry(report, "chrome_companion")
    assert (e.cls, e.items, e.item_bytes) == (inv.EXCLUDED_DECLARED, 2, 2)


# ------------------------------------------------------------------- D2

def test_d2_owner_side_copies_are_declared_never_carried(env):
    data = env[0]
    _write(data / "memory/lumina.db.bak_aaa_20260827", b"A" * 30)
    _write(data / "memory/prefs.json.bak_provider_routing", b"{\"openai_api_key\": \"KEY-SENTINEL\"}")
    report = inv.inventory(data)
    for rel in ("memory/lumina.db.bak_aaa_20260827", "memory/prefs.json.bak_provider_routing"):
        e = _entry(report, rel)
        assert (e.cls, e.decision, e.archive_path) == (inv.EXCLUDED_DECLARED, "D2", None)
    assert report.no_known_blockers, report.blockers
    d2 = _decision(report, "D2")
    assert (d2["status"], d2["entries"], d2["items"], d2["bytes"]) == ("excluded", 2, 2, 30 + 34)
    assert d2["restore"] == "refused_in_verified_archives"


@pytest.mark.parametrize("rel", [
    "memory/prefs.json.bak", "memory/lumina.db.bak", "memory/lumina.db.bak_",
    "memory/lumina.db.bak_x.y", "memory/Lumina.db.bak_x", "memory/other.json.bak_x",
    "memory/lumina.db.bak_-x",
    "lumina.db.bak_x", "telemetry/flight_recorder.db.bak_x", "memory/lumina.db.bak_" + "x" * 65,
])
def test_d2_near_misses_still_block(env, rel):
    _write(env[0] / rel)
    report = inv.inventory(env[0])
    e = _entry(report, rel)
    assert e.cls == inv.UNDECLARED and e.decision is None
    assert rel in report.blockers


def test_d2_side_copy_that_is_a_link_or_directory_blocks(env):
    data, _base, tmp = env
    _write(tmp / "target.json", b"{}")
    (data / "memory/prefs.json.bak_x").symlink_to(tmp / "target.json")
    (data / "memory/lumina.db.bak_y").mkdir()
    report = inv.inventory(data)
    assert _classes(report)["memory/prefs.json.bak_x"] == inv.UNSAFE
    assert _classes(report)["memory/lumina.db.bak_y"] == inv.UNDECLARED
    assert {"memory/prefs.json.bak_x", "memory/lumina.db.bak_y"} <= set(report.blockers)
    assert _decision(report, "D2")["status"] == "absent"


# ------------------------------------------------------------------- D3

def test_d3_empty_unused_stray_database_is_declared(env):
    data = env[0]
    _stray_db(data)
    report = inv.inventory(data)
    e = _entry(report, "lumina.db")
    assert (e.cls, e.decision, e.size) == (inv.EXCLUDED_DECLARED, "D3", 0)
    assert report.no_known_blockers, report.blockers
    d3 = _decision(report, "D3")
    assert (d3["status"], d3["entries"], d3["bytes"]) == ("excluded", 1, 0)
    assert type(d3["uninspectable_processes"]) is int and d3["uninspectable_processes"] >= 0
    # "Unused" is claimed only as far as it was observed.
    assert d3["condition"] == "verified_empty_no_inspectable_holder"


@pytest.mark.parametrize("case,code", [
    ("not_empty", "not_empty"), ("sidecar", "sidecar_present"),
    ("journal", "sidecar_present"), ("others_write", "writable_by_others"),
    ("held_open", "open_or_unknown"), ("no_proc", "open_or_unknown"),
    ("fd_bound", "open_or_unknown"), ("changed", "changed_during_check"),
    ("hardlink", "linked_elsewhere"), ("foreign", "foreign_owner"),
])
def test_d3_fails_closed_when_a_condition_does_not_hold(env, monkeypatch, case, code):
    data = env[0]
    path = _stray_db(data, mode=0o666 if case == "others_write" else 0o600,
                     content=b"SQLite format 3\0" if case == "not_empty" else b"")
    if case == "sidecar":
        _write(data / "lumina.db-wal", b"")
    if case == "journal":
        _write(data / "lumina.db-journal", b"")
    if case == "no_proc":
        monkeypatch.setattr(inv, "_PROC", str(env[2] / "no-proc"))
    if case == "fd_bound":
        monkeypatch.setattr(inv, "MAX_PROC_FDS", 0)
    if case == "hardlink":
        os.link(path, data / "memory/second-name")
    if case == "foreign":
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    if case == "changed":
        real = inv._open_holders

        def touch_then_scan(st):
            path.write_bytes(b"written during the check")
            return real(st)
        monkeypatch.setattr(inv, "_open_holders", touch_then_scan)
    with contextlib.ExitStack() as stack:
        if case == "held_open":
            fd = os.open(path, os.O_RDONLY)
            stack.callback(os.close, fd)
        report = inv.inventory(data)
    e = _entry(report, "lumina.db")
    assert (e.cls, e.decision) == (inv.UNDECLARED, "D3")
    assert code in e.reason.split(": ", 1)[1].split(",")
    assert "lumina.db" in report.blockers and not report.no_known_blockers
    assert _decision(report, "D3")["status"] == "blocked"


def test_d3_stray_database_that_is_a_link_blocks(env):
    data, _base, tmp = env
    _write(tmp / "real.db", b"")
    (data / "lumina.db").symlink_to(tmp / "real.db")
    report = inv.inventory(data)
    e = _entry(report, "lumina.db")
    assert (e.cls, e.decision) == (inv.UNSAFE, "D3")
    assert _decision(report, "D3")["status"] == "blocked"


def test_d3_never_names_the_configured_database():
    # config.DB_PATH is always DATA_DIR/memory/lumina.db; D3 matches only the root name.
    text = (REPO / "config.example.py").read_text()
    assert 'DB_PATH = os.path.join(DATA_DIR, "memory", "lumina.db")' in text
    assert inv._STRAY_DB == "lumina.db" and "memory/lumina.db" in inv.REQUIRED


# ------------------------------------------------------------------- D4

def test_d4_forensic_quarantine_is_held_not_carried(env):
    data = env[0]
    q = data / "telemetry/flight-recorder-corrupt-20260906-144422"
    _write(q / "flight_recorder.db", b"q" * 40)
    _write(q / "flight_recorder.db-wal", b"w" * 2)
    report = inv.inventory(data)
    e = _entry(report, "telemetry/flight-recorder-corrupt-20260906-144422")
    assert (e.cls, e.decision, e.items, e.item_bytes) == (inv.EXCLUDED_DECLARED, "D4", 2, 42)
    assert report.no_known_blockers, report.blockers
    d4 = _decision(report, "D4")
    assert d4["hold"] == "until_owner_disposition" and d4["active_recovered_state"] is False
    assert (d4["status"], d4["items"], d4["bytes"]) == ("excluded", 2, 42)


@pytest.mark.parametrize("rel,kind", [
    ("telemetry/flight-recorder-corrupt-2026096-144422", "dir"),
    ("telemetry/flight-recorder-corrupt-20260906-144422x", "dir"),
    ("telemetry/flight-recorder-corrupt-20260906", "dir"),
    ("memory/flight-recorder-corrupt-20260906-144422", "dir"),
    ("flight-recorder-corrupt-20260906-144422", "dir"),
    ("telemetry/flight-recorder-corrupt-20260906-144422", "file"),
])
def test_d4_near_misses_still_block(env, rel, kind):
    path = env[0] / rel
    if kind == "dir":
        _write(path / "flight_recorder.db")
    else:
        _write(path)
    report = inv.inventory(env[0])
    assert _classes(report)[rel] in inv.BLOCKING
    assert rel in report.blockers
    assert _decision(report, "D4")["status"] == ("blocked" if kind == "file" else "absent")


def test_declared_directories_share_the_inventory_bounds(env, monkeypatch):
    data = env[0]
    for i in range(30):
        _write(data / f"chrome_companion/f{i}")
    monkeypatch.setattr(inv, "MAX_ENTRIES", 25)
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(data)


# -------------------------------------------------------------- receipt

def test_receipt_lists_every_decision_even_when_absent(env):
    receipt = inv.inventory(env[0]).exclusion_receipt()
    assert receipt["schema"] == "lumina.backup.declared_exclusions" and receipt["version"] == 1
    assert receipt["paths_listed"] is False and receipt["originals_modified"] is False
    assert [d["decision"] for d in receipt["decisions"]] == ["D1", "D2", "D3", "D4"]
    assert all((d["status"], d["entries"], d["items"], d["bytes"]) == ("absent", 0, 0, 0)
               for d in receipt["decisions"])
    assert "uninspectable_processes" not in receipt["decisions"][2]


def test_prime_shaped_root_has_no_inventory_blockers(env):
    # S3: artifacts/generation is now a captured family (export 2.2), so a
    # Prime-shaped root with D1-D4 declared has no inventory blockers left.
    data, _base, tmp = env
    _prime_shaped(data)
    _write(data / "artifacts/generation/ab/abcd", b"img")
    before = _tree(data)
    report = inv.inventory(data)
    assert report.blockers == ()
    assert _tree(data) == before  # nothing created, changed or removed
    decisions = {d["decision"]: d for d in report.exclusion_receipt()["decisions"]}
    assert {k: (v["status"], v["entries"], v["items"]) for k, v in decisions.items()} == {
        "D1": ("excluded", 1, 3), "D2": ("excluded", 4, 4), "D3": ("excluded", 1, 1),
        "D4": ("excluded", 1, 3)}
    blob = json.dumps(report.exclusion_receipt())
    for needle in (str(tmp), "bak_", "chrome_companion/", "144422", "KEY-SENTINEL", "lumina.db"):
        assert needle not in blob


def test_declarations_do_not_change_what_the_exporter_archives(env):
    data, base, tmp = env
    _prime_shaped(data)
    report = inv.inventory(data)
    manifest, _receipt = abx.build_review_export_with_receipt(
        str(data), str(base), str(tmp / "export.zip"),
        identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    assert inv.parity(report, manifest)["parity"] is True
    names = [m["archive_path"] for m in manifest["members"]]
    assert not any("bak" in n or "chrome_companion" in n or "corrupt" in n for n in names)


# ------------------------------------------------------------ W-A (Wire)

def _prefs_member(path):
    return json.loads(dict(entries(path))["state/preferences/prefs.json"])


def test_wire_settings_are_installation_local_and_never_archived(env):
    data, base, tmp = env
    prefs = json.loads((data / "memory/prefs.json").read_text())
    prefs.update(WIRE)
    (data / "memory/prefs.json").write_text(json.dumps(prefs))
    manifest, receipt = abx.build_review_export_with_receipt(
        str(data), str(base), str(tmp / "export.zip"),
        identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    archived = _prefs_member(tmp / "export.zip")
    assert not set(WIRE) & set(archived)
    assert manifest["export_observations"]["prefs_unclassified_count"] == 0
    assert "unclassified_preferences" not in receipt["coverage_blockers"]
    assert receipt["prefs_omitted_count"] >= len(WIRE)


def test_other_wire_keys_stay_unclassified():
    raw = json.dumps({**WIRE, "wire_future_switch": True}).encode()
    data, counts = abx._scrub_preferences_counts(raw)
    assert counts == {"omitted": 7, "unclassified": 1, "destination_local": 6}
    assert abx._scrub_preferences(raw) == (data, 7, 1)
    assert json.loads(data) == {"telegram_remote_access": False}


def test_installation_local_set_matches_the_wire_runtime_keys():
    tree = ast.parse((REPO / "comms/wire_autonomy.py").read_text())
    prefs = {node.value.value for node in tree.body
             if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
             and any(isinstance(t, ast.Name) and t.id.startswith("PREF_") for t in node.targets)}
    window = (REPO / "ui/main_window.py").read_text()
    listener = set(re.findall(r'get\("(wire_[a-z_]+)"', window))
    assert prefs | listener == set(abx._DESTINATION_LOCAL_PREFS)


def _legacy_from(data, dest, monkeypatch):
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(memory_backup, "_db", lambda: _FakeConn())
    dest.parent.mkdir(parents=True, exist_ok=True)
    memory_backup.build_memory_backup(str(data), str(dest))
    return dest


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_recovery_leaves_wire_autonomy_and_wake_listener_off(source, target, tmp_path, monkeypatch, kind):
    data, _base = source
    prefs = json.loads((data / "memory/prefs.json").read_text())
    prefs.update(WIRE)
    (data / "memory/prefs.json").write_text(json.dumps(prefs))
    archive = (_build_v2(source, tmp_path / "archives" / "v2.zip") if kind == "v2"
               else _legacy_from(data, tmp_path / "archives" / "legacy.zip", monkeypatch))
    note = {u["unit"]: u for u in ri.intake_archive(archive)["units"]}["preferences"]
    assert note["wire_autonomy_and_wake"] == "absent_off"
    assert note["installation_local_keys_omitted"] == (0 if kind == "v2" else 6)
    # v2 archived prefs already went through the scrubber (only the forced
    # telegram flag remains to drop); legacy prefs are raw.
    security = 1 if kind == "v2" else 5
    assert note["security_fields_dropped"] == security
    data_dir, base = target
    _plan, receipt = restore(archive, data_dir, base)
    rebind = receipt["rebind"]["preferences"]
    assert (rebind["security_fields_dropped"], rebind["installation_local_keys_omitted"]) == (
        security, 0 if kind == "v2" else 6)
    restored = json.loads((data_dir / "memory/prefs.json").read_text())
    assert not set(WIRE) & set(restored)
    checks = ar.verify_restored_root(data_dir, receipt)
    assert checks["checks"]["wire_settings_absent"] is True and checks["passed"] is True
    restored["wire_autonomy"] = True
    (data_dir / "memory/prefs.json").write_text(json.dumps(restored))
    checks = ar.verify_restored_root(data_dir, receipt)
    assert checks["checks"]["wire_settings_absent"] is False and checks["passed"] is False


# --------------------------------------------------- D2 on restore intake

@pytest.mark.parametrize("name", ["state/preferences/prefs.json.bak_x", "state/databases/lumina.db.BAK",
                                  "memory/prefs.json.bak", "metadata/notes.bak~"])
@pytest.mark.parametrize("declare", [True, False])
def test_verified_archive_carrying_a_side_copy_is_refused(source, tmp_path, name, declare):
    archive = _build_v2(source, tmp_path / "archives" / "v2.zip")
    items = entries(archive)
    if declare:
        manifest = json.loads(dict(items)["manifest.json"])
        manifest["members"].append({**next(m for m in manifest["members"]
                                           if m["logical_id"] == "state.preferences.prefs_json"),
                                    "archive_path": name})
        items = [(n, json.dumps(manifest).encode() if n == "manifest.json" else d) for n, d in items]
    repack(archive, items + [(name, b"{\"openai_api_key\": \"KEY-SENTINEL\"}")])
    intake = ri.intake_archive(archive)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] == "ARCHIVE_SIDE_COPY_MEMBER"
    assert intake["units"] == []


def test_every_canonical_member_name_is_outside_the_side_copy_rule(source, tmp_path):
    data, base = source
    v1 = tmp_path / "archives" / "v1.zip"
    v1.parent.mkdir(parents=True, exist_ok=True)
    ab.build_agent_backup(str(data), str(base), str(v1),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    v2 = _build_v2(source, tmp_path / "archives" / "v2.zip")
    names = {n for a in (v1, v2) for n, _ in entries(a)}
    names |= {p["archive_path"] for p in ab._CANONICAL_MEMBER_POLICY.values() if "archive_path" in p}
    assert names and not any(ri.is_side_copy_member(n) for n in names)
    for archive in (v1, v2):
        assert ri.intake_archive(archive)["kind"] == ri.VERIFIED


@pytest.mark.parametrize("path,expected", [
    ("overlay/personas/old.bak.json", False), ("state/custom_tools/x.bak.py", False),
    ("custom_tools/x.bak.py", False), ("projects/a.bak_1/chats.json", False),
    ("state/audit/tool_audit.log.bak-1", True), ("telemetry/flight_recorder.db.bak.2", True),
    ("memory/prefs.json.backup", False), ("memory/bakery.json", False), (None, False),
])
def test_side_copy_rule_is_narrow(path, expected):
    assert ri.is_side_copy_member(path) is expected


def test_cockpit_labels_the_wire_check():
    from core import agent_backup_restore_cockpit as cp
    lines = cp.describe_checks({"checks": {"wire_settings_absent": True}})
    assert lines == ["[PASS] Wire autonomy and the wake listener are absent (off)"]


def test_placement_refuses_a_side_copy_for_every_input_class():
    with pytest.raises(ar.RestoreRefused) as exc:
        ar._placement("memory/prefs.json.bak_x", "rid")
    assert exc.value.code == "MEMBER_INVALID"
    assert ar._placement("custom_tools/helper.bak.py", "rid")[0] == "data"


def test_legacy_recovery_keeps_its_existing_handling(source, target, tmp_path, monkeypatch):
    data, _base = source
    before = ri.intake_archive(_legacy_from(data, tmp_path / "archives" / "plain.zip", monkeypatch))
    _prime_shaped(data)
    archive = _legacy_from(data, tmp_path / "archives" / "decorated.zip", monkeypatch)
    after = ri.intake_archive(archive)
    assert after["kind"] == ri.LEGACY
    assert after["units"] == before["units"]
    added = 2 + 4 + 1 + 3   # D1, D2, D3 and D4 files the legacy writer zips
    changed = {k for k in after["coverage"] if after["coverage"][k] != before["coverage"][k]}
    assert changed == {"unclassified_entries"}
    assert after["coverage"]["unclassified_entries"] == before["coverage"]["unclassified_entries"] + added
    data_dir, base = target
    _plan, receipt = restore(archive, data_dir, base)
    restored = [str(p.relative_to(data_dir)) for p in data_dir.rglob("*")]
    assert not any("bak" in p or "chrome_companion" in p or "corrupt" in p for p in restored)
    assert not (data_dir / "lumina.db").exists()
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
