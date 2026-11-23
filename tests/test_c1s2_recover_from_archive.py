"""C1-S2: Recover from this restore, owner PIN enrollment, and the Wire gate's
second restore marker.

Design (all sealed, in reports/AGENT-BACKUP-RESTORE-PHASE-B-R1/c1s2/): the
plan, addendum D1, amendment A2, A2-R1-PIN, Tech's TC2/TA2 conditions, the
TA2-05 decision and C1S2-BUILD-SCOPE-R0. Everything is synthetic: data roots
in tmp_path built by the real exporter and the real restore engine, the real
C1 store, an in-test fake relay and synthetic tokens. No real data root, no
real relay, no real seat.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import core.agent_backup_restore as ar
import core.agent_backup_restore_intake as ri
from core import operator_ingress as oi
from core import persistence
from core import pin_gate
from core import suspension as S
from core import suspension_backup as c1
from core import suspension_runtime as R
from core import wire_seat_gate as G
from tools import aivengers_wire as wire

from test_agent_backup_verified_export_01 import source  # noqa: F401
from test_c1_verified_backup_s3a import (  # noqa: F401
    _fresh_cache, incomplete, rebuild, verified, vsource)
from test_c1_restore_intake_s3b import SELECTIVE_CASES, _damage_db, restore, target, v23  # noqa: F401
from test_wire_seat_gate_wb1 import _bind, relay, spies  # noqa: F401

REPO = pathlib.Path(__file__).resolve().parent.parent
PIN = "123456"


class _Crash(BaseException):
    """A simulated process death at an injected point (not an OSError)."""


# ── fixtures and helpers ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _pin_session():
    for key in ("verified_channels", "attempts"):
        pin_gate._session_state[key].clear()
    yield
    for key in ("verified_channels", "attempts"):
        pin_gate._session_state[key].clear()
    R.stop()


def _use(monkeypatch, root):
    """Bind this process to `root` as a desktop launch of it would."""
    _bind(monkeypatch, root)
    monkeypatch.setattr(persistence, "PREFS_PATH", str(pathlib.Path(root) / "memory" / "prefs.json"))
    R.start(root, role="desktop", heartbeat=False)


def _restored(archive, data_dir, base, monkeypatch, *, start=True, **kw):
    plan, receipt = restore(archive, data_dir, base, **kw)
    root = pathlib.Path(data_dir)
    ns = SimpleNamespace(root=root, receipt=receipt, plan=plan, restore_id=receipt["restore_id"],
                         trace_path=root / receipt["c1"]["trace"])
    if start:
        _use(monkeypatch, root)
    return ns


@pytest.fixture
def restored23(v23, target, monkeypatch):
    data_dir, base = target
    return _restored(v23, data_dir, base, monkeypatch)


def _acks():
    return {"acknowledge_unreadable_runtimes": True, "acknowledge_prior_state_since_archive": True}


def _core_recover(root, **kw):
    """The C1 function itself (no PIN: that is the runtime wrapper's job)."""
    args = {**_acks(), **kw}
    return S.recover_from_archive(root, ingress=oi.local_suspension_control("recover_from_archive"),
                                  inputs=ar.archive_recovery_inputs(root), **args)


def _enroll(pin=PIN):
    R.set_owner_pin(ingress=oi.local_suspension_control("owner_pin_setup"), pin=pin, confirm=pin)


def _runtime_recover(root, pin=PIN, **kw):
    args = {**_acks(), **kw}
    return R.recover_from_archive(ingress=oi.local_suspension_control("recover_from_archive"),
                                  pin=pin, inputs=ar.archive_recovery_inputs(root), **args)


def _release():
    return R.release_wire_seat(
        ingress=oi.local_suspension_control("wire_seat_release"),
        expected=tuple((rid, sha) for rid, sha, _at in G.process_seat_verdict().unreleased),
        acknowledged_source_unverified=True,
        acknowledgement_sha256=G.release_acknowledgement_sha256())


def _db(root):
    return sqlite3.connect(f"file:{pathlib.Path(root) / 'memory' / S.STORE_NAME}?mode=ro", uri=True)


def _meta(root):
    conn = _db(root)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _rows(root, table):
    conn = _db(root)
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def _lineage(meta):
    return {k: v for k, v in meta.items() if k.startswith("lineage_")}


def _source_soul(source_root):
    return json.loads((pathlib.Path(source_root) / "memory" / S.IDENTITY_NAME).read_text())["soul_id"]


def _record_of(root, restore_id):
    return (pathlib.Path(root) / "restore_history" / restore_id / "c1" / "recovery_state.json")


def _suspended_on_first_sight(root, scope):
    store = S.open_store(root)
    try:
        with pytest.raises(S.AdmissionRefused) as info:
            store.capture(scope)
        return info.value.refusal is S.Refusal.SUSPENDED
    finally:
        store.close()


def _all_text(root):
    """Every string a live store holds outside meta (incidents and audit)."""
    out = []
    for table in ("incidents", "audit"):
        for row in _rows(root, table):
            out.extend(str(v) for v in row)
    return " ".join(out)


# ── Recover from this restore: normal (plan §3.1) ─────────────────────────

def test_a_kept_record_is_adopted_with_verified_and_claimed_lineage(restored23, vsource):
    root = restored23.root
    assert S.probe(root).status is S.StoreStatus.ABSENT_WITH_TRACE
    out = _core_recover(root)
    assert out.lineage_written and out.record_status == S.RECORD_ADOPTED
    assert out.full_recovery == "not_proven" and out.unknown_scope_policy == "suspended"
    meta = _meta(root)
    trace = json.loads(restored23.trace_path.read_bytes())
    lineage = _lineage(meta)
    assert lineage[S.LINEAGE_RESTORE_ID_KEY] == restored23.restore_id == trace["restore_id"]
    assert lineage[S.LINEAGE_ARCHIVE_SHA256_KEY] == trace["archive_sha256"]
    assert lineage[S.LINEAGE_TRACE_SHA256_KEY] == hashlib.sha256(restored23.trace_path.read_bytes()).hexdigest()
    assert lineage[S.LINEAGE_RECORD_STATUS_KEY] == "adopted"
    kept = _record_of(root, restored23.restore_id).read_bytes()
    assert lineage[S.LINEAGE_RECORD_SHA256_KEY] == hashlib.sha256(kept).hexdigest()
    source_soul = _source_soul(vsource[0])
    assert lineage[S.LINEAGE_CLAIMED_SOURCE_SOUL_KEY] == source_soul        # a claim, named as one
    # A new safety identity: never the archive's.
    assert meta["soul_id"] == out.soul_id != source_soul
    assert meta["store_incarnation"] == out.new_incarnation != lineage[S.LINEAGE_CLAIMED_SOURCE_INCARNATION_KEY]
    assert meta["unknown_scope_policy"] == "suspended"
    scopes = _rows(root, "scopes")
    assert [(s[0], s[1], s[2], s[3]) for s in scopes] == [(S.OWNER_SCOPE, 1, 1, 1)]
    (incident,) = _rows(root, "incidents")
    assert incident[1] == S.OWNER_SCOPE and incident[3] == S.RECOVERY_TRIGGER and incident[9] == 1
    assert json.loads(incident[4]) == {"reason": "restored_archive_prior_state_unknown",
                                       "unknown_outcome_leases": 0}
    for table in ("runtimes", "events", "deferred", "leases"):
        assert _rows(root, table) == [], table
    actions = [r[2] for r in _rows(root, "audit")]
    assert actions == ["restored_from_archive"]
    # The recovery receipt is written first and never claims a full recovery.
    rec = json.loads((pathlib.Path(out.evidence_dir) / S.ARCHIVE_RECOVERY_RECEIPT_NAME).read_text())
    assert rec["full_recovery"] == "not_proven" and rec["lineage"] == "written"
    assert rec["every_scope_starts_suspended"] is True and rec["record"] == "adopted"
    assert S.probe(root).status is S.StoreStatus.HEALTHY


def _skipped(v23, data_dir, base, mp, tmp_path):
    return _restored(v23, data_dir, base, mp, selection={ri.C1_UNIT: "skip"})


def _damaged(v23, data_dir, base, mp, tmp_path):
    name, mutate, rehash = SELECTIVE_CASES[0]
    hostile = rebuild(v23, tmp_path / f"{name}.zip", mutate, rehash=rehash)
    return _restored(hostile, data_dir, base, mp, selection={"primary_database": "recover"})


def _tampered_after_restore(v23, data_dir, base, mp, tmp_path):
    ns = _restored(v23, data_dir, base, mp)
    path = _record_of(ns.root, ns.restore_id)
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b'"version":', b'"version" :', 1))
    return ns


@pytest.mark.parametrize("make,status", [
    (_skipped, S.RECORD_SKIPPED_AT_RESTORE),
    (_damaged, S.RECORD_UNAVAILABLE_AS_RECORDED),
    (_tampered_after_restore, S.RECORD_INVALID_AT_RECOVER),
], ids=["skipped", "damaged", "tampered_after_restore"])
def test_a_record_that_is_not_adopted_still_gives_full_lineage_and_the_baseline(
        v23, target, tmp_path, monkeypatch, make, status):
    data_dir, base = target
    ns = make(v23, data_dir, base, monkeypatch, tmp_path)
    out = _core_recover(ns.root)
    assert out.lineage_written and out.record_status == status and out.unknown_outcome_leases == 0
    lineage = _lineage(_meta(ns.root))
    assert set(lineage) == {S.LINEAGE_RESTORE_ID_KEY, S.LINEAGE_ARCHIVE_SHA256_KEY,
                            S.LINEAGE_TRACE_SHA256_KEY, S.LINEAGE_RECORD_STATUS_KEY}
    assert lineage[S.LINEAGE_RECORD_STATUS_KEY] == status
    assert _meta(ns.root)["unknown_scope_policy"] == "suspended"


def test_a_pre_2_3_restore_recovers_with_lineage_and_no_record(vsource, target, tmp_path, monkeypatch):
    data_dir, base = target
    incomplete(vsource, tmp_path / "incomplete.zip")
    ns = _restored(tmp_path / "incomplete.zip", data_dir, base, monkeypatch)
    out = _core_recover(ns.root)
    assert out.lineage_written and out.record_status == S.RECORD_NOT_IN_ARCHIVE
    assert _lineage(_meta(ns.root))[S.LINEAGE_RECORD_STATUS_KEY] == "not_in_archive"


# ── the receipt must pair, or there is no lineage (D1-1) ──────────────────

def _break_receipt(ns, how):
    path = ns.root / "restore_history" / ns.restore_id / "receipt.json"
    path.chmod(0o600)
    if how == "deleted":
        path.unlink()
        return
    receipt = json.loads(path.read_bytes())
    if how == "corrupt":
        path.write_bytes(path.read_bytes()[:-2])
        return
    if how == "other_archive":
        receipt["archive"]["archive_sha256"] = "0" * 64
    elif how == "other_trace_path":
        receipt["c1"]["trace"] = "memory/suspension_evidence/x/restore_trace.json"
    receipt.pop("receipt_digest")
    receipt["receipt_digest"] = ar._digest(receipt)   # a consistent, re-sealed edit
    path.write_bytes(ar.abx._json_bytes(receipt))


@pytest.mark.parametrize("how", ["deleted", "corrupt", "other_archive", "other_trace_path"])
def test_an_unpaired_receipt_gives_the_baseline_with_no_lineage_and_a_locked_wire(restored23, how):
    _break_receipt(restored23, how)
    inputs = ar.archive_recovery_inputs(restored23.root)
    assert inputs.receipt_paired is False and inputs.record_bytes is None
    with pytest.raises(S.RecoveryBlocked):           # the no-link consequence needs its own ack
        _core_recover(restored23.root)
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE
    out = _core_recover(restored23.root, acknowledge_no_lineage=True)
    assert not out.lineage_written and out.record_status == S.RECORD_NOT_CONSIDERED
    meta = _meta(restored23.root)
    assert _lineage(meta) == {} and meta["unknown_scope_policy"] == "suspended"
    rec = json.loads((pathlib.Path(out.evidence_dir) / S.ARCHIVE_RECOVERY_RECEIPT_NAME).read_text())
    assert rec["lineage"] == "not_written_receipt_unpaired" and rec["full_recovery"] == "not_proven"
    R._reopen_after_owner_action()
    blocker = R.wire_release_blocker()
    assert blocker is not None and blocker.code == "c1_recovery_unavailable"
    assert G.process_seat_verdict().reason == G.RECOVERED_UNRELEASED


def test_inputs_that_disagree_with_the_trace_are_unpaired_in_c1_too(restored23):
    real = ar.archive_recovery_inputs(restored23.root)
    for change in ({"restore_id": "f" * 32}, {"archive_sha256": "0" * 64}, {"trace_relpath": "x"}):
        forged = S.ArchiveRecoveryInputs(**{**real.__dict__, **change})
        with pytest.raises(S.RecoveryBlocked):
            S.recover_from_archive(restored23.root, ingress=oi.local_suspension_control("recover_from_archive"),
                                   inputs=forged, **_acks())
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE


# ── preconditions and authority (P1-P5, TC2-05) ───────────────────────────

@pytest.mark.parametrize("make", [
    lambda: oi.local_text_submission(),
    lambda: oi.telegram_relayed_text("e1"),
    lambda: oi.local_runtime_action("approve"),
    lambda: oi.local_suspension_control("recover"),
    lambda: oi.local_suspension_control("resume"),
    lambda: oi.local_suspension_control("wire_seat_release"),
    lambda: oi.local_suspension_control("owner_pin_setup"),
    lambda: None,
    lambda: SimpleNamespace(kind=oi.IngressKind.LOCAL_SUSPENSION_CONTROL, event_id="recover_from_archive",
                            _mint=object()),
], ids=["typed_chat", "telegram", "runtime_action", "recover", "resume", "wire_release",
        "pin_setup", "none", "look_alike"])
def test_recover_from_archive_refuses_every_other_envelope(restored23, make):
    with pytest.raises(S.NotAuthorized):
        S.recover_from_archive(restored23.root, ingress=make(),
                               inputs=ar.archive_recovery_inputs(restored23.root), **_acks())
    _enroll()
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        R.recover_from_archive(ingress=make(), pin=PIN, inputs=ar.archive_recovery_inputs(restored23.root),
                               **_acks())
    assert info.value.code == "not_authorized"
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE


def test_the_new_envelopes_are_distinct_from_every_existing_one():
    recover, setup = oi.local_suspension_control("recover_from_archive"), oi.local_suspension_control("owner_pin_setup")
    assert oi.may_recover_from_archive(recover) and not oi.may_recover_from_archive(setup)
    assert oi.may_set_owner_pin(setup) and not oi.may_set_owner_pin(recover)
    for env in (recover, setup):
        assert not oi.may_release_wire_seat(env)
    release = oi.local_suspension_control("wire_seat_release")
    for env in (release, oi.local_suspension_control("recover"), oi.local_text_submission()):
        assert not oi.may_recover_from_archive(env) and not oi.may_set_owner_pin(env)


def test_each_new_envelope_is_minted_once_from_a_literal_in_the_dialog():
    mints = {}
    for path in [REPO / "main.py", *sorted((REPO / "core").rglob("*.py")), *sorted((REPO / "ui").rglob("*.py")),
                 *sorted((REPO / "comms").rglob("*.py")), *sorted((REPO / "tools").rglob("*.py"))]:
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "local_suspension_control"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                mints.setdefault(node.args[0].value, []).append(rel)
    assert mints["recover_from_archive"] == ["ui/suspension_dialog.py"]
    assert mints["owner_pin_setup"] == ["ui/suspension_dialog.py"]


def test_recover_needs_both_acknowledgements_and_a_root_awaiting_recovery(restored23, tmp_path):
    for acks in ({"acknowledge_unreadable_runtimes": True, "acknowledge_prior_state_since_archive": False},
                 {"acknowledge_unreadable_runtimes": False, "acknowledge_prior_state_since_archive": True},
                 {"acknowledge_unreadable_runtimes": False, "acknowledge_prior_state_since_archive": False}):
        with pytest.raises(S.RecoveryBlocked):
            S.recover_from_archive(restored23.root, ingress=oi.local_suspension_control("recover_from_archive"),
                                   inputs=ar.archive_recovery_inputs(restored23.root), **acks)
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE
    _core_recover(restored23.root)
    with pytest.raises(S.RecoveryBlocked):           # healthy now: never a second time
        _core_recover(restored23.root)
    native = tmp_path / "native"
    (native / "memory").mkdir(parents=True, mode=0o700)
    with pytest.raises(S.RecoveryBlocked):           # no trace at all
        S.recover_from_archive(native, ingress=oi.local_suspension_control("recover_from_archive"),
                               inputs=S.ArchiveRecoveryInputs(False, None, None, None, None, None, None), **_acks())
    with pytest.raises(TypeError):
        S.recover_from_archive(restored23.root, ingress=oi.local_suspension_control("recover_from_archive"),
                               inputs=SimpleNamespace(), **_acks())


# ── in-flight work, authority replay, hostile records (plan §3.2, §3.3) ───

@pytest.fixture
def v23_in_flight(source):
    data = source[0]
    (data / "memory").chmod(0o700)
    store = S.initialize(data, ingress=oi.local_suspension_control("init"))
    runtime = store.register_runtime()
    for scope in ("channel:discord:123456", S.OWNER_SCOPE):
        store.admit(store.capture(scope), kind=S.LeaseKind.TOOL, name="t", runtime_id=runtime)
    token = store.capture(S.OWNER_SCOPE)
    store.trip(token, trigger=S.Trigger.BREAKER_TRIPPED, evidence={"trip_kind": "stale_streak"})
    store.close()
    verified(source)
    return SimpleNamespace(archive=source[2], token=token, data=data)


def test_archived_in_flight_work_becomes_one_acknowledged_owner_incident(v23_in_flight, target, monkeypatch):
    data_dir, base = target
    ns = _restored(v23_in_flight.archive, data_dir, base, monkeypatch)
    out = _core_recover(ns.root)
    assert out.record_status == S.RECORD_ADOPTED and out.unknown_outcome_leases == 2
    (incident,) = _rows(ns.root, "incidents")
    assert json.loads(incident[4]) == {"reason": "restored_archive_prior_state_unknown",
                                       "unknown_outcome_leases": 2}
    assert incident[9] == 1                               # requires the owner's acknowledgement
    rec = json.loads((pathlib.Path(out.evidence_dir) / S.ARCHIVE_RECOVERY_RECEIPT_NAME).read_text())
    assert rec["in_flight_by_scope_class"] == {"owner": 1, "discord": 1, "wire": 0, "other": 0}
    # The old token, minted by the source's store, is foreign here.
    store = S.open_store(ns.root)
    try:
        with pytest.raises(S.AdmissionRefused) as info:
            store.admit(v23_in_flight.token, kind=S.LeaseKind.TOOL, name="t",
                        runtime_id=store.register_runtime())
        assert info.value.refusal in (S.Refusal.FOREIGN_INCARNATION, S.Refusal.SUSPENDED,
                                      S.Refusal.STALE_TOKEN)
        assert info.value.refusal is not None
    finally:
        store.close()


def test_resume_needs_the_unknown_outcome_acknowledgement_and_then_works(v23_in_flight, target, monkeypatch):
    data_dir, base = target
    ns = _restored(v23_in_flight.archive, data_dir, base, monkeypatch)
    _enroll()
    _runtime_recover(ns.root)
    (view,) = R.operator_view()
    out = R.resume(view, ingress=oi.local_suspension_control("resume"), ack_unknown=False)
    assert out.result is S.ResumeResult.UNKNOWN_NEEDS_ACK
    (view,) = R.operator_view()
    out = R.resume(view, ingress=oi.local_suspension_control("resume"), ack_unknown=True)
    assert out.result is S.ResumeResult.RESUMED and R.operator_view() == ()
    # Channels still start suspended one by one after the owner scope resumed.
    R.stop()
    assert _suspended_on_first_sight(ns.root, "channel:discord:9")


def test_a_source_store_and_a_recovered_store_never_open_in_each_other(vsource, v23, target, tmp_path,
                                                                     monkeypatch):
    data_dir, base = target
    ns = _restored(v23, data_dir, base, monkeypatch)
    _core_recover(ns.root)
    a, b = tmp_path / "copy_a", tmp_path / "copy_b"
    for dst, src_mem, pin_mem in ((a, ns.root / "memory", vsource[0] / "memory"),
                                  (b, vsource[0] / "memory", ns.root / "memory")):
        (dst / "memory").mkdir(parents=True, mode=0o700)
        shutil.copy2(src_mem / S.STORE_NAME, dst / "memory" / S.STORE_NAME)
        shutil.copy2(pin_mem / S.IDENTITY_NAME, dst / "memory" / S.IDENTITY_NAME)
        for p in (dst / "memory").iterdir():
            p.chmod(0o600)
        with pytest.raises(S.StoreFault) as info:
            S.open_store(dst).close()
        assert info.value.fault is S.Fault.SOUL_MISMATCH


def _hostile_record(kept: bytes, how: str) -> bytes:
    record = json.loads(kept)
    if how == "extra_key":
        record["note"] = "SEND_MONEY_NOW_CANARY"
        return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    if how == "noncanonical":
        return json.dumps(record, sort_keys=True, indent=1).encode()
    if how == "owner_clear":
        record["owner_scope"]["suspended"] = False
        return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    if how == "duplicate_key":
        return kept.replace(b'{"', b'{"schema":"x","', 1)
    if how == "huge_epoch":
        record["owner_scope"]["execution_epoch"] = 2 ** 62
        return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    raise AssertionError(how)


@pytest.mark.parametrize("how", ["extra_key", "noncanonical", "duplicate_key", "huge_epoch"])
def test_a_hostile_record_is_never_partly_adopted(restored23, how):
    root = restored23.root
    real = ar.archive_recovery_inputs(root)
    raw = _hostile_record(real.record_bytes, how)
    forged = S.ArchiveRecoveryInputs(**{**real.__dict__, "record_bytes": raw,
                                        "record_sha256": hashlib.sha256(raw).hexdigest()})
    out = S.recover_from_archive(root, ingress=oi.local_suspension_control("recover_from_archive"),
                                 inputs=forged, **_acks())
    assert out.record_status == S.RECORD_INVALID_AT_RECOVER and out.unknown_outcome_leases == 0
    lineage = _lineage(_meta(root))
    assert S.LINEAGE_RECORD_SHA256_KEY not in lineage and S.LINEAGE_CLAIMED_SOURCE_SOUL_KEY not in lineage
    assert "CANARY" not in _all_text(root)


def test_a_valid_record_whose_sha256_is_not_the_receipts_is_not_adopted(restored23):
    root = restored23.root
    real = ar.archive_recovery_inputs(root)
    forged = S.ArchiveRecoveryInputs(**{**real.__dict__, "record_sha256": "0" * 64})
    out = S.recover_from_archive(root, ingress=oi.local_suspension_control("recover_from_archive"),
                                 inputs=forged, **_acks())
    assert out.record_status == S.RECORD_INVALID_AT_RECOVER


def test_a_record_claiming_the_owner_was_clear_still_starts_the_owner_suspended(restored23):
    """Adopted or not, the record can never clear anything (Q1)."""
    root = restored23.root
    real = ar.archive_recovery_inputs(root)
    raw = _hostile_record(real.record_bytes, "owner_clear")
    forged = S.ArchiveRecoveryInputs(**{**real.__dict__, "record_bytes": raw,
                                        "record_sha256": hashlib.sha256(raw).hexdigest()})
    S.recover_from_archive(root, ingress=oi.local_suspension_control("recover_from_archive"),
                           inputs=forged, **_acks())
    assert [(s[0], s[3]) for s in _rows(root, "scopes")] == [(S.OWNER_SCOPE, 1)]


def test_no_archived_string_reaches_a_live_incident_audit_or_operator_view(restored23, vsource):
    root = restored23.root
    _core_recover(root)
    archived = json.loads(_record_of(root, restored23.restore_id).read_bytes())
    claims = {archived["provenance"]["claimed_source_soul_id"],
              archived["provenance"]["claimed_source_incarnation"]}
    text = _all_text(root)
    assert not any(c in text for c in claims)
    store = S.open_store(root)
    try:
        shown = json.dumps([v.evidence for v in store.operator_view()])
    finally:
        store.close()
    assert not any(c in shown for c in claims)


def test_two_restores_of_one_archive_are_two_identities_each_needing_its_own_release(
        v23, tmp_path, monkeypatch):
    roots = []
    for name in ("one", "two"):
        base = tmp_path / f"{name}_install"
        (base / "skills").mkdir(parents=True)
        (base / "personas").mkdir()
        (tmp_path / f"{name}_parent").mkdir()
        ns = _restored(v23, tmp_path / f"{name}_parent" / "data", base, monkeypatch, start=False)
        _core_recover(ns.root)
        roots.append(ns.root)
    m1, m2 = _meta(roots[0]), _meta(roots[1])
    assert m1["soul_id"] != m2["soul_id"] and m1["store_incarnation"] != m2["store_incarnation"]
    assert m1[S.LINEAGE_RESTORE_ID_KEY] != m2[S.LINEAGE_RESTORE_ID_KEY]
    _use(monkeypatch, roots[0])
    _release()
    assert G.seat_verdict(roots[0]).reason == G.RELEASED
    assert G.seat_verdict(roots[1]).reason == G.RECOVERED_UNRELEASED


# ── interruption (A2-6, TC2-06, TA2-01) ──────────────────────────────────

def _crash_when(monkeypatch, fn_name, predicate):
    real = getattr(S, fn_name)

    def wrapper(*a, **k):
        if predicate(*a, **k):
            raise _Crash(fn_name)
        return real(*a, **k)
    monkeypatch.setattr(S, fn_name, wrapper)
    return real


def test_kill_after_the_recovery_receipt_retry_completes_with_lineage(restored23, monkeypatch):
    root = restored23.root
    real = _crash_when(monkeypatch, "_write_new_private", lambda path, data: path.endswith(S.STORE_NAME))
    with pytest.raises(_Crash):
        _core_recover(root)
    monkeypatch.setattr(S, "_write_new_private", real)
    assert S.probe(root).status is S.StoreStatus.ABSENT_WITH_TRACE
    out = _core_recover(root)
    assert out.lineage_written and _lineage(_meta(root))[S.LINEAGE_RESTORE_ID_KEY] == restored23.restore_id


@pytest.mark.parametrize("where", ["create_schema", "inside_the_transaction"])
def test_kill_before_commit_faults_repair_refuses_and_plain_recover_leaves_the_wire_locked(
        restored23, monkeypatch, where):
    root = restored23.root
    if where == "create_schema":
        real = _crash_when(monkeypatch, "_create_schema", lambda *a, **k: True)
        restore_name, restore_fn = "_create_schema", real
    else:
        real = _crash_when(monkeypatch, "_insert_incident", lambda *a, **k: True)
        restore_name, restore_fn = "_insert_incident", real
    with pytest.raises(_Crash):
        _core_recover(root)
    monkeypatch.setattr(S, restore_name, restore_fn)
    probe = S.probe(root)
    assert probe.status is S.StoreStatus.FAULT and probe.fault is S.Fault.IDENTITY_MISSING
    with pytest.raises(S.RecoveryBlocked, match="damaged"):
        S.repair_identity(root, ingress=oi.local_suspension_control("repair_identity"))
    R._reopen_after_owner_action()
    blocker = R.wire_release_blocker()
    assert blocker is not None and blocker.code == "c1_recovery_interrupted"
    assert "Repair identity" in blocker.text
    S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unreadable_runtimes=True)
    assert _lineage(_meta(root)) == {}
    R._reopen_after_owner_action()
    assert R.wire_release_blocker().code == "c1_recovery_unavailable"
    assert G.process_seat_verdict().reason == G.RECOVERED_UNRELEASED


def test_kill_after_commit_before_the_pin_repair_keeps_the_lineage(restored23, monkeypatch):
    root = restored23.root
    real = _crash_when(monkeypatch, "_write_new_private", lambda path, data: path.endswith(S.IDENTITY_NAME))
    with pytest.raises(_Crash):
        _core_recover(root)
    monkeypatch.setattr(S, "_write_new_private", real)
    probe = S.probe(root)
    assert probe.status is S.StoreStatus.FAULT and probe.fault is S.Fault.IDENTITY_MISSING
    S.repair_identity(root, ingress=oi.local_suspension_control("repair_identity")).close()
    assert _lineage(_meta(root))[S.LINEAGE_RESTORE_ID_KEY] == restored23.restore_id
    R._reopen_after_owner_action()
    assert R.wire_release_blocker() is None


def _insecure_store(root):
    os.chmod(pathlib.Path(root) / "memory" / S.STORE_NAME, 0o644)


def _corrupt_store(root):
    store = pathlib.Path(root) / "memory" / S.STORE_NAME
    for extra in ("-wal", "-shm"):
        pathlib.Path(f"{store}{extra}").unlink(missing_ok=True)
    store.write_bytes(b"not a database" * 64)


@pytest.mark.parametrize("damage", [_insecure_store, _corrupt_store], ids=["insecure", "corrupt"])
def test_only_identity_faults_point_the_owner_to_repair_identity(restored23, damage):
    """TR1-03 (TA2-01, D-5): Repair identity is offered only for the identity
    faults, so no other fault's text may send the owner to it."""
    _enroll()
    _runtime_recover(restored23.root)
    damage(restored23.root)
    R._reopen_after_owner_action()
    snapshot = R.status()
    assert snapshot.state is R.State.FAULT
    assert snapshot.fault not in (S.Fault.IDENTITY_MISSING, S.Fault.IDENTITY_INVALID)
    blocker = R.wire_release_blocker()
    assert blocker is not None and blocker.code == "c1_not_ready"
    assert "Repair identity" not in blocker.text


@pytest.mark.parametrize("point", ["receipt", "store", "schema", "incident", "pin"])
def test_no_kill_point_leaves_an_identity_without_suspensions_or_lineage(restored23, monkeypatch, point):
    root = restored23.root
    preds = {
        "receipt": ("_write_new_private", lambda path, data: path.endswith(S.ARCHIVE_RECOVERY_RECEIPT_NAME)),
        "store": ("_write_new_private", lambda path, data: path.endswith(S.STORE_NAME)),
        "schema": ("_create_schema", lambda *a, **k: True),
        "incident": ("_insert_incident", lambda *a, **k: True),
        "pin": ("_write_new_private", lambda path, data: path.endswith(S.IDENTITY_NAME)),
    }
    name, pred = preds[point]
    _crash_when(monkeypatch, name, pred)
    with pytest.raises(_Crash):
        _core_recover(root)
    if S.probe(root).status is S.StoreStatus.HEALTHY:
        raise AssertionError("a crash must not leave a healthy store")
    db = root / "memory" / S.STORE_NAME
    tables = set()
    if db.exists() and db.stat().st_size:
        conn = _db(root)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    if "meta" in tables:                       # a rolled-back schema leaves no tables: no identity
        meta = _meta(root)
        if "soul_id" in meta:
            assert meta[S.LINEAGE_RESTORE_ID_KEY] == restored23.restore_id
            assert [(s[0], s[3]) for s in _rows(root, "scopes")] == [(S.OWNER_SCOPE, 1)]


# ── unknown scopes and first-run on restored roots (A2-4, TA2-04) ────────

def test_u1_refused_after_recover_from_archive_and_channels_stay_suspended(restored23):
    root = restored23.root
    _enroll()
    _runtime_recover(root)
    with pytest.raises(S.RecoveryBlocked, match="every new channel suspended"):
        R.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))
    with pytest.raises(S.NotAuthorized):
        R.acknowledge_unknown_scopes(ingress=None)
    R.stop()
    assert _meta(root)["unknown_scope_policy"] == "suspended"
    assert _suspended_on_first_sight(root, "channel:discord:x")
    assert _suspended_on_first_sight(root, "channel:wire:general")


def test_u1_refused_after_plain_recover_on_a_restored_root(restored23):
    root = restored23.root
    S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unreadable_runtimes=True)
    store = S.open_store(root)
    try:
        with pytest.raises(S.RecoveryBlocked):
            store.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))
        with pytest.raises(S.RecoveryBlocked):
            store.acknowledge_unknown_scopes(ingress=oi.local_text_submission())
    finally:
        store.close()
    assert _meta(root)["unknown_scope_policy"] == "suspended"
    assert _suspended_on_first_sight(root, "channel:wire:general")


@pytest.mark.parametrize("state", ["trace_present", "trace_deleted_marker_kept"])
def test_u2_plain_recover_never_starts_channels_unpaused_on_a_restored_root(restored23, state):
    root = restored23.root
    if state == "trace_deleted_marker_kept":
        restored23.trace_path.unlink()
    with pytest.raises(S.RecoveryBlocked, match="every new channel suspended"):
        S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                                acknowledge_unreadable_runtimes=True, acknowledge_unknown_scopes=True)
    assert not (root / "memory" / S.STORE_NAME).exists()


def _plain_recover_after_a_lost_pin(root):
    store = S.open_store(root)
    store.close()
    (pathlib.Path(root) / "memory" / S.IDENTITY_NAME).unlink()
    assert S.probe(root).fault is S.Fault.IDENTITY_MISSING
    S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unreadable_runtimes=True)


@pytest.mark.parametrize("how", ["kill_before_the_pin", "pin_lost_after_recover"])
def test_sc2_01_plain_recover_with_complete_salvage_keeps_channels_suspended(restored23, monkeypatch, how):
    """SC2-01 (independent R0 review): when the old store salvages completely,
    plain Recover used to give policy clear on a restored copy."""
    root = restored23.root
    if how == "kill_before_the_pin":
        real = _crash_when(monkeypatch, "_write_new_private", lambda path, data: path.endswith(S.IDENTITY_NAME))
        with pytest.raises(_Crash):
            _core_recover(root)
        monkeypatch.setattr(S, "_write_new_private", real)
        S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                                acknowledge_unreadable_runtimes=True)
    else:
        _core_recover(root)
        _plain_recover_after_a_lost_pin(root)
    assert _meta(root)["unknown_scope_policy"] == "suspended"
    assert _suspended_on_first_sight(root, "channel:discord:x")
    assert _suspended_on_first_sight(root, "channel:wire:general")
    store = S.open_store(root)
    try:
        with pytest.raises(S.RecoveryBlocked):
            store.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))
    finally:
        store.close()


def test_a_native_root_with_complete_salvage_keeps_todays_policy(tmp_path):
    root = tmp_path / "native"
    (root / "memory").mkdir(parents=True, mode=0o700)
    S.initialize(root, ingress=oi.local_suspension_control("initialize")).close()
    _plain_recover_after_a_lost_pin(root)
    assert _meta(root)["unknown_scope_policy"] == "clear"


def test_u1_refused_on_a_released_copy(restored23):
    _enroll()
    _runtime_recover(restored23.root)
    for view in R.operator_view():
        R.resume(view, ingress=oi.local_suspension_control("resume"), ack_unknown=True)
    _release()
    assert G.process_seat_verdict().reason == G.RELEASED
    with pytest.raises(S.RecoveryBlocked):
        R.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))


def test_a_store_with_restore_lineage_stays_restored_even_if_every_record_is_deleted(restored23):
    root = restored23.root
    _core_recover(root)
    shutil.rmtree(root / "restore_history")
    for child in (root / "memory" / S.EVIDENCE_DIRNAME).iterdir():
        if (child / G.TRACE_NAME).exists():
            shutil.rmtree(child)
    assert G.seat_verdict(root).reason == G.NO_TRACE          # the hand edit fools the gate...
    store = S.open_store(root)
    try:
        with pytest.raises(S.RecoveryBlocked):                  # ...but not C1: lineage is restore
            store.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))
    finally:
        store.close()


def test_unreadable_restore_markers_count_as_restored(tmp_path):
    root = tmp_path / "native"
    (root / "memory").mkdir(parents=True, mode=0o700)
    store = S.initialize(root, ingress=oi.local_suspension_control("initialize"))
    store.close()
    hist = root / "restore_history"
    hist.mkdir(mode=0o700)
    (hist / "not-a-restore-id").mkdir(mode=0o700)
    assert G.seat_verdict(root).reason == G.EVIDENCE_UNREADABLE
    store = S.open_store(root)
    try:
        with pytest.raises(S.RecoveryBlocked):
            store.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))
    finally:
        store.close()


def test_a_native_root_keeps_todays_unknown_scope_behaviour(tmp_path):
    root = tmp_path / "native"
    (root / "memory").mkdir(parents=True, mode=0o700)
    S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unknown_prior_state=True)
    store = S.open_store(root)
    try:
        store.acknowledge_unknown_scopes(ingress=oi.local_suspension_control("acknowledge_unknown_scopes"))
    finally:
        store.close()
    assert _meta(root)["unknown_scope_policy"] == "clear"


def test_initialize_is_refused_on_a_restored_root_whose_evidence_was_deleted(restored23):
    root = restored23.root
    shutil.rmtree(root / "memory" / S.EVIDENCE_DIRNAME)
    assert S.probe(root).status is S.StoreStatus.ABSENT_NO_TRACE
    assert G.seat_verdict(root).reason == G.TRACE_MISSING
    with pytest.raises(S.RecoveryBlocked, match="restored from a backup"):
        S.initialize(root, ingress=oi.local_suspension_control("initialize"))
    assert not (root / "memory" / S.STORE_NAME).exists()


# ── audit vocabulary and snapshots of a recovered root (A2-5, TA2-07) ────

def _snapshot_redactions(root):
    state = c1.capture_recovery_state(root)
    record = json.loads(state.payload)
    c1.validate_recovery_state(record)
    return record["redactions"]


@pytest.mark.parametrize("make", [None, _skipped, _damaged, _tampered_after_restore],
                         ids=["adopted", "skipped", "damaged", "invalid_at_recover"])
def test_a_recovered_root_snapshots_cleanly_for_every_record_status(v23, target, tmp_path, monkeypatch, make):
    data_dir, base = target
    ns = (_restored(v23, data_dir, base, monkeypatch) if make is None
          else make(v23, data_dir, base, monkeypatch, tmp_path))
    _core_recover(ns.root)
    redactions = _snapshot_redactions(ns.root)
    assert not any("[unknown_key]" in k for k in redactions), redactions
    assert {k for k in redactions if k.startswith("audit.detail.")} <= {"audit.detail.new_incarnation"}
    assert not any(k.startswith("incidents.evidence") for k in redactions), redactions


def test_the_unpaired_baseline_also_snapshots_cleanly(restored23):
    _break_receipt(restored23, "corrupt")
    _core_recover(restored23.root, acknowledge_no_lineage=True)
    redactions = _snapshot_redactions(restored23.root)
    assert {k for k in redactions if k.startswith("audit.detail.")} <= {"audit.detail.new_incarnation"}


def test_the_golden_v1_record_still_validates_after_the_append():
    golden = json.loads((REPO / "tests" / "fixtures" / "c1_recovery_state_v1_golden.json").read_bytes())
    S.validate_recovery_state(golden)
    assert "restored_from_archive" in S._AUDIT_ACTIONS
    assert {a for a in S._AUDIT_ACTIONS} - {"restored_from_archive"} == {
        "initialize", "runtime_registered", "trip_foreign_incarnation", "trip_invalid_token",
        "late_trip_evidence", "trip_while_suspended", "trip", "scope_created_suspended", "resume",
        "resume_refused", "unknown_scopes_acknowledged", "identity_repaired", "no_trace_recovery",
        "destructive_recovery"}


# ── owner PIN enrollment (A2-1, A2-R1, PS-1..10, TA2-05/06) ──────────────

@pytest.mark.parametrize("make", [
    lambda: oi.local_text_submission(), lambda: oi.telegram_relayed_text("e"),
    lambda: oi.local_runtime_action("x"), lambda: None,
    lambda: oi.local_suspension_control("recover"), lambda: oi.local_suspension_control("resume"),
    lambda: oi.local_suspension_control("wire_seat_release"),
    lambda: oi.local_suspension_control("recover_from_archive"),
], ids=["typed_chat", "telegram", "runtime_action", "none", "recover", "resume", "wire_release",
        "recover_from_archive"])
def test_pin_setup_refuses_every_other_envelope(restored23, make):
    with pytest.raises(R.OwnerPinRefused) as info:
        R.set_owner_pin(ingress=make(), pin=PIN, confirm=PIN)
    assert info.value.code == "not_authorized"
    assert pin_gate.owner_pin_state() == "unset"


def test_pin_setup_is_set_once_and_never_verifies(restored23):
    _enroll()
    assert pin_gate.owner_pin_state() == "set"
    assert pin_gate._session_state["verified_channels"] == set()
    for channel in ("default", R.RECOVER_PIN_CHANNEL, "discord-1"):
        assert not pin_gate.is_verified(channel)
    before = pathlib.Path(persistence.PREFS_PATH).read_bytes()
    with pytest.raises(R.OwnerPinRefused) as info:
        _enroll("654321")
    assert info.value.code == "already_set"
    assert pathlib.Path(persistence.PREFS_PATH).read_bytes() == before


@pytest.mark.parametrize("content", [b"{not json", b"[]", b'{"pin_hash": 7}', b'{"pin_hash": "' + b"a" * 64 + b'"}',
                                     b'{"pin_salt": "00"}', b'{"pin_hash": "", "pin_salt": ""}'],
                         ids=["corrupt", "list_root", "hash_int", "hash_without_salt", "salt_without_hash",
                              "empty_strings"])
def test_unreadable_or_half_present_pin_state_refuses_and_writes_nothing(restored23, content):
    prefs = pathlib.Path(persistence.PREFS_PATH)
    prefs.write_bytes(content)
    assert pin_gate.owner_pin_state() == "unreadable"
    with pytest.raises(R.OwnerPinRefused) as info:
        _enroll()
    assert info.value.code == "unreadable" and "restore it again into a new folder" in info.value.text
    assert prefs.read_bytes() == content
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(restored23.root)
    assert info.value.code == "pin_unreadable"


_GOOD_HASH = "a" * 64

# TR1-01: shapes a hand edit could leave. Each one must read as "unreadable",
# never as "set", never raise, never count as a PIN guess and never be replaced.
_MALFORMED_PIN_PAIRS = {
    "odd_salt_3": {"pin_hash": _GOOD_HASH, "pin_salt": "abc"},
    "odd_salt_1": {"pin_hash": _GOOD_HASH, "pin_salt": "a"},
    "uppercase_salt": {"pin_hash": _GOOD_HASH, "pin_salt": "AB" * 16},
    "non_hex_salt": {"pin_hash": _GOOD_HASH, "pin_salt": "zz" * 16},
    "salt_trailing_space": {"pin_hash": _GOOD_HASH, "pin_salt": "00" * 16 + " "},
    "salt_int": {"pin_hash": _GOOD_HASH, "pin_salt": 7},
    "salt_list": {"pin_hash": _GOOD_HASH, "pin_salt": ["00"]},
    "salt_null": {"pin_hash": _GOOD_HASH, "pin_salt": None},
    "hash_list": {"pin_hash": [_GOOD_HASH], "pin_salt": "00" * 16},
    "hash_dict": {"pin_hash": {"h": _GOOD_HASH}, "pin_salt": "00" * 16},
    "hash_uppercase": {"pin_hash": "A" * 64, "pin_salt": "00" * 16},
    "hash_63": {"pin_hash": "a" * 63, "pin_salt": "00" * 16},
    "hash_newline": {"pin_hash": _GOOD_HASH + "\n", "pin_salt": "00" * 16},
}


@pytest.mark.parametrize("shape", sorted(_MALFORMED_PIN_PAIRS))
def test_a_malformed_stored_pin_pair_fails_closed_everywhere_and_is_never_replaced(restored23, shape):
    from tools.pin import register_pin_tools
    root = restored23.root
    prefs = pathlib.Path(persistence.PREFS_PATH)
    content = json.dumps(_MALFORMED_PIN_PAIRS[shape]).encode()
    prefs.write_bytes(content)
    data_before = {f["rel"]: hashlib.sha256((root / f["rel"]).read_bytes()).hexdigest()
                   for f in restored23.receipt["data_files"] if f["rel"] != "memory/prefs.json"}
    assert pin_gate.owner_pin_state() == "unreadable"
    # The non-owner tool path (submit_pin) and verify_pin itself: refused, never a raw error,
    # and not counted as a guess.
    ok, message = pin_gate.verify_pin(PIN, channel_id="discord-tr1")
    assert ok is False and message
    assert not pin_gate.is_verified("discord-tr1") and pin_gate._session_state["attempts"].get("discord-tr1", []) == []
    registry = SimpleNamespace(tools={})
    registry.register = lambda name, fn, **_kw: registry.tools.__setitem__(name, fn)
    register_pin_tools(registry, channel_id="discord-tr1")
    assert registry.tools["submit_pin"](PIN).startswith("PIN rejected:")
    assert not pin_gate.is_verified("discord-tr1")
    # Never permission to overwrite: setup refuses with the restore-again way out (DR-3).
    with pytest.raises(R.OwnerPinRefused) as info:
        _enroll()
    assert info.value.code == "unreadable" and "restore it again into a new folder" in info.value.text
    assert pin_gate.set_initial_owner_pin(PIN) == "already_set"
    assert prefs.read_bytes() == content
    # Recover refuses plainly and changes nothing.
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(root)
    assert info.value.code == "pin_unreadable" and "restore it again into a new folder" in info.value.text
    assert not pin_gate.is_verified(R.RECOVER_PIN_CHANNEL)
    assert S.probe(root).status is S.StoreStatus.ABSENT_WITH_TRACE
    # The restored data stays readable and untouched whatever the PIN state (owner requirement 2).
    assert _db_digest(root / "memory" / "lumina.db")
    assert {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in data_before} == data_before


def test_a_pin_set_by_the_legacy_set_pin_still_reads_as_set_and_verifies(restored23):
    pin_gate.set_pin("legacy-pin-1")
    assert pin_gate.owner_pin_state() == "set"
    assert pin_gate.verify_pin("legacy-pin-1", channel_id="discord-legacy") == (True, "Verified.")
    assert pin_gate.verify_pin("wrong-pin-1", channel_id="discord-other")[1] == "Incorrect PIN."


@pytest.mark.parametrize("now", ["odd_salt", "removed"])
def test_recover_refuses_plainly_when_the_pin_changes_after_its_first_check(restored23, monkeypatch, now):
    """TR1-01: the pair is read strictly, then verify_pin reads it again. If it
    changed in between, the refusal says why instead of "wrong PIN"."""
    _enroll()
    prefs = pathlib.Path(persistence.PREFS_PATH)
    prefs.write_text(json.dumps({"pin_hash": _GOOD_HASH, "pin_salt": "abc"}) if now == "odd_salt" else "{}")
    real, calls = pin_gate.owner_pin_state, []

    def first_says_set():
        calls.append(1)
        return "set" if len(calls) == 1 else real()
    monkeypatch.setattr(pin_gate, "owner_pin_state", first_says_set)
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(restored23.root)
    assert info.value.code == ("pin_unreadable" if now == "odd_salt" else "pin_not_set")
    assert not pin_gate.is_verified(R.RECOVER_PIN_CHANNEL)
    assert pin_gate._session_state["attempts"].get(R.RECOVER_PIN_CHANNEL, []) == []
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE


def test_set_if_unset_itself_is_strict_and_set_once(restored23):
    prefs = pathlib.Path(persistence.PREFS_PATH)
    assert persistence.set_if_unset({"k"}, {"k": 1}) == "set"
    assert persistence.set_if_unset({"k"}, {"k": 2}) == "already_set"
    assert json.loads(prefs.read_text())["k"] == 1
    for bad in (b"{oops", b"[1]", b"\xff\xfe"):
        prefs.write_bytes(bad)
        assert persistence.set_if_unset({"k"}, {"k": 3}) == "unreadable"
        assert prefs.read_bytes() == bad


def test_set_initial_owner_pin_itself_is_set_once(restored23):
    assert pin_gate.set_initial_owner_pin("123456") == "set"
    assert pin_gate.set_initial_owner_pin("654321") == "already_set"
    assert pin_gate.verify_pin("123456", channel_id="t1")[0] is True
    assert pin_gate.verify_pin("654321", channel_id="t2")[0] is False


def test_set_if_unset_adds_only_its_own_keys(restored23):
    prefs = pathlib.Path(persistence.PREFS_PATH)
    before = json.loads(prefs.read_text()) if prefs.exists() else {}
    _enroll()
    after = json.loads(prefs.read_text())
    assert set(after) - set(before) == {"pin_hash", "pin_salt"}
    assert {k: after[k] for k in before} == before


def test_set_if_unset_keeps_a_concurrent_writers_keys(restored23):
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            persistence.update({"wire_autonomy_spend": n})
            n += 1
    t = threading.Thread(target=writer)
    t.start()
    try:
        _enroll()
    finally:
        stop.set()
        t.join()
    persistence.update({"wire_wake_listener": False})
    prefs = json.loads(pathlib.Path(persistence.PREFS_PATH).read_text())
    assert "wire_autonomy_spend" in prefs and pin_gate.owner_pin_state() == "set"


@pytest.mark.parametrize("pin,confirm,code", [
    ("12345", "12345", "too_short"), ("1" * 129, "1" * 129, "too_long"),
    ("123456", "123457", "mismatch"), (" 123456", " 123456", "whitespace"),
    ("123456 ", "123456 ", "whitespace"), ("      ", "      ", "whitespace"), (None, None, "invalid"),
])
def test_pin_input_rules(restored23, pin, confirm, code):
    with pytest.raises(R.OwnerPinRefused) as info:
        R.set_owner_pin(ingress=oi.local_suspension_control("owner_pin_setup"), pin=pin, confirm=confirm)
    assert info.value.code == code and pin_gate.owner_pin_state() == "unset"


@pytest.mark.parametrize("pin", ["123456", "1" * 128, "abcdef", "a1b2c3", "Lumina-2026!"],
                         ids=["six_digits", "max_128", "letters_only", "mixed", "symbols"])
def test_six_digits_and_no_character_class_rule(restored23, pin):
    R.set_owner_pin(ingress=oi.local_suspension_control("owner_pin_setup"), pin=pin, confirm=pin)
    assert pin_gate.owner_pin_state() == "set"


def test_a_pin_that_cannot_be_encoded_is_refused_plainly(restored23):
    bad = "123456\ud800"
    with pytest.raises(R.OwnerPinRefused) as info:
        R.set_owner_pin(ingress=oi.local_suspension_control("owner_pin_setup"), pin=bad, confirm=bad)
    assert info.value.code == "invalid" and pin_gate.owner_pin_state() == "unset"
    _enroll()
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(restored23.root, pin=bad)
    assert info.value.code == "pin_wrong"
    assert not pin_gate.is_verified(R.RECOVER_PIN_CHANNEL)


def test_the_pin_never_appears_in_any_log_label_record_or_audit(restored23, capsys, caplog):
    canary = "Zq9Canary7pin"
    R.set_owner_pin(ingress=oi.local_suspension_control("owner_pin_setup"), pin=canary, confirm=canary)
    with pytest.raises(R.RecoverFromArchiveRefused) as wrong:
        _runtime_recover(restored23.root, pin=canary + "x")
    out = _runtime_recover(restored23.root, pin=canary)
    texts = [capsys.readouterr().out, caplog.text, str(wrong.value), wrong.value.text,
             pathlib.Path(persistence.PREFS_PATH).read_text(), _all_text(restored23.root),
             json.dumps(_meta(restored23.root)),
             (pathlib.Path(out.evidence_dir) / S.ARCHIVE_RECOVERY_RECEIPT_NAME).read_text()]
    assert not any(canary in t for t in texts)


def test_a_restored_copy_never_needs_the_original_installations_pin(v23, target, monkeypatch, tmp_path):
    """DR-1: the source had a PIN; the archive and the restored copy carry none,
    and the source's PIN never verifies on the copy."""
    data_dir, base = target
    ns = _restored(v23, data_dir, base, monkeypatch)
    prefs = ns.root / "memory" / "prefs.json"
    if prefs.exists():
        data = json.loads(prefs.read_text())
        assert "pin_hash" not in data and "pin_salt" not in data
    assert pin_gate.owner_pin_state() == "unset"
    assert pin_gate.verify_pin("any-original-pin")[0] is False
    _enroll()
    out = _runtime_recover(ns.root)
    assert out.lineage_written and R.status().state is R.State.ACTIVE


def test_recover_refuses_without_a_pin_with_a_wrong_pin_and_after_lockout(restored23):
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(restored23.root)
    assert info.value.code == "pin_not_set"
    _enroll()
    for _ in range(pin_gate.MAX_ATTEMPTS - 1):
        with pytest.raises(R.RecoverFromArchiveRefused) as info:
            _runtime_recover(restored23.root, pin="000000")
        assert info.value.code == "pin_wrong"
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(restored23.root, pin="000000")
    assert info.value.code == "pin_locked"
    with pytest.raises(R.RecoverFromArchiveRefused) as info:
        _runtime_recover(restored23.root)        # even the right PIN, while locked out
    assert info.value.code == "pin_locked"
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE


def test_the_verification_is_reset_after_success_and_after_refusal(restored23):
    _enroll()
    with pytest.raises(S.RecoveryBlocked):
        _runtime_recover(restored23.root, acknowledge_prior_state_since_archive=False)
    assert not pin_gate.is_verified(R.RECOVER_PIN_CHANNEL)
    _runtime_recover(restored23.root)
    assert not pin_gate.is_verified(R.RECOVER_PIN_CHANNEL)
    assert R.RECOVER_PIN_CHANNEL not in ("default",) and not R.RECOVER_PIN_CHANNEL.startswith("discord")


def test_a_forgotten_pin_before_recover_means_restoring_again_with_fresh_enrollment(
        v23, tmp_path, monkeypatch):
    roots = []
    for name in ("first", "second"):
        base = tmp_path / f"{name}_install"
        (base / "skills").mkdir(parents=True)
        (base / "personas").mkdir()
        (tmp_path / f"{name}_parent").mkdir()
        roots.append(_restored(v23, tmp_path / f"{name}_parent" / "data", base, monkeypatch))
        if name == "first":
            _enroll("111111")                      # ...and then forgotten
    assert pin_gate.owner_pin_state() == "unset"   # the second copy starts with none
    _enroll("222222")
    out = _runtime_recover(roots[1].root, pin="222222")
    assert out.lineage_written


def test_pin_setup_is_refused_outside_a_restored_copy_awaiting_recovery(restored23, tmp_path, monkeypatch):
    _enroll()
    _runtime_recover(restored23.root)
    prefs = pathlib.Path(persistence.PREFS_PATH)
    prefs.write_text("{}")                          # even with no PIN on file now
    with pytest.raises(R.OwnerPinRefused) as info:
        _enroll()
    assert info.value.code == "not_awaiting_recovery"
    native = tmp_path / "native"
    (native / "memory").mkdir(parents=True, mode=0o700)
    S.initialize(native, ingress=oi.local_suspension_control("initialize")).close()
    _use(monkeypatch, native)
    with pytest.raises(R.OwnerPinRefused) as info:
        _enroll()
    assert info.value.code == "not_awaiting_recovery"


def _second_trace(ns, shape):
    """Give a restored root a second trace file in its own evidence folder."""
    evidence = ns.root / "memory" / "suspension_evidence"
    folder = evidence / "zz-second"
    folder.mkdir(mode=0o700)
    raw = ns.trace_path.read_bytes()
    if shape == "two_restores":
        trace = json.loads(raw)
        trace["restore_id"] = "f" * 32
        raw = json.dumps(trace, sort_keys=True, separators=(",", ":")).encode()
    elif shape == "valid_plus_invalid":
        raw = b"{}"
    fd = os.open(folder / "restore_trace.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


_SECOND_TRACE_VERDICT = {"two_restores": G.RECOVERED_UNRELEASED, "valid_plus_invalid": G.TRACE_INVALID,
                         "copied_trace": G.RECOVERED_UNRELEASED}


@pytest.mark.parametrize("shape", sorted(_SECOND_TRACE_VERDICT))
def test_awaiting_recovery_needs_exactly_one_valid_trace_in_one_folder(restored23, shape):
    """TR1-03 (D1-2, TA2-05): the store probe alone is not enough. A root that
    Recover would refuse never shows PIN setup or Recover from this restore."""
    root = restored23.root
    assert R.awaiting_archive_recovery() and G.single_trace(root) is not None
    _second_trace(restored23, shape)
    assert S.probe(root).status is S.StoreStatus.ABSENT_WITH_TRACE   # only the gate clause decides
    assert G.seat_verdict(root).reason == _SECOND_TRACE_VERDICT[shape]
    assert G.single_trace(root) is None
    assert R.awaiting_archive_recovery() is False
    with pytest.raises(R.OwnerPinRefused) as info:
        _enroll()
    assert info.value.code == "not_awaiting_recovery"
    assert pin_gate.owner_pin_state() == "unset"


def test_only_submit_pin_the_tier_gate_and_the_recover_wrapper_consult_the_pin():
    callers = set()
    for path in [*sorted((REPO / "core").rglob("*.py")), *sorted((REPO / "tools").rglob("*.py")),
                 *sorted((REPO / "ui").rglob("*.py")), *sorted((REPO / "comms").rglob("*.py")),
                 REPO / "main.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", getattr(node.func, "id", None))
                    if name in ("verify_pin", "is_verified"):
                        callers.add((path.relative_to(REPO).as_posix(), fn.name))
    files = {f for f, _ in callers}
    assert files == {"tools/pin.py", "core/agent.py", "core/suspension_runtime.py"}, callers
    assert {fn for f, fn in callers if f == "core/suspension_runtime.py"} == {"recover_from_archive"}


def test_only_the_set_once_path_and_legacy_set_pin_write_the_pin_keys():
    tree = ast.parse((REPO / "core" / "pin_gate.py").read_text(encoding="utf-8"))
    writers = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                name = getattr(node.func, "attr", getattr(node.func, "id", None)) if isinstance(node, ast.Call) else None
                if name in ("set_if_unset", "save", "save_prefs"):
                    writers.add(fn.name)
    assert writers == {"set_pin", "set_initial_owner_pin"}
    callers = []
    for path in sorted((REPO / "core").rglob("*.py")) + sorted((REPO / "ui").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("set_pin", "set_initial_owner_pin",
                                                                                 "set_if_unset"):
                callers.append((path.relative_to(REPO).as_posix(), node.func.attr))
    assert sorted(set(callers)) == [("core/pin_gate.py", "set_if_unset"),
                                    ("core/suspension_runtime.py", "set_initial_owner_pin")]


# ── release-time trace check (A2-3) ───────────────────────────────────────

def _recovered_and_resumed(ns):
    _enroll()
    _runtime_recover(ns.root)


def test_the_release_compares_the_trace_hash_recorded_at_recover(restored23):
    _recovered_and_resumed(restored23)
    assert R.wire_release_blocker() is None
    trace = json.loads(restored23.trace_path.read_bytes())
    trace["at"] = trace["at"] + 1
    restored23.trace_path.chmod(0o600)
    restored23.trace_path.write_bytes(json.dumps(trace, sort_keys=True, separators=(",", ":")).encode())
    assert G.process_seat_verdict().reason == G.RECOVERED_UNRELEASED   # still one valid trace
    with pytest.raises(R.WireReleaseRefused) as info:
        _release()
    assert info.value.code == "c1_trace_changed"
    assert G.process_seat_verdict().reason == G.RECOVERED_UNRELEASED


def test_the_release_refuses_when_the_trace_hash_is_absent(restored23):
    _recovered_and_resumed(restored23)
    conn = sqlite3.connect(str(restored23.root / "memory" / S.STORE_NAME))
    try:
        with conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (S.LINEAGE_TRACE_SHA256_KEY,))
    finally:
        conn.close()
    with pytest.raises(R.WireReleaseRefused) as info:
        _release()
    assert info.value.code == "c1_trace_changed"


def test_the_release_succeeds_when_all_three_lineage_values_match(restored23):
    _recovered_and_resumed(restored23)
    out = _release()
    assert out.restore_id == restored23.restore_id
    assert G.process_seat_verdict().reason == G.RELEASED


# ── the second restore marker (A2-7, TA2-08) ─────────────────────────────

def test_marker_names_match_the_restore_engine():
    assert G.MARKER_DIRNAME == ar.HISTORY_DIRNAME and G.MARKER_RECEIPT_NAME == ar.RECEIPT_NAME


def test_a_real_engine_restore_leaves_a_private_marker(restored23):
    hist = restored23.root / "restore_history"
    child = hist / restored23.restore_id
    for path in (hist, child):
        st = os.lstat(path)
        assert (st.st_mode & 0o777) == 0o700
    st = os.lstat(child / "receipt.json")
    assert (st.st_mode & 0o777) == 0o600 and st.st_nlink == 1


@pytest.mark.parametrize("delete", ["trace_file", "trace_dir", "whole_evidence", "memory_dir"])
def test_deleting_the_trace_keeps_the_wire_locked_with_no_socket(restored23, relay, spies, delete):
    root = restored23.root
    if delete == "trace_file":
        restored23.trace_path.unlink()
    elif delete == "trace_dir":
        shutil.rmtree(restored23.trace_path.parent)
    elif delete == "whole_evidence":
        shutil.rmtree(root / "memory" / S.EVIDENCE_DIRNAME)
    else:
        R.stop()
        shutil.rmtree(root / "memory")
    v = G.process_seat_verdict()
    assert (v.allowed, v.reason) == (False, G.TRACE_MISSING)
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    with pytest.raises(wire.WireSeatQuarantinedError):
        client.ping()
    assert spies.sockets == [] and relay.accepted == 0
    assert "restore record is missing" in G.describe(v)


def test_deleting_the_trace_after_recover_from_archive_keeps_the_wire_locked(restored23):
    _recovered_and_resumed(restored23)
    restored23.trace_path.unlink()
    assert G.process_seat_verdict().reason == G.TRACE_MISSING
    with pytest.raises(R.WireReleaseRefused) as info:
        _release()
    assert info.value.code == "not_locked"           # TRACE_MISSING is never releasable


def test_deleting_the_trace_after_release_stays_released(restored23):
    _recovered_and_resumed(restored23)
    _release()
    restored23.trace_path.unlink()
    assert G.process_seat_verdict().reason == G.RELEASED


def test_a_folder_without_its_receipt_still_marks_the_restore(restored23):
    (restored23.root / "restore_history" / restored23.restore_id / "receipt.json").unlink()
    restored23.trace_path.unlink()
    assert G.seat_verdict(restored23.root).reason == G.TRACE_MISSING


@pytest.mark.parametrize("shape", ["history_symlink", "receipt_is_dir", "receipt_symlink", "non_hex_child",
                                   "child_is_file", "unreadable_history", "receipt_group_readable",
                                   "receipt_hard_linked"])
def test_a_marker_that_is_not_plain_and_private_refuses(restored23, tmp_path, shape):
    root = restored23.root
    hist = root / "restore_history"
    child = hist / restored23.restore_id
    receipt = child / "receipt.json"
    if shape == "history_symlink":
        moved = tmp_path / "elsewhere"
        hist.rename(moved)
        hist.symlink_to(moved)
    elif shape == "receipt_is_dir":
        receipt.unlink()
        receipt.mkdir(mode=0o700)
    elif shape == "receipt_symlink":
        target_file = tmp_path / "r.json"
        target_file.write_bytes(receipt.read_bytes())
        receipt.unlink()
        receipt.symlink_to(target_file)
    elif shape == "non_hex_child":
        (hist / "Not-A-Restore").mkdir(mode=0o700)
    elif shape == "child_is_file":
        (hist / ("a" * 32)).write_text("x")
    elif shape == "unreadable_history":
        hist.chmod(0o000)
    elif shape == "receipt_group_readable":
        receipt.chmod(0o640)
    elif shape == "receipt_hard_linked":
        os.link(receipt, tmp_path / "extra_link")
    try:
        v = G.seat_verdict(root)
        assert not v.allowed and v.reason in (G.EVIDENCE_UNREADABLE,), (shape, v.reason)
    finally:
        if shape == "unreadable_history":
            hist.chmod(0o700)


def test_an_over_cap_history_refuses(restored23, monkeypatch):
    monkeypatch.setattr(G, "MAX_EVIDENCE_CHILDREN", 1)
    (restored23.root / "restore_history" / ("b" * 32)).mkdir(mode=0o700)
    assert G.seat_verdict(restored23.root).reason == G.OVER_CAP


def test_the_marker_receipt_is_never_opened(restored23, monkeypatch):
    opened = []
    real_open = G.os.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(G.os, "open", spy)
    G.seat_verdict(restored23.root)
    G.single_trace(restored23.root)
    assert not any(p.endswith("receipt.json") for p in opened), opened


def test_a_native_root_without_restore_history_is_unchanged(tmp_path):
    root = tmp_path / "native"
    (root / "memory").mkdir(parents=True, mode=0o700)
    assert G.seat_verdict(root).reason == G.NO_TRACE
    assert not G.is_restored_root(root)
    (root / "restore_history").mkdir(mode=0o700)      # empty: no restore named
    assert G.seat_verdict(root).reason == G.NO_TRACE


# ── owner texts (TA2-02, TA2-09, D1-5, DR-5) ─────────────────────────────

def test_the_pin_setup_text_describes_how_submit_pin_unlocks_tools():
    """TR1-02: the owner unlocks non-owner tools by sending the PIN to Lumina in
    that conversation (tools/pin.py submit_pin). The text says so, and warns
    about shared channels and chat history instead of forbidding the one use."""
    from tools.pin import register_pin_tools
    from ui import suspension_dialog as D
    registry = SimpleNamespace(tools={})
    registry.register = lambda name, fn, **kw: registry.tools.__setitem__(name, kw["description"])
    register_pin_tools(registry)
    assert "provided by the user in conversation" in registry.tools["submit_pin"]
    text = D.PIN_SETUP_TEXT
    assert "never type it into a chat" not in text and "never type" not in text.lower()
    assert "you send it to Lumina in that conversation" in text
    assert "passes it to the PIN check" in text
    assert "never in a shared channel" in text and "stays in that chat's history" in text


def _all_owner_texts():
    from ui import suspension_dialog as D
    texts = (list(G.STATUS_TEXT.values()) + list(G.REMEDY_TEXT.values())
             + list(R._RELEASE_TEXT.values()) + list(R._RECOVER_TEXT.values())
             + list(R._PIN_SETUP_TEXT.values())
             + [S.RESTORED_UNKNOWN_SCOPES_TEXT, S.RESTORED_INITIALIZE_TEXT]
             + [D.ARCHIVE_TITLE_TEXT, D.ARCHIVE_INTRO_TEXT, D.PIN_SETUP_TEXT, D.PIN_UNREADABLE_TEXT,
                D.ARCHIVE_ACK_RUNTIMES_TEXT, D.ARCHIVE_ACK_PRIOR_TEXT, D.ARCHIVE_ACK_NO_LINK_TEXT,
                D.RESTORED_CHANNELS_TEXT] + list(D.RECORD_STATUS_TEXT.values()))
    return texts


def test_the_gate_text_names_no_control_in_any_restored_state():
    text = G.describe(G.SeatVerdict(False, G.RECOVERED_UNRELEASED))
    for control in ("Recover from this restore", "Repair identity", "Set owner PIN", "Review safe recovery",
                    "Allow this copy", "Resume"):
        assert control not in text, control
    assert "Suspension controls" in text


def test_no_owner_text_claims_a_completed_or_full_recovery():
    banned = re.compile(r"(?i)\b(fully recovered|recovery (is )?complete|restored to normal operation|"
                        r"full recovery (is )?(done|proven|complete))\b")
    bad = [t for t in _all_owner_texts() if banned.search(t)]
    assert bad == []
    assert S.FULL_RECOVERY_NOT_PROVEN == "not_proven"


def test_credentials_texts_promise_no_credentials_backup():
    from ui import suspension_dialog as D
    assert "Credentials and your PIN are not in an Agent Backup" in D.PIN_SETUP_TEXT
    assert not any("Credentials Backup" in t for t in _all_owner_texts())


def test_restore_again_texts_say_what_is_and_is_not_kept():
    for text in (G.REMEDY_TEXT[G.TRACE_MISSING], R._RELEASE_TEXT["c1_recovery_unavailable"],
                 R._RELEASE_TEXT["c1_trace_changed"], S.RESTORED_INITIALIZE_TEXT):
        assert "nothing from the backup is lost" in text.lower()
        assert "not carried over" in text
    for code in (G.TRACE_MISSING, G.OVER_CAP, G.EVIDENCE_UNREADABLE):
        assert "restore_history" in G.REMEDY_TEXT[code]


# ── restored data stays accessible (owner requirement 2) ─────────────────

def _db_digest(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def test_restored_data_is_readable_before_any_pin_or_recovery_and_untouched_by_them(restored23, vsource):
    root = restored23.root
    data_files = [f for f in restored23.receipt["data_files"]]
    before = {f["rel"]: hashlib.sha256((root / f["rel"]).read_bytes()).hexdigest() for f in data_files}
    assert pin_gate.owner_pin_state() == "unset" and R.status().state is not R.State.ACTIVE
    counts = _db_digest(root / "memory" / "lumina.db")
    source_counts = _db_digest(vsource[0] / "memory" / "lumina.db")
    assert source_counts and {t: counts.get(t) for t in source_counts} == source_counts
    _enroll()
    _runtime_recover(root)
    after = {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in before
             if rel != "memory/prefs.json"}
    assert after == {k: v for k, v in before.items() if k != "memory/prefs.json"}


# ── the end-to-end recovery proof (plan §3.5) ────────────────────────────

def test_end_to_end_backup_restore_pin_recover_resume_release_and_connect(vsource, target, monkeypatch, relay,
                                                                        tmp_path):
    verified(vsource)                              # a Verified 2.3 Agent Backup of a synthetic Lumina
    archive = vsource[2]
    data_dir, base = target
    ns = _restored(archive, data_dir, base, monkeypatch)
    root = ns.root
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    with pytest.raises(wire.WireSeatQuarantinedError):
        client.ping()
    with pytest.raises(R.WireReleaseRefused) as info:   # release before Recover
        _release()
    assert info.value.code in ("c1_not_ready", "not_locked")
    assert R.awaiting_archive_recovery()
    _enroll("246810")                                # a 6-digit numeric PIN, fresh on this copy
    out = _runtime_recover(root, pin="246810")
    assert out.lineage_written and out.record_status == S.RECORD_ADOPTED
    assert R.status().state is R.State.ACTIVE and not R.awaiting_archive_recovery()
    prefs = json.loads((root / "memory" / "prefs.json").read_text())
    assert not any(k.startswith("wire_") and prefs[k] for k in prefs)   # nothing auto-enabled (W-A)
    with pytest.raises(wire.WireSeatQuarantinedError):
        client.ping()
    for view in R.operator_view():
        R.resume(view, ingress=oi.local_suspension_control("resume"), ack_unknown=True)
    assert R.operator_view() == ()
    _release()
    assert G.process_seat_verdict().reason == G.RELEASED
    assert client.ping() is True and relay.accepted == 1
    # Q5, measured only: a re-backup of the recovered copy.
    try:
        verified((root, base, tmp_path / "rebackup.zip"))
        rebackup = "verified"
    except Exception as exc:          # recorded, not fixed (TS3B-05)
        rebackup = f"{type(exc).__name__}:{getattr(exc, 'code', '')}:{getattr(exc, 'detail', '')}"
    try:
        incomplete((root, base, None), tmp_path / "rebackup_incomplete.zip")
        fallback = "incomplete_state_export_ok"
    except Exception as exc:
        fallback = f"{type(exc).__name__}:{getattr(exc, 'code', '')}:{getattr(exc, 'detail', '')}"
    print(f"[C1-S2 Q5] re-backup of a recovered copy: verified={rebackup} | incomplete={fallback}")
    # The data stays backed up either way: an Incomplete State Export works.
    assert fallback == "incomplete_state_export_ok"


# ── the dialog (offscreen Qt): the cold start on a disposable root ───────

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qapp):
    from ui.suspension_dialog import SuspensionDialog
    made = []

    def make():
        d = SuspensionDialog()
        made.append(d)
        return d
    yield make
    for d in made:
        d.close()


def _shown(widget):
    return not widget.isHidden()


def test_qt_cold_start_pin_then_recover_then_resume_then_release(qapp, dialog, restored23, relay):
    d = dialog()
    assert _shown(d.archive_group) and _shown(d.pin_setup_group)
    assert d.pin_entry.echoMode() == d.pin_entry.EchoMode.Password
    for hidden in (d.recover_btn, d.unknown_scopes_group, d.archive_recover_btn, d.ack_scopes_btn):
        assert not _shown(hidden)
    assert _shown(d.restored_channels_label)
    d.pin_entry.setText("135790")
    d.pin_confirm_entry.setText("135790")
    d.set_pin_btn.click()
    assert d.pin_entry.text() == "" and d.pin_confirm_entry.text() == ""
    assert "Owner PIN set" in d.result_label.text()
    assert "135790" not in d.result_label.text() + d.technical_details.text()
    assert not _shown(d.pin_setup_group) and _shown(d.archive_recover_btn)
    assert not d.archive_recover_btn.isEnabled()
    assert not _shown(d.archive_ack_no_link_group)          # the receipt pairs
    d.archive_pin_entry.setText("135790")
    d.archive_ack_runtimes.setChecked(True)
    d.archive_ack_prior.setChecked(True)
    assert d.archive_recover_btn.isEnabled()
    d.archive_recover_btn.click()
    assert d.archive_pin_entry.text() == ""
    text = d.result_label.text()
    assert "135790" not in text + d.technical_details.text()
    assert "Full recovery is not proven" in text and "new identity" in text
    assert R.status().state is R.State.ACTIVE
    assert not _shown(d.archive_group) and not _shown(d.ack_scopes_btn)
    d.ack_unknown.setChecked(True)
    d.resume_btn.click()
    assert R.operator_view() == ()
    d.wire_ack.setChecked(True)
    assert d.wire_release_btn.isEnabled()
    d.wire_release_btn.click()
    assert G.process_seat_verdict().reason == G.RELEASED
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    assert client.ping() is True


def test_qt_pin_group_is_hidden_on_native_fault_and_recovered_roots(qapp, dialog, restored23, tmp_path,
                                                                     monkeypatch):
    _enroll()
    _runtime_recover(restored23.root)
    d = dialog()
    assert not _shown(d.archive_group) and not _shown(d.pin_setup_group)
    d.close()
    native = tmp_path / "native"
    (native / "memory").mkdir(parents=True, mode=0o700)
    S.initialize(native, ingress=oi.local_suspension_control("initialize")).close()
    _use(monkeypatch, native)
    d = dialog()
    assert not _shown(d.archive_group) and not _shown(d.restored_channels_label)
    d.close()


def test_qt_fault_restored_root_shows_repair_first(qapp, dialog, restored23, monkeypatch):
    _crash_when(monkeypatch, "_create_schema", lambda *a, **k: True)
    with pytest.raises(_Crash):
        _core_recover(restored23.root)
    monkeypatch.undo()
    _use(monkeypatch, restored23.root)
    d = dialog()
    assert not _shown(d.archive_group) and not _shown(d.pin_setup_group)
    assert _shown(d.repair_btn) and _shown(d.recover_btn)
    assert not _shown(d.unknown_scopes_group)
    assert "Repair identity" in d.wire_blocker_label.text()


def test_qt_unpaired_receipt_needs_the_no_link_acknowledgement(qapp, dialog, restored23):
    _break_receipt(restored23, "corrupt")
    _enroll()
    d = dialog()
    assert _shown(d.archive_ack_no_link_group)
    d.archive_pin_entry.setText(PIN)
    d.archive_ack_runtimes.setChecked(True)
    d.archive_ack_prior.setChecked(True)
    assert not d.archive_recover_btn.isEnabled()
    d.archive_ack_no_link.setChecked(True)
    assert d.archive_recover_btn.isEnabled()
    d.archive_recover_btn.click()
    assert "can't use the Wire seat" in d.result_label.text()
    assert _lineage(_meta(restored23.root)) == {}


def test_qt_a_malformed_pin_shows_the_restore_again_text_not_already_set(qapp, dialog, restored23):
    """TR1-01: an odd-length salt is not a PIN. The dialog gives DR-3's way out
    instead of "already set" and a Recover button that can never work."""
    pathlib.Path(persistence.PREFS_PATH).write_text(json.dumps({"pin_hash": _GOOD_HASH, "pin_salt": "abc"}))
    d = dialog()
    assert _shown(d.archive_group) and _shown(d.pin_unreadable_label)
    assert not _shown(d.pin_setup_group) and not _shown(d.archive_recover_btn)
    assert "restore it again into a new folder" in d.pin_unreadable_label.text()


def test_qt_an_unexpected_recover_error_is_shown_and_the_dialog_refreshes(qapp, dialog, restored23, monkeypatch):
    """TR1-01: the slot never fails silently after clearing the PIN entry."""
    _enroll()
    d = dialog()

    def boom(**_kw):
        raise ValueError("synthetic")
    monkeypatch.setattr(R, "recover_from_archive", boom)
    refreshed = []
    real_refresh = d.refresh
    monkeypatch.setattr(d, "refresh", lambda: (refreshed.append(1), real_refresh()))
    d.archive_pin_entry.setText(PIN)
    d.archive_ack_runtimes.setChecked(True)
    d.archive_ack_prior.setChecked(True)
    d._recover_from_archive()               # called directly: an escaping error fails the test
    assert d.archive_pin_entry.text() == ""
    assert d.result_label.text().startswith("Not recovered: the recovery could not be completed.")
    assert "ValueError" in d.technical_details.text() and PIN not in d.result_label.text()
    assert refreshed
    assert S.probe(restored23.root).status is S.StoreStatus.ABSENT_WITH_TRACE


@pytest.mark.parametrize("shape", sorted(_SECOND_TRACE_VERDICT))
def test_qt_a_root_recover_would_refuse_shows_no_pin_setup_or_recover(qapp, dialog, restored23, shape):
    """TR1-03 (TA2-05): the dialog follows awaiting_archive_recovery()."""
    _second_trace(restored23, shape)
    d = dialog()
    for hidden in (d.archive_group, d.pin_setup_group, d.archive_recover_btn):
        assert not _shown(hidden)
