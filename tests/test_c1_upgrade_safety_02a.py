"""LUMINA-C1-UPGRADE-SAFETY-02 Slice A -- recovery when no C1 trace survives.

The desktop told an owner who can't prove a first launch to choose Recover,
and recover_destructively() then refused ABSENT_NO_TRACE as "initialization,
not recovery". Now, on an explicit acknowledgement that earlier state is
unknown, it starts a new restrictive record: nothing restored, new soul and
incarnation, the owner suspended (outcome-unknown acknowledgement needed to
resume), every scope first seen afterwards suspended, and a receipt saying no
evidence survived -- written first, so a crash never looks like a first run.

Every store is disposable under pytest's tmp_path.
"""
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from core import operator_ingress as oi
from core import suspension as S
from core import suspension_runtime as R


def local(action="test"):
    return oi.local_suspension_control(action)


def mem(data_dir):
    return Path(data_dir) / "memory"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def only_incident(store):
    views = store.operator_view()
    assert len(views) == 1
    return views[0]


def resume(store, expectation, **kw):
    return store.resume(S.desktop_recovery_event(local("resume"), expectation, **kw), expectation)


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "data"
    (root / "memory").mkdir(parents=True, mode=0o700)
    return root


def _erase_every_trace(data_dir):
    """What an owner may face: earlier C1 use, but nothing of it left."""
    for name in S._STORE_FILES + (S.IDENTITY_NAME,):
        (mem(data_dir) / name).unlink(missing_ok=True)
    assert S.probe(data_dir).status is S.StoreStatus.ABSENT_NO_TRACE


def _recover_no_trace(data_dir, **kw):
    return S.recover_destructively(data_dir, ingress=local("recover"),
                                   acknowledge_unknown_prior_state=True, **kw)


def _receipt(outcome):
    with open(os.path.join(outcome.evidence_dir, S.NO_TRACE_RECEIPT_NAME), "rb") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# The new path
# ---------------------------------------------------------------------------

def test_no_trace_recovery_without_the_acknowledgement_is_refused_and_writes_nothing(data_dir):
    for flag in (False, None, 1, "yes"):
        with pytest.raises(S.RecoveryBlocked) as exc:
            S.recover_destructively(data_dir, ingress=local(), acknowledge_unknown_prior_state=flag)
        assert "acknowledge" in exc.value.reason
    assert os.listdir(mem(data_dir)) == []


def test_no_trace_recovery_needs_a_local_envelope(data_dir):
    for bad in (None, oi.telegram_relayed_text("telegram:1:2"), oi.local_runtime_action("x")):
        with pytest.raises(S.NotAuthorized):
            S.recover_destructively(data_dir, ingress=bad, acknowledge_unknown_prior_state=True)
    assert os.listdir(mem(data_dir)) == []


def test_no_trace_recovery_never_pre_clears_unknown_channels(data_dir):
    with pytest.raises(S.RecoveryBlocked):
        _recover_no_trace(data_dir, acknowledge_unknown_scopes=True)
    assert os.listdir(mem(data_dir)) == []


def test_no_trace_recovery_starts_a_new_restrictive_record_and_restores_nothing(data_dir):
    outcome = _recover_no_trace(data_dir)
    assert outcome.evidence_survived is False and outcome.salvage_complete is False
    assert outcome.old_incarnation is None and outcome.unknown_outcome_leases == -1
    assert outcome.suspended_scopes == (S.OWNER_SCOPE,) and outcome.unknown_scope_policy == "suspended"

    receipt = _receipt(outcome)
    assert receipt["evidence_survived"] is False and receipt["prior_state"] == "unknown"
    assert receipt["soul_id"] == outcome.soul_id and receipt["store_incarnation"] == outcome.new_incarnation
    assert os.stat(os.path.join(outcome.evidence_dir, S.NO_TRACE_RECEIPT_NAME)).st_mode & 0o077 == 0
    assert os.listdir(outcome.evidence_dir) == [S.NO_TRACE_RECEIPT_NAME]   # nothing passed off as evidence

    assert S.probe(data_dir).status is S.StoreStatus.HEALTHY
    with S.open_store(data_dir) as st:
        assert st.soul_id == outcome.soul_id and st.incarnation == outcome.new_incarnation
        assert st.unknown_scopes_presumed_suspended()
        owner = only_incident(st)
        assert owner.scope == S.OWNER_SCOPE and owner.trigger == S.RECOVERY_TRIGGER and owner.requires_ack
        assert owner.evidence == {"reason": "prior_state_unknown_no_trace"}
        with pytest.raises(S.AdmissionRefused) as exc:
            st.capture(S.OWNER_SCOPE)
        assert exc.value.refusal is S.Refusal.SUSPENDED
        channel = S.channel_scope("telegram", "42")
        with pytest.raises(S.AdmissionRefused) as exc:                  # unknown scopes fail closed
            st.capture(channel)
        assert exc.value.refusal is S.Refusal.SUSPENDED
        actions = [r[2] for r in st.audit_records()]
        assert "no_trace_recovery" in actions and "destructive_recovery" not in actions
        detail = json.loads(next(r[5] for r in st.audit_records() if r[2] == "no_trace_recovery"))
        assert detail["evidence"] == "none_survived" and detail["prior_state"] == "unknown"

        # Review: resuming needs the outcome-unknown acknowledgement.
        assert resume(st, owner.expectation()).result is S.ResumeResult.UNKNOWN_NEEDS_ACK
        assert st.scope_state(S.OWNER_SCOPE).suspended is True
        assert resume(st, owner.expectation(), ack_unknown=True).result is S.ResumeResult.RESUMED
        st.capture(S.OWNER_SCOPE)
        # Channels stay suspended until the owner reviews them too.
        with pytest.raises(S.AdmissionRefused):
            st.capture(S.channel_scope("telegram", "43"))
        st.acknowledge_unknown_scopes(ingress=local())
        st.capture(S.channel_scope("telegram", "44"))


def test_no_trace_recovery_mints_a_new_identity_so_old_tokens_never_run(data_dir):
    with S.initialize(data_dir, ingress=local()) as old:
        old_token = old.capture(S.OWNER_SCOPE)
        old_soul, old_incarnation = old.soul_id, old.incarnation
    _erase_every_trace(data_dir)
    outcome = _recover_no_trace(data_dir)
    assert outcome.soul_id != old_soul and outcome.new_incarnation != old_incarnation
    with S.open_store(data_dir) as st:
        with pytest.raises(S.AdmissionRefused) as exc:
            st.verify(old_token)
        assert exc.value.refusal is S.Refusal.FOREIGN_INCARNATION


def test_every_no_trace_recovery_mints_its_own_identity(data_dir):
    first = _recover_no_trace(data_dir)
    with S.open_store(data_dir) as st:
        resume(st, only_incident(st).expectation(), ack_unknown=True)
        token = st.capture(S.OWNER_SCOPE)
    _erase_every_trace_including_receipts(data_dir)
    second = _recover_no_trace(data_dir)
    assert second.soul_id != first.soul_id and second.new_incarnation != first.new_incarnation
    with S.open_store(data_dir) as st:
        with pytest.raises(S.AdmissionRefused) as exc:
            st.verify(token)
        assert exc.value.refusal is S.Refusal.FOREIGN_INCARNATION


def _erase_every_trace_including_receipts(data_dir):
    shutil.rmtree(mem(data_dir) / S.EVIDENCE_DIRNAME)
    _erase_every_trace(data_dir)


def test_a_handle_on_an_erased_store_cannot_use_the_new_one(data_dir):
    survivor = S.initialize(data_dir, ingress=local())
    try:
        _erase_every_trace(data_dir)
        _recover_no_trace(data_dir)
        with pytest.raises(S.AdmissionRefused) as exc:
            survivor.capture(S.OWNER_SCOPE)
        assert exc.value.refusal is S.Refusal.STORE_FAULT
    finally:
        survivor.close()


def test_after_no_trace_recovery_initialize_is_refused(data_dir):
    _recover_no_trace(data_dir)
    with pytest.raises(S.RecoveryBlocked):
        S.initialize(data_dir, ingress=local())


def test_losing_the_new_store_later_still_leaves_a_trace(data_dir):
    outcome = _recover_no_trace(data_dir)
    for name in S._STORE_FILES + (S.IDENTITY_NAME,):
        (mem(data_dir) / name).unlink(missing_ok=True)
    probe = S.probe(data_dir)
    assert probe.status is S.StoreStatus.ABSENT_WITH_TRACE and S.EVIDENCE_DIRNAME in probe.traces
    assert os.path.exists(os.path.join(outcome.evidence_dir, S.NO_TRACE_RECEIPT_NAME))


# ---------------------------------------------------------------------------
# Crashes and races fail closed
# ---------------------------------------------------------------------------

def test_a_crash_after_the_receipt_leaves_a_trace_not_a_first_run(data_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError("crash")
    monkeypatch.setattr(S, "_create_schema", boom)
    with pytest.raises(OSError):
        _recover_no_trace(data_dir)
    monkeypatch.undo()
    assert S.probe(data_dir).status is not S.StoreStatus.ABSENT_NO_TRACE
    with pytest.raises(S.RecoveryBlocked):
        S.initialize(data_dir, ingress=local())
    outcome = S.recover_destructively(data_dir, ingress=local(), acknowledge_unreadable_runtimes=True)
    assert outcome.suspended_scopes == (S.OWNER_SCOPE,) and outcome.unknown_scope_policy == "suspended"


def test_a_crash_inside_the_creating_transaction_never_leaves_a_clear_owner(data_dir, monkeypatch):
    real = S._insert_incident

    def crash_at_incident(*a, **k):
        raise sqlite3.OperationalError("crash")
    monkeypatch.setattr(S, "_insert_incident", crash_at_incident)
    with pytest.raises(sqlite3.OperationalError):
        _recover_no_trace(data_dir)
    monkeypatch.setattr(S, "_insert_incident", real)
    # The schema rolled back with the incident: no store whose owner is clear.
    assert S.probe(data_dir).status is S.StoreStatus.FAULT
    with pytest.raises(S.RecoveryBlocked):                                # nothing to re-pin
        S.repair_identity(data_dir, ingress=local())
    S.recover_destructively(data_dir, ingress=local(), acknowledge_unreadable_runtimes=True)
    with S.open_store(data_dir) as st:
        assert st.scope_state(S.OWNER_SCOPE).suspended is True


def test_a_crash_before_the_identity_pin_repairs_to_a_suspended_owner(data_dir, monkeypatch):
    real = S._write_new_private

    def no_pin(path, data):
        if path.endswith(S.IDENTITY_NAME):
            raise OSError("crash")
        return real(path, data)
    monkeypatch.setattr(S, "_write_new_private", no_pin)
    with pytest.raises(OSError):
        _recover_no_trace(data_dir)
    monkeypatch.undo()
    probe = S.probe(data_dir)
    assert probe.status is S.StoreStatus.FAULT and probe.fault is S.Fault.IDENTITY_MISSING
    with S.repair_identity(data_dir, ingress=local()) as st:
        assert st.scope_state(S.OWNER_SCOPE).suspended is True
        assert st.unknown_scopes_presumed_suspended()


@pytest.mark.parametrize("crash_at", ["_insert_incident", "_audit"])
def test_a_crash_during_traces_recovery_never_clears_a_known_suspension(data_dir, monkeypatch, crash_at):
    # Pre-existing hole found in this slice: the traces path keeps the old
    # identity pin, so a schema committed before its suspensions opened
    # HEALTHY with the owner clear.
    with S.initialize(data_dir, ingress=local()) as st:
        st.trip(st.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED)
    (mem(data_dir) / S.STORE_NAME).unlink()
    real = getattr(S, crash_at)

    def crash(conn, *a, **k):
        if crash_at == "_audit" and a and a[1] != "destructive_recovery":
            return real(conn, *a, **k)
        raise sqlite3.OperationalError("crash")
    monkeypatch.setattr(S, crash_at, crash)
    with pytest.raises(sqlite3.OperationalError):
        S.recover_destructively(data_dir, ingress=local(), acknowledge_unreadable_runtimes=True)
    monkeypatch.undo()
    assert S.probe(data_dir).status is not S.StoreStatus.HEALTHY
    S.recover_destructively(data_dir, ingress=local(), acknowledge_unreadable_runtimes=True)
    with S.open_store(data_dir) as st:
        assert st.scope_state(S.OWNER_SCOPE).suspended is True


def test_state_appearing_mid_recovery_stops_it_and_is_left_alone(data_dir, monkeypatch):
    real = S._new_evidence_dir

    def racing_initializer(paths, now):
        target = real(paths, now)
        (mem(data_dir) / S.STORE_NAME).write_bytes(b"someone else's store")
        return target
    monkeypatch.setattr(S, "_new_evidence_dir", racing_initializer)
    with pytest.raises(S.RecoveryBlocked):
        _recover_no_trace(data_dir)
    assert (mem(data_dir) / S.STORE_NAME).read_bytes() == b"someone else's store"
    assert not (mem(data_dir) / S.IDENTITY_NAME).exists()


# ---------------------------------------------------------------------------
# Existing paths are unchanged
# ---------------------------------------------------------------------------

def test_fresh_install_initialize_is_unchanged(data_dir):
    with S.initialize(data_dir, ingress=local()) as st:
        assert st.operator_view() == () and not st.unknown_scopes_presumed_suspended()
        st.capture(S.OWNER_SCOPE)


def test_a_healthy_store_is_never_replaced_even_with_the_acknowledgement(data_dir):
    with S.initialize(data_dir, ingress=local()) as st:
        incarnation = st.incarnation
    before = {n: sha(mem(data_dir) / n) for n in (S.STORE_NAME, S.IDENTITY_NAME)}
    with pytest.raises(S.RecoveryBlocked):
        _recover_no_trace(data_dir)
    assert {n: sha(mem(data_dir) / n) for n in before} == before
    assert not (mem(data_dir) / S.EVIDENCE_DIRNAME).exists()
    with S.open_store(data_dir) as st:
        assert st.incarnation == incarnation


def test_absent_with_traces_recovery_still_preserves_and_keeps_its_soul(data_dir):
    with S.initialize(data_dir, ingress=local()) as st:
        soul = st.soul_id
    pin = sha(mem(data_dir) / S.IDENTITY_NAME)
    for name in S._STORE_FILES:
        (mem(data_dir) / name).unlink(missing_ok=True)
    outcome = _recover_no_trace(data_dir, acknowledge_unreadable_runtimes=True)
    assert outcome.evidence_survived is True and outcome.soul_id == soul
    assert sha(os.path.join(outcome.evidence_dir, S.IDENTITY_NAME)) == pin
    assert not os.path.exists(os.path.join(outcome.evidence_dir, S.NO_TRACE_RECEIPT_NAME))


def test_identity_repair_does_not_take_the_no_trace_path(data_dir):
    with S.initialize(data_dir, ingress=local()) as st:
        incarnation = st.incarnation
    (mem(data_dir) / S.IDENTITY_NAME).unlink()
    probe = S.probe(data_dir)
    assert probe.status is S.StoreStatus.FAULT and probe.fault is S.Fault.IDENTITY_MISSING
    with S.repair_identity(data_dir, ingress=local()) as st:
        assert st.incarnation == incarnation


def test_a_surviving_store_never_takes_the_no_trace_path(data_dir):
    with S.initialize(data_dir, ingress=local()) as st:
        soul = st.soul_id
    (mem(data_dir) / S.IDENTITY_NAME).unlink()
    outcome = _recover_no_trace(data_dir)
    assert outcome.evidence_survived is True and outcome.soul_id == soul
    assert os.path.exists(os.path.join(outcome.evidence_dir, S.STORE_NAME))


# ---------------------------------------------------------------------------
# The desktop runtime passes the acknowledgement through; nothing else may
# ---------------------------------------------------------------------------

def test_the_desktop_runtime_recovers_with_no_trace_and_becomes_active(tmp_path):
    root = tmp_path / "no-trace-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    R.start(root, role="desktop", heartbeat=False)
    try:
        assert R.status().probe_status is S.StoreStatus.ABSENT_NO_TRACE
        with pytest.raises(S.RecoveryBlocked):
            R.recover(ingress=local("recover"))
        outcome = R.recover(ingress=local("recover"), acknowledge_unknown_prior_state=True)
        assert outcome.evidence_survived is False
        status = R.status()
        assert status.state is R.State.ACTIVE and status.active_incidents == 1
        with pytest.raises(R.SuspensionRefused):
            R.capture(S.OWNER_SCOPE)
    finally:
        R.stop()


@pytest.mark.parametrize("role", ["cli", "telegram_bridge", "discord_bridge", "eval", "driver"])
def test_only_the_desktop_may_recover_with_no_trace(tmp_path, role):
    root = tmp_path / f"no-trace-{role}"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    R.start(root, role=role, heartbeat=False)
    try:
        with pytest.raises(S.NotAuthorized):
            R.recover(ingress=local("recover"), acknowledge_unknown_prior_state=True)
        assert os.listdir(root / S.MEMORY_DIRNAME) == []
    finally:
        R.stop()


# ---------------------------------------------------------------------------
# Shipped docs describe the fixed behavior (Closure Documentation Law)
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent


def _flat(text):
    return " ".join(text.split())


def test_shipped_docs_no_longer_describe_the_no_trace_refusal_as_current():
    for rel in ("docs/upgrading.md", "docs/security.md", "docs/troubleshooting.md",
                "knowledge_packs/official/lumina_self_knowledge.json", "README.md"):
        text = _flat((REPO / rel).read_text(encoding="utf-8"))
        assert "this is initialization, not recovery" not in text, rel
        assert "Current limitation: Recover" not in text and "Current limitation: recovery" not in text, rel
        assert "recovery limitation when all prior traces" not in text, rel


def test_the_upgrade_guide_quotes_the_dialog_labels_exactly():
    import ast
    tree = ast.parse((REPO / "ui" / "suspension_dialog.py").read_text(encoding="utf-8"))
    joined = " | ".join(_flat(n.value) for n in ast.walk(tree)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str))
    guide = _flat((REPO / "docs" / "upgrading.md").read_text(encoding="utf-8"))
    label = ("I can't confirm this is the first launch. Earlier suspensions, other Lumina processes "
             "and the outcome of earlier work are unknown; start with my work and every channel "
             "suspended for review")
    result = "New safety record started; nothing was restored"
    for needle in (label, result):
        assert needle in joined and needle in guide, needle
