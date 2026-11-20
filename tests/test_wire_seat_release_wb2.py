"""W-B2: owner-controlled Wire seat release for a restored copy.

Design: reports/AGENT-BACKUP-RESTORE-PHASE-B-R1/wb/ WB-PROPOSAL.md, A1, A1-E1,
and WB2-PRECODE-NOTE.md (deviations D-1..D-9). Everything is synthetic: data
roots in tmp_path built by the real restore engine from a synthetic archive,
the real C1 Recover, an in-test fake relay, synthetic tokens. No real data
root, no real relay, no real seat.

C1-S2 boundary (D-1). The release requires the active C1 store's meta to
carry `lineage_restore_id` / `lineage_archive_sha256` for the restore. Only
C1-S2's recover_from_archive (not implemented) will write them, so the
product path refuses today -- tests named `..._production_...` show that.
Tests that need a releasable root call `_synthetic_c1s2_lineage`, a TEST-ONLY
stand-in that writes the two meta rows C1-S2 is specified to write. It is
not product code and nothing in the product calls anything like it.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import config
from core import operator_ingress as oi
from core import persistence
from core import suspension as S
from core import suspension_runtime as R
from core import wire_seat_gate as G
from tools import aivengers_wire as wire

from test_agent_backup_restore_02 import source, v2, target, restore  # noqa: F401
from test_wire_seat_gate_wb1 import (  # noqa: F401  (fixtures + helpers, reused)
    _umask, relay, spies, _bind, _evidence, _put, _release_bytes, _trace_bytes,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
RELEASE = "wire_seat_release"


class _Crash(BaseException):
    """A simulated process death at an injected point (not an OSError)."""


# ── fixtures and helpers ───────────────────────────────────────────────────

def _make_restored(archive, data_dir, base, monkeypatch, *, start=True):
    _plan, receipt = restore(archive, data_dir, base)
    root = pathlib.Path(data_dir)
    trace = json.loads((root / receipt["c1"]["trace"]).read_text())
    ns = SimpleNamespace(root=root, receipt=receipt, restore_id=receipt["restore_id"],
                         archive_sha256=trace["archive_sha256"], archive=pathlib.Path(archive),
                         trace_path=root / receipt["c1"]["trace"])
    if start:
        _use(monkeypatch, ns)
    return ns


def _use(monkeypatch, ns):
    """Bind this process to `ns.root` as a desktop launch of it would."""
    _bind(monkeypatch, ns.root)
    monkeypatch.setattr(persistence, "PREFS_PATH", str(ns.root / "memory" / "prefs.json"))
    R.start(ns.root, role="desktop", heartbeat=False)


@pytest.fixture
def restored(v2, target, monkeypatch):
    data_dir, base = target
    ns = _make_restored(v2, data_dir, base, monkeypatch)
    yield ns
    R.stop()


def _recover():
    """The real owner-local Recover the dialog runs (today's C1 recovery)."""
    R.recover(ingress=oi.local_suspension_control("recover"), acknowledge_unreadable_runtimes=True)
    assert R.status().state is R.State.ACTIVE


def _synthetic_c1s2_lineage(root, restore_id, archive_sha256):
    """TEST-ONLY stand-in for C1-S2: the two lineage meta rows that
    recover_from_archive is specified to write in its creating transaction.
    Nothing in the product writes these keys."""
    conn = sqlite3.connect(os.path.join(root, "memory", S.STORE_NAME))
    try:
        with conn:
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
                (R.LINEAGE_RESTORE_ID_KEY, restore_id),
                (R.LINEAGE_ARCHIVE_SHA256_KEY, archive_sha256)])
    finally:
        conn.close()


@pytest.fixture
def releasable(restored):
    _recover()
    _synthetic_c1s2_lineage(restored.root, restored.restore_id, restored.archive_sha256)
    return restored


def _expected():
    return tuple((rid, sha) for rid, sha, _at in G.process_seat_verdict().unreleased)


_DEFAULT = object()


def _release(*, ingress=_DEFAULT, expected=None, ack=True, ack_sha=None):
    return R.release_wire_seat(
        ingress=oi.local_suspension_control(RELEASE) if ingress is _DEFAULT else ingress,
        expected=_expected() if expected is None else expected,
        acknowledged_source_unverified=ack,
        acknowledgement_sha256=G.release_acknowledgement_sha256() if ack_sha is None else ack_sha)


def _refused(code, **kw):
    with pytest.raises(R.WireReleaseRefused) as info:
        _release(**kw)
    assert info.value.code == code, info.value.code
    assert info.value.text == R._RELEASE_TEXT[code]
    return info.value


def _inventory(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            data = None
            if os.path.isfile(p) and not os.path.islink(p) and not name.startswith("suspension.db"):
                data = hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
            out[os.path.relpath(p, root)] = (st.st_mode, data)
    return out


def _release_files(root):
    ev = _evidence(root)
    return sorted(ev.glob("*/" + G.RELEASE_NAME)) if ev.exists() else []


def _locked(root):
    return G.seat_verdict(root).reason == G.RECOVERED_UNRELEASED


# ── authorization ──────────────────────────────────────────────────────────

def test_owner_gui_release_succeeds_end_to_end_and_the_relay_then_accepts(releasable, monkeypatch, relay):
    root = releasable.root
    assert _locked(root)
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    with pytest.raises(wire.WireSeatQuarantinedError):
        client.ping()
    assert relay.accepted == 0
    outcome = _release()
    assert G.process_seat_verdict().reason == G.RELEASED
    assert outcome.restore_id == releasable.restore_id
    assert client.ping() is True and relay.accepted == 1   # only now, and only on request
    # the same verdict from a fresh process (nothing cached)
    code = ("import sys; sys.path.insert(0, %r); from core import wire_seat_gate as G; "
            "print(G.process_seat_verdict().reason)" % str(REPO))
    env = dict(os.environ, LUMINA_DATA_DIR=str(root))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         check=True, cwd=str(root))
    assert out.stdout.strip() == G.RELEASED


def test_the_release_record_is_exact_private_canonical_and_unverified(releasable):
    outcome = _release()
    path = releasable.root / outcome.record
    raw = path.read_bytes()
    st = os.lstat(path)
    assert path.name == G.RELEASE_NAME and st.st_nlink == 1 and (st.st_mode & 0o777) == 0o600
    assert (os.lstat(path.parent).st_mode & 0o777) == 0o700
    rec = json.loads(raw)
    assert set(rec) == G.RELEASE_KEYS
    assert json.dumps(rec, sort_keys=True, separators=(",", ":")).encode() == raw
    assert rec["source_installation_status"] == "unverified_owner_acknowledged"
    assert rec["ingress"] == "local_suspension_control:wire_seat_release"
    assert rec["acknowledgement_sha256"] == G.release_acknowledgement_sha256()
    assert rec["restore_id"] == releasable.restore_id
    assert rec["restore_trace_sha256"] == hashlib.sha256(releasable.trace_path.read_bytes()).hexdigest()
    with S.open_store(releasable.root) as store:
        assert rec["store_incarnation"] == store.incarnation == outcome.store_incarnation
    assert list(path.parent.iterdir()) == [path]          # the tmp was removed, nothing else
    assert path.parent.parent == _evidence(releasable.root)
    assert path.parent != releasable.trace_path.parent    # a fresh evidence directory


def test_production_path_after_a_real_recover_refuses_until_c1s2(restored):
    """No synthetic lineage: today's Recover is not accepted (D-1)."""
    _recover()
    assert S.probe(restored.root).status is S.StoreStatus.HEALTHY
    before = _inventory(restored.root)
    blocker = R.wire_release_blocker(_expected())
    assert blocker is not None and blocker.code == "c1_recovery_unavailable"
    _refused("c1_recovery_unavailable")
    assert _inventory(restored.root) == before and _release_files(restored.root) == []
    assert _locked(restored.root)


def test_no_product_code_writes_the_c1s2_lineage_keys():
    hits = []
    for rel in _product_py():
        text = (REPO / rel).read_text(encoding="utf-8")
        if "lineage_restore_id" in text or "lineage_archive_sha256" in text:
            hits.append(rel)
    assert hits == ["core/suspension_runtime.py"]  # the reader's constants only
    tree = ast.parse((REPO / "core" / "suspension_runtime.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(r"\b(INSERT|UPDATE|REPLACE)\b", node.value), node.value


@pytest.mark.parametrize("make", [
    lambda: oi.local_text_submission(),
    lambda: oi.telegram_relayed_text("telegram:42:1"),
    lambda: oi.local_runtime_action("image_approval_button"),
    lambda: oi.local_runtime_action(RELEASE),
    lambda: oi.local_suspension_control("recover"),
    lambda: oi.local_suspension_control("resume"),
    lambda: oi.local_suspension_control("wire_seat_release "),
    lambda: None,
    lambda: object(),
    lambda: SimpleNamespace(kind=oi.IngressKind.LOCAL_SUSPENSION_CONTROL, event_id=RELEASE,
                            _mint=None),
], ids=["typed_chat", "telegram", "runtime_action", "runtime_action_named_release", "recover",
        "resume", "near_miss_action", "none", "object", "forged_lookalike"])
def test_every_other_envelope_is_refused_and_writes_nothing(releasable, make):
    before = _inventory(releasable.root)
    _refused("not_authorized", ingress=make())
    assert _inventory(releasable.root) == before and _locked(releasable.root)


def test_may_release_wire_seat_accepts_only_the_button_envelope():
    assert oi.may_release_wire_seat(oi.local_suspension_control(RELEASE)) is True
    for other in (oi.local_text_submission(), oi.telegram_relayed_text("t"),
                  oi.local_runtime_action(RELEASE), oi.local_suspension_control("recover")):
        assert oi.may_release_wire_seat(other) is False
    # may_control_suspension is unchanged and still not the release authority
    assert oi.may_control_suspension(oi.local_text_submission()) is True


def test_a_non_desktop_process_cannot_release(releasable, monkeypatch):
    R.start(releasable.root, role="telegram_bridge", heartbeat=False)
    _refused("not_authorized")
    assert _locked(releasable.root)


@pytest.mark.parametrize("ack, ack_sha", [
    (False, None), (1, None), ("yes", None), (None, None),
    (True, "0" * 64), (True, G.acknowledgement_sha256(G.RELEASE_BODY_TEXT, G.RELEASE_ACK_TEXT, "I agree.")),
    (True, G.release_acknowledgement_sha256().upper()), (True, 42),
], ids=["false", "truthy_int", "truthy_str", "none", "zero_hash", "other_caption", "upper", "not_str"])
def test_the_uncertainty_acknowledgement_is_required_exactly(releasable, ack, ack_sha):
    before = _inventory(releasable.root)
    _refused("not_acknowledged", ack=ack, ack_sha=ack_sha)
    assert _inventory(releasable.root) == before


def test_release_waits_for_safe_recovery(restored):
    assert R.status().state is R.State.UNINITIALIZED
    assert R.wire_release_blocker(_expected()).code == "c1_not_ready"
    _refused("c1_not_ready")
    assert _release_files(restored.root) == []


def test_an_unexpected_error_while_checking_keeps_the_button_off(releasable, monkeypatch):
    assert R.wire_release_blocker(_expected()) is None          # releasable as built
    def broken():
        raise RuntimeError("prefs unreadable")
    monkeypatch.setattr(persistence, "load", broken)
    blocker = R.wire_release_blocker(_expected())
    assert blocker is not None and blocker.code == "c1_not_ready"
    with pytest.raises(RuntimeError):
        _release()
    assert _locked(releasable.root) and _release_files(releasable.root) == []


def test_a_runtime_that_is_not_active_refuses_on_its_own(releasable, monkeypatch):
    """The runtime-state condition alone (store handle and disk both healthy)."""
    import dataclasses
    real = R._status_locked
    monkeypatch.setattr(R, "_status_locked", lambda: dataclasses.replace(real(), state=R.State.FAULT))
    _refused("c1_not_ready")
    assert R.wire_release_blocker(_expected()).code == "c1_not_ready"
    assert _locked(releasable.root)


def test_a_store_probe_that_is_not_healthy_refuses_on_its_own(releasable, monkeypatch):
    """The fresh-probe condition alone (runtime handle still healthy)."""
    monkeypatch.setattr(S, "probe", lambda d: S.StoreProbe(S.StoreStatus.FAULT, S.Fault.INSECURE, ()))
    _refused("c1_not_ready")
    assert _locked(releasable.root)


def test_the_temp_file_is_created_exclusively_and_never_through_a_link(releasable, monkeypatch, tmp_path):
    """A pre-planted symlink at the temp name is refused, never written through."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    fixed = releasable.root / "memory" / "suspension_evidence" / "20261119T000000Z-planted0"
    fixed.mkdir(mode=0o700)
    planted = fixed / f"{G.RELEASE_NAME}.{(b'\x00' * 6).hex()}.tmp"
    os.symlink(victim, planted)
    monkeypatch.setattr(S, "_new_evidence_dir", lambda paths, now: str(fixed))
    monkeypatch.setattr(os, "urandom", lambda n: b"\x00" * n)
    _refused("write_failed")
    assert victim.read_bytes() == b"untouched" and _locked(releasable.root)
    assert os.path.islink(planted)            # not ours: never removed by the cleanup


def test_a_faulted_store_refuses(releasable):
    (releasable.root / "memory" / S.IDENTITY_NAME).unlink()
    _refused("c1_not_ready")
    assert _locked(releasable.root)


@pytest.mark.parametrize("which", ["restore_id", "archive_sha256"])
def test_lineage_for_a_different_restore_refuses(restored, which):
    _recover()
    rid = restored.restore_id if which != "restore_id" else "f" * 32
    arc = restored.archive_sha256 if which != "archive_sha256" else "0" * 64
    _synthetic_c1s2_lineage(restored.root, rid, arc)
    _refused("c1_recovery_unavailable")
    assert _locked(restored.root)


@pytest.mark.parametrize("expected", [
    (("f" * 32, "0" * 64),), (), None, "x",
], ids=["other_pair", "empty", "list_form", "string"])
def test_the_shown_restore_must_equal_a_fresh_scan(releasable, expected):
    if expected is None:
        expected = [list(p) for p in _expected()]   # right content, wrong type
    _refused("changed", expected=expected)
    assert _locked(releasable.root)


def test_the_release_must_name_what_the_owner_was_shown(releasable):
    """WB2-03: no default; a missing `expected` refuses before anything else."""
    with pytest.raises(R.WireReleaseRefused) as info:
        R.release_wire_seat(ingress=oi.local_suspension_control(RELEASE), expected=None,
                            acknowledged_source_unverified=True,
                            acknowledgement_sha256=G.release_acknowledgement_sha256())
    assert info.value.code == "changed"
    with pytest.raises(TypeError):
        R.release_wire_seat(ingress=oi.local_suspension_control(RELEASE),
                            acknowledged_source_unverified=True,
                            acknowledgement_sha256=G.release_acknowledgement_sha256())
    assert _locked(releasable.root) and _release_files(releasable.root) == []


def test_the_process_root_must_be_the_runtime_root(releasable, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    (other / "memory").mkdir(parents=True)
    monkeypatch.setenv("LUMINA_DATA_DIR", str(other))
    monkeypatch.setattr(config, "DATA_DIR", str(other))
    _refused("root_mismatch", expected=())
    monkeypatch.setenv("LUMINA_DATA_DIR", "relative")
    _refused("root_mismatch", expected=())
    assert _locked(releasable.root)


@pytest.mark.parametrize("pref", ["wire_wake_listener", "wire_autonomy"])
def test_wire_features_must_be_off_and_prefs_are_never_written(releasable, pref):
    prefs = releasable.root / "memory" / "prefs.json"
    prefs.write_text(json.dumps({pref: True, "tts_voice": "x"}))
    raw = prefs.read_bytes()
    _refused("wire_prefs_on")
    assert prefs.read_bytes() == raw and _locked(releasable.root)
    prefs.write_text(json.dumps({pref: False, "tts_voice": "x"}))
    raw = prefs.read_bytes()
    _release()
    assert prefs.read_bytes() == raw                  # the release wrote no preference


def test_a_native_root_is_not_offered_a_release(tmp_path, monkeypatch):
    root = tmp_path / "native"
    (root / "memory").mkdir(parents=True, mode=0o700)
    S.initialize(root, ingress=oi.local_suspension_control("initialize")).close()
    _use(monkeypatch, SimpleNamespace(root=root))
    try:
        assert G.process_seat_verdict().reason == G.NO_TRACE
        _refused("not_locked", expected=())
        assert not _evidence(root).exists()
    finally:
        R.stop()


def test_two_restore_records_in_one_root_need_manual_review(releasable):
    _put(releasable.root, "restore_trace.json", _trace_bytes("b" * 32))
    assert len(G.seat_verdict(releasable.root).unreleased) == 2
    _refused("multiple_restores")
    assert _release_files(releasable.root) == []


# ── integrity ──────────────────────────────────────────────────────────────

def test_junk_and_torn_release_evidence_never_blocks_the_owner(releasable):
    trace_dir = releasable.trace_path.parent
    sha = hashlib.sha256(releasable.trace_path.read_bytes()).hexdigest()
    good = _release_bytes(releasable.restore_id, sha)
    (trace_dir / G.RELEASE_NAME).write_bytes(good[: len(good) // 2])   # torn, beside the trace
    (trace_dir / G.RELEASE_NAME).chmod(0o600)
    _put(releasable.root, G.RELEASE_NAME, good[:-1])                    # truncated elsewhere
    _put(releasable.root, G.RELEASE_NAME, good, mode=0o644)             # valid bytes, bad mode
    _put(releasable.root, G.RELEASE_NAME + ".0123456789ab.tmp", good)   # a leftover tmp
    v = G.seat_verdict(releasable.root)
    assert v.reason == G.RECOVERED_UNRELEASED and v.ignored_releases == 3
    junk = _inventory(releasable.root)
    _release()
    assert G.seat_verdict(releasable.root).reason == G.RELEASED
    after = _inventory(releasable.root)
    assert all(after[k] == junk[k] for k in junk)    # append-only: nothing existing touched


def test_a_hard_link_to_a_live_release_is_ignored_and_relocks(releasable):
    outcome = _release()
    record = releasable.root / outcome.record
    alias = releasable.root / "memory" / "suspension_evidence" / "alias"
    alias.mkdir(mode=0o700)
    os.link(record, alias / G.RELEASE_NAME)
    assert _locked(releasable.root)          # nlink 2: neither copy is trusted (fail closed)
    (alias / G.RELEASE_NAME).unlink()
    assert G.seat_verdict(releasable.root).reason == G.RELEASED


def test_a_symlinked_release_is_ignored(releasable, tmp_path):
    sha = hashlib.sha256(releasable.trace_path.read_bytes()).hexdigest()
    outside = tmp_path / "outside.json"
    outside.write_bytes(_release_bytes(releasable.restore_id, sha))
    outside.chmod(0o600)
    child = _evidence(releasable.root) / "20261119T000000Z-linked00"
    child.mkdir(mode=0o700)
    os.symlink(outside, child / G.RELEASE_NAME)
    assert _locked(releasable.root)


def test_a_symlinked_evidence_folder_refuses_and_offers_no_release(releasable, tmp_path):
    ev = _evidence(releasable.root)
    moved = tmp_path / "moved-evidence"
    shutil.move(str(ev), str(moved))
    os.symlink(moved, ev)
    assert G.seat_verdict(releasable.root).reason == G.EVIDENCE_UNREADABLE
    _refused("not_locked", expected=())


def test_a_copied_release_does_not_unlock_another_installation(v2, tmp_path, monkeypatch):
    roots = []
    for name in ("a", "b"):
        data, base = tmp_path / f"{name}-data", tmp_path / f"{name}-base"
        data.mkdir(mode=0o700)
        base.mkdir(mode=0o700)
        roots.append(_make_restored(v2, data, base, monkeypatch, start=False))
    a, b = roots
    assert a.restore_id != b.restore_id
    _use(monkeypatch, a)
    _recover()
    _synthetic_c1s2_lineage(a.root, a.restore_id, a.archive_sha256)
    record = a.root / _release().record
    # B: same archive, its own restore. A's valid release, copied beside B's trace
    # and into its own directory, names A's restore, so it releases nothing in B.
    shutil.copy2(record, b.trace_path.parent / G.RELEASE_NAME)
    _put(b.root, G.RELEASE_NAME, record.read_bytes())
    vb = G.seat_verdict(b.root)
    assert vb.reason == G.RECOVERED_UNRELEASED and [r for r, _, _ in vb.unreleased] == [b.restore_id]
    assert G.seat_verdict(a.root).reason == G.RELEASED
    # B is released only by its own owner action
    _use(monkeypatch, b)
    _recover()
    _synthetic_c1s2_lineage(b.root, b.restore_id, b.archive_sha256)
    _release()
    assert G.seat_verdict(b.root).reason == G.RELEASED
    R.stop()


def test_damaged_restore_evidence_has_no_release(releasable):
    releasable.trace_path.chmod(0o644)
    assert G.seat_verdict(releasable.root).reason == G.TRACE_INVALID
    _refused("not_locked", expected=())
    releasable.trace_path.chmod(0o600)
    raw = releasable.trace_path.read_bytes()
    releasable.trace_path.write_bytes(raw.replace(b'"version":1', b'"version": 1'))
    assert G.seat_verdict(releasable.root).reason == G.TRACE_INVALID
    _refused("not_locked", expected=())
    assert _release_files(releasable.root) == []


@pytest.mark.parametrize("field, value", [
    ("restore_id", "A" * 32), ("restore_id", "a" * 31), ("restore_trace_sha256", "g" * 64),
    ("acknowledgement_sha256", "0" * 63), ("store_incarnation", "x" * 5000),
    ("at", float("nan")), ("at", -1), ("at", "now"),
])
def test_the_record_builder_refuses_anything_its_reader_would_not_accept(field, value):
    kw = dict(at=1.5, restore_id="a" * 32, restore_trace_sha256="b" * 64,
              acknowledgement_sha256="c" * 64, store_incarnation="d" * 32)
    assert G._release_pair(G.release_record_bytes(**kw)) == ("a" * 32, "b" * 64)
    kw[field] = value
    with pytest.raises((ValueError, TypeError)):
        G.release_record_bytes(**kw)


def test_written_records_are_private_whatever_the_umask(releasable):
    old = os.umask(0)
    try:
        outcome = _release()
    finally:
        os.umask(old)
    path = releasable.root / outcome.record
    assert (os.lstat(path).st_mode & 0o777) == 0o600
    assert (os.lstat(path.parent).st_mode & 0o777) == 0o700
    assert G.seat_verdict(releasable.root).reason == G.RELEASED


# ── fault injection: crash points and filesystem refusals ──────────────────

def _inject(monkeypatch, name, at):
    """Wrap os.<name>: run `at(real, *args)` for calls on the release's own
    paths/fds, the real call otherwise."""
    real = getattr(os, name)
    state = SimpleNamespace(fds=set())

    def wrapper(*args, **kwargs):
        target = args[0] if args else None
        ours = (isinstance(target, (str, bytes, os.PathLike))
                and G.RELEASE_NAME in os.fsdecode(target)) or (isinstance(target, int)
                                                               and target in state.fds)
        if name == "open" and ours:
            fd = real(*args, **kwargs)
            state.fds.add(fd)
            return at(real, fd, *args) if at else fd
        if ours and at is not None:
            return at(real, *args, **kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, name, wrapper)
    return state


def _assert_never_torn(root):
    """I-3: any file named wire_release.json is complete and canonical."""
    for path in _release_files(root):
        raw = path.read_bytes()
        assert G._release_pair(raw) is not None, raw


def _retry_reaches_released(root, monkeypatch):
    monkeypatch.undo()
    _bind(monkeypatch, root)
    monkeypatch.setattr(persistence, "PREFS_PATH", str(root / "memory" / "prefs.json"))
    _release()
    assert G.seat_verdict(root).reason == G.RELEASED
    _assert_never_torn(root)


def test_crash_before_the_temp_file_exists(releasable, monkeypatch):
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda path, *a, **k: (_ for _ in ()).throw(_Crash())
                        if G.RELEASE_NAME in os.fsdecode(path) else real_open(path, *a, **k))
    with pytest.raises(_Crash):
        _release()
    assert _locked(releasable.root) and _release_files(releasable.root) == []
    _retry_reaches_released(releasable.root, monkeypatch)


def test_crash_while_writing_leaves_only_a_torn_temp_that_is_never_read(releasable, monkeypatch):
    def half_then_die(real, fd, data):
        real(fd, bytes(data[: len(data) // 2]))
        raise _Crash()
    real_write = os.write
    opened = []
    real_open = os.open

    def open_spy(path, *a, **k):
        fd = real_open(path, *a, **k)
        if isinstance(path, str) and path.endswith(".tmp"):
            opened.append(fd)
        return fd

    def write_spy(fd, data):
        if fd in opened:
            return half_then_die(real_write, fd, data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", open_spy)
    monkeypatch.setattr(os, "write", write_spy)
    with pytest.raises(_Crash):
        _release()
    tmps = list(_evidence(releasable.root).glob("*/*.tmp"))
    assert len(tmps) == 1 and _release_files(releasable.root) == []
    assert _locked(releasable.root)
    _retry_reaches_released(releasable.root, monkeypatch)
    assert tmps[0].exists()                      # left as evidence, never opened by the gate


def test_crash_after_the_temp_is_complete_but_before_the_link(releasable, monkeypatch):
    def die(real, *a, **k):
        raise _Crash()
    _inject(monkeypatch, "link", die)
    with pytest.raises(_Crash):
        _release()
    assert _release_files(releasable.root) == [] and _locked(releasable.root)
    _retry_reaches_released(releasable.root, monkeypatch)


def test_crash_after_the_link_before_the_temp_is_removed(releasable, monkeypatch):
    def die(real, *a, **k):
        raise _Crash()
    _inject(monkeypatch, "unlink", die)
    with pytest.raises(_Crash):
        _release()
    files = _release_files(releasable.root)
    assert len(files) == 1 and os.lstat(files[0]).st_nlink == 2
    _assert_never_torn(releasable.root)
    assert _locked(releasable.root)              # two links: ignored, still fail closed
    _retry_reaches_released(releasable.root, monkeypatch)


def test_crash_after_unlink_before_the_directory_fsyncs(releasable, monkeypatch):
    real = S._fsync_dir

    def die(path):
        raise _Crash()
    monkeypatch.setattr(S, "_fsync_dir", die)
    with pytest.raises(_Crash):
        _release()
    monkeypatch.setattr(S, "_fsync_dir", real)
    _assert_never_torn(releasable.root)
    assert G.seat_verdict(releasable.root).reason == G.RELEASED   # complete record, single link
    _refused("not_locked", expected=())                           # nothing left to release


@pytest.mark.parametrize("err, code", [
    (errno.EPERM, "link_unsupported"), (errno.ENOTSUP, "link_unsupported"),
    (errno.EXDEV, "link_unsupported"), (errno.EMLINK, "link_unsupported"),
    (errno.ENOSPC, "write_failed"), (errno.EDQUOT, "write_failed"), (errno.EACCES, "write_failed"),
])
def test_a_refused_link_is_named_plainly_and_cleaned_up(releasable, monkeypatch, err, code):
    """E1-5 / WB2-04 / WB2-05: no hard links => link_unsupported; other link
    errors are ordinary write failures. Either way this call removes its own
    temp file and empty directory, and nothing else."""
    before = _inventory(releasable.root)
    def refuse(real, *a, **k):
        raise OSError(err, os.strerror(err))
    _inject(monkeypatch, "link", refuse)
    for _ in range(3):                                   # repeated clicks leave no residue
        _refused(code)
    assert _inventory(releasable.root) == before and _locked(releasable.root)
    _retry_reaches_released(releasable.root, monkeypatch)


def test_a_failed_temp_removal_reports_not_visible_and_stays_locked(releasable, monkeypatch):
    def refuse(real, *a, **k):
        raise PermissionError(errno.EACCES, "denied")
    _inject(monkeypatch, "unlink", refuse)
    _refused("not_visible")
    assert _locked(releasable.root)
    _retry_reaches_released(releasable.root, monkeypatch)


def test_a_failed_write_is_refused_plainly(releasable, monkeypatch):
    real_open = os.open

    def nospace(path, *a, **k):
        if isinstance(path, str) and path.endswith(".tmp"):
            raise OSError(errno.ENOSPC, "full")
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", nospace)
    before = _inventory(releasable.root)
    _refused("write_failed")
    assert _inventory(releasable.root) == before and _locked(releasable.root)   # own empty dir removed
    _retry_reaches_released(releasable.root, monkeypatch)


def test_the_writer_rechecks_the_memory_folder_before_writing(releasable):
    """WB2-06/S08: an unsafe memory/ refuses before any directory or file is made."""
    before = _inventory(releasable.root)
    (releasable.root / "memory").chmod(0o770)
    try:
        with pytest.raises(R.WireReleaseRefused) as info:
            R._publish_wire_release(str(releasable.root), b"x")
    finally:
        (releasable.root / "memory").chmod(0o700)
    assert info.value.code == "write_failed"
    assert _inventory(releasable.root) == before


def test_the_writer_removes_only_its_own_temp_and_never_renames(releasable, monkeypatch):
    calls = []
    for name in ("unlink", "remove", "rename", "replace", "rmdir"):
        real = getattr(os, name)
        monkeypatch.setattr(os, name, (lambda n, r: lambda *a, **k: (calls.append((n, a)), r(*a, **k))[1])(name, real))
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: (calls.append(("rmtree", a)), real_rmtree(*a, **k))[1])
    events, tmp_fds = [], set()
    real_fsync, real_link, real_os_open = os.fsync, os.link, os.open

    def open_spy(path, *a, **k):
        fd = real_os_open(path, *a, **k)
        if isinstance(path, str) and path.endswith(".tmp"):
            tmp_fds.add(fd)
            events.append("open_tmp")
        return fd
    monkeypatch.setattr(os, "open", open_spy)
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync_tmp") if fd in tmp_fds else None,
                                                 real_fsync(fd))[1])
    monkeypatch.setattr(os, "link", lambda *a, **k: (events.append("link"), real_link(*a, **k))[1])
    real_fsync_dir = S._fsync_dir
    dirs = []
    monkeypatch.setattr(S, "_fsync_dir", lambda p: (dirs.append(p), real_fsync_dir(p))[1])
    outcome = _release()
    assert [n for n, _ in calls] == ["unlink"]
    (name, (path, *_)), = calls
    assert os.fsdecode(path).endswith(".tmp") and G.RELEASE_NAME in os.fsdecode(path)
    record_dir = str((releasable.root / outcome.record).parent)
    assert dirs[:3] == [record_dir, str(_evidence(releasable.root)), str(releasable.root / "memory")]
    assert events[:3] == ["open_tmp", "fsync_tmp", "link"]   # bytes durable before the name appears


def test_repeating_the_release_changes_nothing(releasable):
    _release()
    before = _inventory(releasable.root)
    _refused("not_locked", expected=())
    assert _inventory(releasable.root) == before and len(_release_files(releasable.root)) == 1


# ── behavioural invariants ─────────────────────────────────────────────────

def test_release_starts_nothing_reads_no_token_and_changes_nothing_else(releasable, monkeypatch, relay):
    prefs = releasable.root / "memory" / "prefs.json"
    prefs.write_text(json.dumps({"wire_wake_listener": False, "wire_autonomy": False,
                                 "wire_autonomy_watermark": 17, "tts_voice": "af_bella"}))
    archive_sha = hashlib.sha256(releasable.archive.read_bytes()).hexdigest()
    sockets, opened, clients, updates = [], [], [], []
    real_socket, real_open, real_os_open = socket.socket, open, os.open
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (sockets.append(a), real_socket(*a, **k))[1])
    import builtins
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(os.fsdecode(p) if not isinstance(p, int) else ""),
                                                        real_os_open(p, *a, **k))[1])
    reads = []
    for meth in ("read_text", "read_bytes"):
        real_meth = getattr(pathlib.Path, meth)
        monkeypatch.setattr(pathlib.Path, meth, (lambda r: lambda self, *a, **k: (reads.append(str(self)),
                                                                                   r(self, *a, **k))[1])(real_meth))
    real_init = wire.AivengersWireClient.__init__
    monkeypatch.setattr(wire.AivengersWireClient, "__init__",
                        lambda self, *a, **k: (clients.append(a), real_init(self, *a, **k))[1])
    monkeypatch.setattr(persistence, "update", lambda *a, **k: updates.append(a))
    monkeypatch.setattr(persistence, "save", lambda *a, **k: updates.append(a))
    with S.open_store(releasable.root) as store:
        incidents = [(v.scope, v.suspension_id) for v in store.operator_view()]
        owner_suspended = store.scope_state(S.OWNER_SCOPE).suspended
    before = _inventory(releasable.root)
    threads = threading.active_count()
    outcome = _release()
    assert sockets == [] and clients == [] and updates == [] and relay.accepted == 0
    assert not any("seats" in p or p.endswith(".token") for p in opened + reads)
    assert threading.active_count() == threads
    after = _inventory(releasable.root)
    added = set(after) - set(before)
    record_dir = os.path.dirname(outcome.record)
    assert added == {record_dir, outcome.record}            # one new dir, one record, nothing else
    assert all(after[k] == before[k] for k in before)        # prefs and everything else byte-identical
    assert hashlib.sha256(releasable.archive.read_bytes()).hexdigest() == archive_sha
    with S.open_store(releasable.root) as store:
        assert [(v.scope, v.suspension_id) for v in store.operator_view()] == incidents
        assert store.scope_state(S.OWNER_SCOPE).suspended == owner_suspended is True


def test_environment_variables_cannot_release(restored, monkeypatch):
    _recover()   # production path: no C1-S2 lineage
    for name in ("LUMINA_WIRE_WAKE", "LUMINA_WIRE_SEAT_RELEASE", "LUMINA_WIRE_RELEASE",
                 "LUMINA_RELEASE_WIRE_SEAT", "LUMINA_TESTING"):
        monkeypatch.setenv(name, "1")
    _refused("c1_recovery_unavailable")
    assert _locked(restored.root)
    tree = ast.parse((REPO / "core" / "suspension_runtime.py").read_text(encoding="utf-8"))
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in (
                "release_wire_seat", "_release_plan", "_publish_wire_release", "wire_release_blocker"):
            names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "")
                     for n in ast.walk(fn)}
            assert not names & {"environ", "getenv", "putenv"}, fn.name


# ── structure: one mint site, one status literal, no other path ──────────

def _product_py():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split() if not p.startswith("tests/") and (REPO / p).exists()]


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def test_the_release_envelope_is_minted_once_as_the_direct_argument_of_the_release_call():
    mints, refs = [], []
    for rel in _product_py():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), filename=rel)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "attr", getattr(node.func, "id", None))
                if fname == "local_suspension_control":
                    assert len(node.args) == 1 and not node.keywords, rel
                    arg = node.args[0]
                    assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                        f"{rel}: a suspension envelope must be minted from a literal"
                    if arg.value.strip() == RELEASE:
                        mints.append((rel, node, parents))
            name = (node.attr if isinstance(node, ast.Attribute)
                    else node.id if isinstance(node, ast.Name) else None)
            if name in ("release_wire_seat", "may_release_wire_seat", "_publish_wire_release",
                        "WIRE_SEAT_RELEASE_ACTION"):
                refs.append((rel, name))
    assert len(mints) == 1
    rel, call, parents = mints[0]
    assert rel == "ui/suspension_dialog.py"
    kw = parents[call]
    assert isinstance(kw, ast.keyword) and kw.arg == "ingress"
    outer = parents[kw]
    assert isinstance(outer, ast.Call) and getattr(outer.func, "attr", None) == "release_wire_seat"
    assert sorted(set(refs)) == [
        ("core/operator_ingress.py", "WIRE_SEAT_RELEASE_ACTION"),
        ("core/suspension_runtime.py", "_publish_wire_release"),
        ("core/suspension_runtime.py", "may_release_wire_seat"),
        ("ui/suspension_dialog.py", "release_wire_seat"),
    ]


def test_the_unverified_status_is_a_single_literal_with_no_parameter():
    hits = []
    for rel in _product_py():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in ("unverified_owner_acknowledged", "source_installation_status"):
                    hits.append((rel, node.value))
                assert not re.search(r"verified_(retired|stopped)|retired_verified", node.value), rel
    # key set, record builder, reader -- and the one status value
    assert sorted(hits) == [("core/wire_seat_gate.py", "source_installation_status")] * 3 + [
        ("core/wire_seat_gate.py", "unverified_owner_acknowledged")]
    tree = ast.parse((REPO / "core" / "wire_seat_gate.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "release_record_bytes")
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    assert params == {"at", "restore_id", "restore_trace_sha256", "acknowledgement_sha256",
                      "store_incarnation"}


def test_the_release_path_imports_no_wire_client_or_channel_code():
    for rel in ("core/suspension_runtime.py", "core/wire_seat_gate.py", "core/operator_ingress.py",
                "ui/suspension_dialog.py"):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for mod in mods:
                assert not mod.startswith(("tools", "comms", "socket")), (rel, mod)


def test_the_writer_and_reader_agree_on_the_record_format(releasable):
    outcome = _release()
    raw = (releasable.root / outcome.record).read_bytes()
    assert G._release_pair(raw) == (outcome.restore_id, outcome.restore_trace_sha256)
    trace_raw = releasable.trace_path.read_bytes()
    assert G._trace_identity(trace_raw)[3] == releasable.archive_sha256
    assert set(json.loads(trace_raw)) == G.TRACE_KEYS


# ── the dialog (Qt offscreen) ──────────────────────────────────────────────

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


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _shown(widget):
    return not widget.isHidden()


def test_dialog_hides_the_wire_group_on_a_native_root(qapp, dialog, tmp_path, monkeypatch):
    root = tmp_path / "native"
    (root / "memory").mkdir(parents=True, mode=0o700)
    S.initialize(root, ingress=oi.local_suspension_control("initialize")).close()
    _use(monkeypatch, SimpleNamespace(root=root))
    d = dialog()
    assert not _shown(d.wire_group)
    R.stop()


def test_dialog_before_recover_offers_nothing_clickable(qapp, dialog, restored):
    d = dialog()
    assert _shown(d.wire_group) and _shown(d.wire_release_btn)
    assert d.wire_release_btn.text() == "Allow this copy to use the Wire seat"
    assert d.wire_ack.isChecked() is False and not d.wire_release_btn.isEnabled()
    assert d.wire_blocker_label.text() == R._RELEASE_TEXT["c1_not_ready"]
    d.wire_ack.setChecked(True)
    assert not d.wire_release_btn.isEnabled()
    assert restored.restore_id[:8] in d.wire_status_label.text()


def test_dialog_production_path_explains_the_c1s2_dependency(qapp, dialog, restored):
    _recover()
    d = dialog()
    d.wire_ack.setChecked(True)
    assert not d.wire_release_btn.isEnabled()
    assert d.wire_blocker_label.text() == R._RELEASE_TEXT["c1_recovery_unavailable"]
    d._release_wire_seat()                       # even a direct call changes nothing
    assert _locked(restored.root) and _release_files(restored.root) == []


def test_dialog_release_flow_with_synthetic_c1s2(qapp, dialog, releasable):
    d = dialog()
    assert d.wire_body_label.text() == G.RELEASE_BODY_TEXT
    assert d.wire_ack_label.text() == G.RELEASE_ACK_TEXT and d.wire_ack.text() == G.RELEASE_ACK_CAPTION
    assert not d.wire_ack.isChecked() and not d.wire_release_btn.isEnabled()
    d.wire_ack.setChecked(True)
    assert d.wire_release_btn.isEnabled()
    d.refresh()
    assert not d.wire_ack.isChecked() and not d.wire_release_btn.isEnabled()   # reset on refresh
    d._release_wire_seat()                                                     # unticked: no-op
    assert _locked(releasable.root)
    d.wire_ack.setChecked(True)
    d.wire_release_btn.click()
    assert G.seat_verdict(releasable.root).reason == G.RELEASED
    assert d.result_label.text() == G.RELEASE_DONE_TEXT
    assert d.wire_status_label.text() == G.RELEASED_STATUS_TEXT
    assert not _shown(d.wire_release_btn) and not d.wire_ack.isChecked()


def test_dialog_checks_readiness_for_exactly_the_restore_it_shows(qapp, dialog, releasable, monkeypatch):
    """WB2-06/S04."""
    seen = []
    real = R.wire_release_blocker
    monkeypatch.setattr(R, "wire_release_blocker", lambda expected=None: (seen.append(expected),
                                                                          real(expected))[1])
    dialog()
    assert seen and all(e == _expected() and type(e) is tuple for e in seen)


def test_dialog_binds_the_hash_to_the_words_actually_shown(qapp, dialog, releasable):
    d = dialog()
    d.wire_body_label.setText(G.RELEASE_BODY_TEXT.replace("may still be running", "is retired"))
    d.wire_ack.setChecked(True)
    d.wire_release_btn.click()
    assert _locked(releasable.root) and _release_files(releasable.root) == []
    assert R._RELEASE_TEXT["not_acknowledged"] in d.result_label.text()


def test_dialog_shows_other_locks_read_only(qapp, dialog, releasable):
    releasable.trace_path.chmod(0o644)
    d = dialog()
    assert _shown(d.wire_group) and not _shown(d.wire_release_btn) and not _shown(d.wire_ack_group)
    assert d.wire_status_label.text() == G.describe(G.SeatVerdict(False, G.TRACE_INVALID))


def test_main_window_clears_the_panel_lock_on_release_without_starting_anything():
    import ui.main_window as mw
    calls = []
    panel = SimpleNamespace(set_lock_reason=lambda t: calls.append(("reason", t)),
                            set_connection_state=lambda s: calls.append(("state", s)))
    fake = SimpleNamespace(safety_banner=SimpleNamespace(refresh=lambda s: None),
                           _refresh_wire_admission_note=lambda: None, _suspension_dialog=None,
                           _wire_listener=None, wire_panel=panel,
                           _start_wire_listener=lambda: calls.append("start"),
                           _start_wire_autonomy=lambda **k: calls.append("autonomy"))
    mw.LuminaWindow._on_suspension_event(fake, "wire_seat", G.SeatVerdict(True, G.RELEASED))
    assert calls == [("reason", ""), ("state", "off")]
    calls.clear()
    fake._wire_listener = object()
    mw.LuminaWindow._on_suspension_event(fake, "wire_seat", G.SeatVerdict(True, G.RELEASED))
    assert calls == []


# ── required statements (O-WB-3) ────────────────────────────────────────

def test_the_confirmation_states_the_four_required_points():
    """WB2-01: the owner scroll's four points must each be in the body the
    owner reads, and the acknowledgement must accept the uncertainty."""
    body = G.RELEASE_BODY_TEXT
    required = {
        "original may still be running": "The original Lumina installation may still be running",
        "cannot establish retirement": "Lumina cannot check whether it has been stopped or retired",
        "competing copies, same identity": "two copies could use the same Wire identity",
        "no autonomy / listener / connection": (
            "does not turn on Wire conversations or the Wire listener and does not connect to the Wire"),
    }
    for point, sentence in required.items():
        assert sentence in body, point
    assert "cannot confirm whether the original installation is still running" in G.RELEASE_ACK_TEXT
    assert G.RELEASE_BUTTON_TEXT == "Allow this copy to use the Wire seat"


def test_owner_texts_name_one_place():
    """WB2-07: the dialog is called Suspension controls everywhere."""
    texts = (list(G.STATUS_TEXT.values()) + list(G.REMEDY_TEXT.values())
             + list(R._RELEASE_TEXT.values()) + [G.RELEASE_BODY_TEXT, G.RELEASE_DONE_TEXT])
    assert not any("Safety controls" in t or "safety controls" in t for t in texts)
    assert "Suspension controls" in G.REMEDY_TEXT[G.RECOVERED_UNRELEASED]
    assert "Suspension controls" in R._RELEASE_TEXT["not_authorized"]


# ── wording lint (A1-7): no claim that the original was checked or retired ─

_BANNED = re.compile(r"\b(retired|retire|stopped|shut\s?down|decommissioned|deactivated|"
                     r"disconnected|verified|no longer running)\b", re.IGNORECASE)

#: Whole reviewed strings allowed to contain a banned word, each with its reason.
_ALLOWED = {
    G.RELEASE_BODY_TEXT: "negated: Lumina cannot check whether it has been stopped or retired",
    "An allowed copy is your decision, not proof that the original is retired.":
        "residual (e) wording required by A1-8: explicitly not proof",
}


def _lint(texts):
    bad = [t for t in texts if _BANNED.search(t) and t not in _ALLOWED]
    assert bad == [], bad


def test_wording_lint_module_texts_and_record_values(releasable):
    texts = (list(G.STATUS_TEXT.values()) + list(G.REMEDY_TEXT.values())
             + [G.RELEASE_BUTTON_TEXT, G.RELEASE_BODY_TEXT, G.RELEASE_ACK_TEXT, G.RELEASE_ACK_CAPTION,
                G.RELEASE_DONE_TEXT, G.RELEASED_STATUS_TEXT] + list(R._RELEASE_TEXT.values()))
    _lint(texts)
    rec = json.loads((releasable.root / _release().record).read_text())
    _lint([v for v in rec.values() if isinstance(v, str)])


def test_wording_lint_rendered_dialog(qapp, dialog, releasable):
    d = dialog()
    shown = [d.wire_status_label.text(), d.wire_body_label.text(), d.wire_ack_label.text(),
             d.wire_ack.text(), d.wire_release_btn.text(), d.wire_blocker_label.text()]
    d.wire_ack.setChecked(True)
    d.wire_release_btn.click()
    shown += [d.result_label.text(), d.wire_status_label.text()]
    _lint([t for t in shown if t])


def test_wording_lint_security_doc_section():
    doc = (REPO / "docs" / "security.md").read_text(encoding="utf-8")
    start = doc.index("## Restored copies and the Wire seat")
    section = doc[start: doc.index("\n## ", start + 1)]
    flat = re.sub(r"\s+", " ", section.replace("*", ""))
    sentences = [s.strip() for s in re.split(r"(?<=[.:])\s+(?=[A-Z-])", flat) if s.strip()]
    hits = [s for s in sentences if _BANNED.search(s)]
    _lint(hits)
    assert "prevent" not in flat.lower()
    for needle in ("Deleting the restore records by hand removes the lock",
                   "permission travels with the folder", "Not available yet",
                   "unverified, owner acknowledged", "Allow this copy to use the Wire seat"):
        assert needle in flat, needle


# ── real entry points: typed chat and relayed Telegram can't release ───────

class _Worker:
    created = []

    def __init__(self, agent, user_input, signals, chat_id=None, attachments=None,
                 approval_event_id=None, suspension_token=None):
        self.user_input = user_input
        _Worker.created.append(self)

    def start(self):
        pass

    def isRunning(self):
        return False

    def cancel_requested(self):
        return False


def test_typed_chat_and_relayed_telegram_cannot_release_even_when_the_button_could(
        qapp, releasable, tmp_path, monkeypatch):
    """Drives the real LuminaWindow on a root the button WOULD release
    (synthetic C1-S2): the owner typing release words or /suspension, and the
    same text relayed from Telegram, release nothing. /suspension only opens
    the dialog, whose box is unticked."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import ui.main_window as mw
    from comms import telegram_origin_routing as routing
    from core import emergency_stop
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "entry" / "lumina.db"))
    (tmp_path / "entry").mkdir()
    monkeypatch.setattr(config, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    monkeypatch.setattr(mw, "AgentWorker", _Worker)
    _Worker.created = []
    emergency_stop._reset_for_tests()
    from core.agent import LuminaAgent
    from tools.memory import create_chat, init_chat_db
    init_chat_db()
    chat_id = create_chat("wb2-entry")
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="wb2-entry"))
    try:
        win._load_chat(chat_id, persist_as_last=False)
        texts = ("Allow this copy to use the Wire seat", "/suspension", "/wire_seat_release",
                 "/suspension wire_seat_release", "I accept this uncertainty.")
        for text in texts:
            win.chat_widget.input.setPlainText(text)
            QTest.keyClick(win.chat_widget.input, Qt.Key_Return)
            QApplication.processEvents()
            win.worker = None
        # After Recover the owner scope is suspended (C1), so Telegram intake
        # could not capture a token here; the relay carries none.
        token = None
        for text in texts:
            route = routing.OriginRoute("42", 7001, chat_id, win._telegram_route_token, 1.0, 1e12)
            request = routing.OriginDispatch(route=route, text=text, approval_event_id="telegram:42:9",
                                             suspension_token=token)
            win._on_telegram_origin_dispatch(request)
            QApplication.processEvents()
            win.worker = None
        assert _locked(releasable.root) and _release_files(releasable.root) == []
        dialog = win._suspension_dialog
        assert dialog is not None   # typed /suspension did reach the dispatcher and open it
        assert dialog.wire_ack.isChecked() is False and not dialog.wire_release_btn.isEnabled()
        assert _shown(dialog.wire_release_btn)   # the button exists; only a click can use it
        dialog.close()
    finally:
        win.worker = None
        win.close()
        emergency_stop._reset_for_tests()
