"""W-B1: Wire seat quarantine for restored data roots.

Design: reports/AGENT-BACKUP-RESTORE-PHASE-B-R1/wb/ WB-PROPOSAL.md, amendment
A1 and erratum A1-E1. Everything runs on synthetic data roots in tmp_path,
an in-test fake relay on a private Unix socket, and synthetic tokens. No real
data root, no real relay, no real seat.

Invariants pinned here:
  I-1  a traced root is refused unless every valid trace has a VALID matching
       release; an invalid release never authorizes;
  I-2  an invalid release (anywhere) never blocks a later valid release;
  I-4  unreadable / over-cap / invalid trace / unknown root refuse, with a
       stable reason and a remedy;
  and the adapter refuses before any token read or socket.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

import config
from core import operator_ingress as oi
from core import persistence
from core import suspension as S
from core import suspension_backup as c1
from core import wire_seat_gate as G
from tools import aivengers_wire as wire

# Real restore-engine fixtures (a synthetic source install, a v2 archive, an
# empty destination) — reused, not copied.
from test_agent_backup_restore_02 import source, v2, target, restore  # noqa: F401

REPO = pathlib.Path(__file__).resolve().parent.parent
RID_A, RID_B = "a" * 32, "b" * 32
ACK = "c" * 64
INCARNATION = "d" * 32


# ── helpers ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _umask():
    old = os.umask(0o077)  # what C1's writers expect
    yield
    os.umask(old)


def _root(tmp_path, name="data"):
    root = tmp_path / name
    (root / "memory").mkdir(parents=True)
    return root


def _bind(monkeypatch, root):
    """Make `root` this process's data root, as a real launch does."""
    monkeypatch.setenv("LUMINA_DATA_DIR", str(root))
    monkeypatch.setattr(config, "DATA_DIR", str(root))


def _evidence(root):
    return pathlib.Path(root) / "memory" / "suspension_evidence"


def _real_trace(root, rid=RID_A):
    """The real C1 writer (requires a C1-empty root)."""
    rel = c1.write_restore_trace(root, restore_id=rid, archive_sha256="e" * 64,
                                 restore_class="incomplete_state_export")
    path = pathlib.Path(root) / rel
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def _trace_bytes(rid=RID_A, **over):
    d = {"schema": "lumina.c1_restore_trace", "version": 1, "at": 1700000000.5,
         "restore_id": rid, "archive_sha256": "e" * 64,
         "restore_class": "incomplete_state_export", "archived_c1_state": "not_restored",
         "required_next_step": "owner_local_recover"}
    d.update(over)
    return _canon({k: v for k, v in d.items() if v is not _DROP})


_DROP = object()


def _release_bytes(rid, trace_sha, **over):
    d = {"schema": G.RELEASE_SCHEMA, "version": 1, "at": 1700000100.25,
         "restore_id": rid, "restore_trace_sha256": trace_sha,
         "source_installation_status": G.SOURCE_STATUS_UNVERIFIED,
         "acknowledgement_sha256": ACK, "store_incarnation": INCARNATION,
         "ingress": G.RELEASE_INGRESS}
    d.update(over)
    return _canon({k: v for k, v in d.items() if v is not _DROP})


_seq = [0]


def _put(root, name, raw, *, dirname=None, mode=0o600):
    """Write one record into a (new, unless named) private evidence child."""
    ev = _evidence(root)
    ev.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dirname is None:
        _seq[0] += 1
        dirname = f"20261119T000000Z-{_seq[0]:08x}"
    child = ev / dirname
    child.mkdir(exist_ok=True, mode=0o700)
    path = child / name
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _verdict(root):
    return G.seat_verdict(root)


# ── native roots ───────────────────────────────────────────────────────────

def test_root_without_memory_is_native(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    v = _verdict(root)
    assert (v.allowed, v.reason) == (True, G.NO_TRACE)


def test_memory_without_evidence_is_native_even_if_group_writable(tmp_path):
    root = _root(tmp_path)
    (root / "memory").chmod(0o775)  # a default umask-002 folder: C1's business, not the seat's
    v = _verdict(root)
    assert (v.allowed, v.reason) == (True, G.NO_TRACE)


def test_ordinary_c1_recovery_evidence_is_native(tmp_path):
    """A native root that went through C1 initialize + destructive recovery
    (evidence directories, no restore trace) keeps the Wire (lab E)."""
    root = _root(tmp_path)
    (root / "memory").chmod(0o700)
    S.initialize(root, ingress=oi.local_suspension_control("initialize")).close()
    (root / "memory" / S.IDENTITY_NAME).unlink()
    S.recover_destructively(root, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unreadable_runtimes=True)
    assert any(_evidence(root).iterdir())
    v = _verdict(root)
    assert (v.allowed, v.reason) == (True, G.NO_TRACE)


def test_stray_files_and_tmp_names_are_never_opened(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    child = next(_evidence(root).iterdir())
    (child / "wire_release.json.0123456789ab.tmp").write_bytes(b"torn")
    (child / "notes.bak").write_bytes(b"x")
    (_evidence(root) / "stray.txt").write_bytes(b"x")
    opened = []
    real_open = os.open

    def spy(path, flags, *a, **k):
        opened.append(os.fsdecode(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(G.os, "open", spy)
    v = _verdict(root)
    assert v.reason == G.RECOVERED_UNRELEASED
    files = [p for p in opened if p.endswith((".json", ".tmp", ".bak", ".txt"))]
    assert set(files) == {G.TRACE_NAME}  # the release name is absent here; nothing else opened


# ── restored roots ─────────────────────────────────────────────────────────

def test_real_trace_without_release_is_quarantined(tmp_path):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    v = _verdict(root)
    assert (v.allowed, v.reason) == (False, G.RECOVERED_UNRELEASED)
    assert [(r, s) for r, s, _ in v.unreleased] == [(RID_A, sha)]


def test_valid_matching_release_allows(tmp_path):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha))
    v = _verdict(root)
    assert (v.allowed, v.reason, v.ignored_releases) == (True, G.RELEASED, 0)


@pytest.mark.parametrize("rid, sha_ok", [(RID_B, True), (RID_A, False)])
def test_release_for_another_restore_or_another_trace_does_not_release(tmp_path, rid, sha_ok):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    _put(root, G.RELEASE_NAME, _release_bytes(rid, sha if sha_ok else "f" * 64))
    assert _verdict(root).reason == G.RECOVERED_UNRELEASED


def test_every_trace_needs_its_own_release(tmp_path):
    root = _root(tmp_path)
    _, sha_a = _real_trace(root)
    raw_b = _trace_bytes(RID_B)
    _put(root, G.TRACE_NAME, raw_b)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha_a))
    v = _verdict(root)
    assert v.reason == G.RECOVERED_UNRELEASED
    assert [r for r, _, _ in v.unreleased] == [RID_B]
    _put(root, G.RELEASE_NAME, _release_bytes(RID_B, hashlib.sha256(raw_b).hexdigest()))
    assert _verdict(root).reason == G.RELEASED


def test_orphan_release_without_any_trace_is_native(tmp_path):
    root = _root(tmp_path)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, "f" * 64))
    v = _verdict(root)
    assert (v.allowed, v.reason) == (True, G.NO_TRACE)


# ── I-1 / I-2: invalid releases never authorize and never block ────────────

def _invalid_releases(rid, sha):
    good = _release_bytes(rid, sha)
    cases = {f"truncated_{n}": (good[:n], 0o600) for n in range(0, len(good))}
    cases.update({
        "status_verified_retired": (_release_bytes(rid, sha, source_installation_status="verified_retired"), 0o600),
        "extra_key": (_canon({**json.loads(good), "extra": 1}), 0o600),
        "missing_key": (_release_bytes(rid, sha, acknowledgement_sha256=_DROP), 0o600),
        "noncanonical": (json.dumps(json.loads(good), indent=1).encode(), 0o600),
        "wrong_schema": (_release_bytes(rid, sha, schema="lumina.other"), 0o600),
        "wrong_version": (_release_bytes(rid, sha, version=2), 0o600),
        "bool_version": (_release_bytes(rid, sha, version=True), 0o600),
        "wrong_ingress": (_release_bytes(rid, sha, ingress="local_text_submission"), 0o600),
        "uppercase_hex": (_release_bytes(rid, sha.upper()), 0o600),
        "bad_incarnation": (_release_bytes(rid, sha, store_incarnation="x"), 0o600),
        "negative_time": (_release_bytes(rid, sha, at=-1), 0o600),
        "oversize": (good[:-1] + b" " * 5000 + b"}", 0o600),
        "group_readable": (good, 0o640),
        "world_readable": (good, 0o604),
    })
    return cases


def test_no_invalid_release_authorizes_and_none_blocks_a_valid_retry(tmp_path):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    cases = _invalid_releases(RID_A, sha)
    assert len(cases) > 100  # every truncation length of a real record
    for i, (name, (raw, mode)) in enumerate(cases.items()):
        _put(root, G.RELEASE_NAME, raw, mode=mode)
        v = _verdict(root)
        assert (v.allowed, v.reason) == (False, G.RECOVERED_UNRELEASED), name
        assert v.ignored_releases == i + 1, name
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha))
    v = _verdict(root)
    assert (v.allowed, v.reason, v.ignored_releases) == (True, G.RELEASED, len(cases))


def test_release_shapes_that_are_not_records_are_ignored(tmp_path):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    good = _release_bytes(RID_A, sha)
    # a symlink named like a release, pointing at a valid record elsewhere
    target_file = tmp_path / "elsewhere.json"
    target_file.write_bytes(good)
    target_file.chmod(0o600)
    ev = _evidence(root)
    link_dir = ev / "20261119T000000Z-link0000"
    link_dir.mkdir(mode=0o700)
    (link_dir / G.RELEASE_NAME).symlink_to(target_file)
    # a hard-linked release (nlink == 2): the crash window between link and unlink
    hard = _put(root, G.RELEASE_NAME, good)
    os.link(hard, hard.parent / "wire_release.json.000000000000.tmp")
    # a directory named like a release
    d = ev / "20261119T000000Z-dir00000"
    d.mkdir(mode=0o700)
    (d / G.RELEASE_NAME).mkdir()
    v = _verdict(root)
    assert (v.allowed, v.reason, v.ignored_releases) == (False, G.RECOVERED_UNRELEASED, 3)
    os.unlink(hard.parent / "wire_release.json.000000000000.tmp")  # the writer's step 4
    assert _verdict(root).reason == G.RELEASED


def test_release_beside_the_trace_counts_by_content_only(tmp_path):
    """A1-E1 E1-1: location is irrelevant. Junk beside the trace never blocks;
    a valid copy beside the trace is as good as one in its own directory."""
    root = _root(tmp_path)
    trace, sha = _real_trace(root)
    junk = trace.parent / G.RELEASE_NAME
    junk.write_bytes(b"{")
    junk.chmod(0o600)
    v = _verdict(root)
    assert (v.reason, v.ignored_releases) == (G.RECOVERED_UNRELEASED, 1)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha))
    assert _verdict(root).reason == G.RELEASED
    junk.unlink()
    good_beside = trace.parent / G.RELEASE_NAME
    good_beside.write_bytes(_release_bytes(RID_A, sha))
    good_beside.chmod(0o600)
    assert _verdict(root).reason == G.RELEASED


def test_a_released_root_with_an_invalid_copy_beside_the_trace_stays_released(tmp_path):
    root = _root(tmp_path)
    trace, sha = _real_trace(root)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha))
    bad = trace.parent / G.RELEASE_NAME
    bad.write_bytes(_release_bytes(RID_A, sha)[:-3])
    bad.chmod(0o600)
    assert _verdict(root).reason == G.RELEASED


# ── I-4: refusals, reasons, precedence ─────────────────────────────────────

def _trace_variants():
    good = _trace_bytes()
    return {
        "missing_field": (_trace_bytes(restore_class=_DROP), 0o600),
        "extra_key": (_canon({**json.loads(good), "x": 1}), 0o600),
        "noncanonical": (json.dumps(json.loads(good), indent=2).encode(), 0o600),
        "wrong_schema": (_trace_bytes(schema="lumina.other"), 0o600),
        "wrong_version": (_trace_bytes(version=2), 0o600),
        "bad_restore_id": (_trace_bytes(rid="A" * 32), 0o600),
        "bad_archive_sha": (_trace_bytes(archive_sha256="g" * 64), 0o600),
        "bad_state": (_trace_bytes(archived_c1_state="restored"), 0o600),
        "truncated": (good[: len(good) // 2], 0o600),
        "empty": (b"", 0o600),
        "oversize": (good[:-1] + b" " * 5000 + b"}", 0o600),
        "group_readable": (good, 0o640),
        "unreadable_mode_000": (good, 0o000),
    }


@pytest.mark.parametrize("case", sorted(_trace_variants()))
def test_an_invalid_trace_refuses(tmp_path, case):
    root = _root(tmp_path)
    raw, mode = _trace_variants()[case]
    _put(root, G.TRACE_NAME, raw, mode=mode)
    v = _verdict(root)
    assert (v.allowed, v.reason) == (False, G.TRACE_INVALID)


def test_a_symlinked_or_hard_linked_trace_refuses(tmp_path):
    root = _root(tmp_path)
    real = tmp_path / "t.json"
    real.write_bytes(_trace_bytes())
    real.chmod(0o600)
    child = _evidence(root) / "20261119T000000Z-sym00000"
    child.mkdir(parents=True, mode=0o700)
    (child / G.TRACE_NAME).symlink_to(real)
    assert _verdict(root).reason == G.TRACE_INVALID
    root2 = _root(tmp_path, "data2")
    trace, _ = _real_trace(root2)
    os.link(trace, tmp_path / "extra-link")
    assert _verdict(root2).reason == G.TRACE_INVALID


def test_record_reads_are_bounded_even_for_huge_files(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha) + b" " * (1 << 20))
    sizes = []
    real_read = os.read

    def spy(fd, n):
        sizes.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(G.os, "read", spy)
    v = _verdict(root)
    assert (v.reason, v.ignored_releases) == (G.RECOVERED_UNRELEASED, 1)
    assert sizes and max(sizes) <= G.MAX_RECORD_BYTES + 1


def test_a_valid_release_never_rescues_an_invalid_trace(tmp_path):
    root = _root(tmp_path)
    raw = _trace_bytes()
    _put(root, G.TRACE_NAME, raw, mode=0o640)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, hashlib.sha256(raw).hexdigest()))
    assert _verdict(root).reason == G.TRACE_INVALID


def _shape(root, tmp_path, shape):
    mem, ev = root / "memory", _evidence(root)
    if shape == "memory_symlink":
        mem.rmdir()
        other = tmp_path / "other-memory"
        other.mkdir()
        mem.symlink_to(other)
    elif shape == "memory_file":
        mem.rmdir()
        mem.write_bytes(b"x")
    elif shape == "evidence_symlink":
        other = tmp_path / "other-evidence"
        other.mkdir(mode=0o700)
        ev.symlink_to(other)
    elif shape == "evidence_file":
        ev.write_bytes(b"x")
    elif shape == "evidence_mode_000":
        ev.mkdir(mode=0o700)
        ev.chmod(0o000)
    elif shape == "evidence_group_writable":
        ev.mkdir(mode=0o700)
        ev.chmod(0o770)
    elif shape == "child_symlink":
        ev.mkdir(mode=0o700)
        other = tmp_path / "other-child"
        other.mkdir(mode=0o700)
        (ev / "20261119T000000Z-link0000").symlink_to(other)
    elif shape == "child_mode_000":
        ev.mkdir(mode=0o700)
        (ev / "20261119T000000Z-0000000a").mkdir(mode=0o700)
        (ev / "20261119T000000Z-0000000a").chmod(0o000)
    elif shape == "child_group_writable":
        ev.mkdir(mode=0o700)
        (ev / "20261119T000000Z-0000000b").mkdir(mode=0o700)
        (ev / "20261119T000000Z-0000000b").chmod(0o770)


EVIDENCE_SHAPES = ["memory_symlink", "memory_file", "evidence_symlink", "evidence_file",
                   "evidence_mode_000", "evidence_group_writable", "child_symlink",
                   "child_mode_000", "child_group_writable"]


@pytest.mark.parametrize("shape", EVIDENCE_SHAPES)
def test_unreadable_evidence_refuses_never_reads_as_no_trace(tmp_path, shape):
    root = _root(tmp_path)
    _shape(root, tmp_path, shape)
    try:
        v = _verdict(root)
        assert (v.allowed, v.reason) == (False, G.EVIDENCE_UNREADABLE), shape
    finally:
        for p in (_evidence(root), *(_evidence(root).glob("*") if _evidence(root).is_dir()
                                     and os.access(_evidence(root), os.X_OK) else ())):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o700)


def test_over_cap_refuses(tmp_path):
    root = _root(tmp_path)
    ev = _evidence(root)
    ev.mkdir(mode=0o700)
    for i in range(G.MAX_EVIDENCE_CHILDREN):
        (ev / f"d{i:05d}").mkdir()
    assert _verdict(root).allowed is True
    (ev / "one-more").mkdir()
    v = _verdict(root)
    assert (v.allowed, v.reason) == (False, G.OVER_CAP)


def test_refusal_precedence(tmp_path, monkeypatch):
    # TRACE_INVALID outranks RECOVERED_UNRELEASED
    root = _root(tmp_path, "p1")
    _real_trace(root)
    _put(root, G.TRACE_NAME, _trace_bytes(RID_B), mode=0o640)
    assert _verdict(root).reason == G.TRACE_INVALID
    # EVIDENCE_UNREADABLE (a linked child, sorted last) outranks TRACE_INVALID
    other = tmp_path / "o"
    other.mkdir(mode=0o700)
    (_evidence(root) / "zzzz-link").symlink_to(other)
    assert _verdict(root).reason == G.EVIDENCE_UNREADABLE
    # OVER_CAP outranks TRACE_INVALID (children are not inspected once over cap)
    root2 = _root(tmp_path, "p2")
    _put(root2, G.TRACE_NAME, b"{", mode=0o600)
    monkeypatch.setattr(G, "MAX_EVIDENCE_CHILDREN", 0)
    assert _verdict(root2).reason == G.OVER_CAP
    monkeypatch.undo()
    # ROOT_* outranks everything
    monkeypatch.delenv("LUMINA_DATA_DIR", raising=False)
    assert G.process_seat_verdict().reason == G.ROOT_UNKNOWN


def test_an_unexpected_failure_refuses_with_the_class_name_only(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _real_trace(root)

    def boom(*a, **k):
        raise RuntimeError("secret path /home/x")

    monkeypatch.setattr(G.os, "listdir", boom)
    v = _verdict(root)
    assert (v.allowed, v.reason, v.error_class) == (False, G.GATE_ERROR, "RuntimeError")
    assert "secret" not in G.describe(v)


def test_every_reader_outcome_has_a_table_code_text_and_remedy():
    # C1-S2 A2-7 (disclosed edit): TRACE_MISSING joins the table, and the
    # do-not-delete sentence now says deleting never unlocks (A2-7, TA2-09).
    refusals = {G.RECOVERED_UNRELEASED, G.TRACE_INVALID, G.TRACE_MISSING, G.EVIDENCE_UNREADABLE,
                G.OVER_CAP, G.ROOT_UNKNOWN, G.ROOT_MISMATCH, G.GATE_ERROR}
    assert set(G.STATUS_TEXT) == refusals == set(G.REMEDY_TEXT)
    assert set(G._PRECEDENCE) | {G.RECOVERED_UNRELEASED, G.GATE_ERROR} == refusals
    dont = ("Do not delete anything under memory/suspension_evidence or restore_history: "
            "deleting restore records never unlocks the Wire and can lock it for good.")
    for code in (G.TRACE_INVALID, G.TRACE_MISSING, G.EVIDENCE_UNREADABLE, G.OVER_CAP):
        assert dont in G.REMEDY_TEXT[code], code
    for code in refusals:
        text = G.describe(G.SeatVerdict(False, code)).lower()
        assert text.startswith("wire locked:"), code
        if code != G.RECOVERED_UNRELEASED:
            assert "release" not in text and "allow" not in text, code
    # C1-S2 TA2-02 (disclosed edit): state-neutral; the gate can't see the
    # C1 store, so it names no control and points to Suspension controls.
    assert G.REMEDY_TEXT[G.RECOVERED_UNRELEASED] == (
        "Open Suspension controls (/suspension) to see what this copy needs before it can use "
        "the Wire.")
    assert "hard link" in G.REMEDY_TEXT[G.TRACE_INVALID]
    assert G.describe(G.SeatVerdict(True, G.NO_TRACE)) == ""


def test_no_text_claims_the_original_installation_was_retired():
    banned = ("retired", "shut down", "shutdown", "stopped", "decommissioned",
              "no longer running", "verified")
    for text in list(G.STATUS_TEXT.values()) + list(G.REMEDY_TEXT.values()):
        low = text.lower()
        for word in banned:
            assert f" {word}" not in f" {low}", (word, text)


# ── process root resolution ────────────────────────────────────────────────

def test_process_root_must_be_set_absolute_and_agree_with_config(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _bind(monkeypatch, root)
    assert G.process_seat_verdict().reason == G.NO_TRACE
    monkeypatch.setenv("LUMINA_DATA_DIR", "relative/data")
    assert G.process_seat_verdict().reason == G.ROOT_UNKNOWN
    monkeypatch.setenv("LUMINA_DATA_DIR", "")
    assert G.process_seat_verdict().reason == G.ROOT_UNKNOWN
    monkeypatch.setenv("LUMINA_DATA_DIR", str(tmp_path / "elsewhere"))
    assert G.process_seat_verdict().reason == G.ROOT_MISMATCH
    monkeypatch.setattr(config, "DATA_DIR", None)
    monkeypatch.setenv("LUMINA_DATA_DIR", str(root))
    assert G.process_seat_verdict().reason == G.ROOT_MISMATCH


def test_verdict_is_the_same_from_a_fresh_process(tmp_path):
    root = _root(tmp_path)
    _, sha = _real_trace(root)
    code = ("import sys; sys.path.insert(0, %r); from core import wire_seat_gate as G; "
            "v = G.seat_verdict(%r); print(v.allowed, v.reason)" % (str(REPO), str(root)))
    run = lambda: subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                                 text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")})
    assert run().stdout.split() == ["False", G.RECOVERED_UNRELEASED]
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha))
    assert run().stdout.split() == ["True", G.RELEASED]


# ── format pins and import law ─────────────────────────────────────────────

def test_reader_encoding_and_trace_keys_match_the_c1_writer(tmp_path):
    root = _root(tmp_path)
    trace, _ = _real_trace(root)
    raw = trace.read_bytes()
    data = json.loads(raw)
    assert set(data) == G.TRACE_KEYS
    assert G._canonical(data) == S._canonical(data) == raw
    assert data["schema"] == G.TRACE_SCHEMA == c1.RESTORE_TRACE_SCHEMA
    assert G.TRACE_NAME == c1.RESTORE_TRACE_NAME
    assert (G.MEMORY_DIRNAME, G.EVIDENCE_DIRNAME) == (S.MEMORY_DIRNAME, S.EVIDENCE_DIRNAME)
    assert G._trace_identity(raw)[:2] == (RID_A, hashlib.sha256(raw).hexdigest())


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_the_gate_imports_only_the_standard_library():
    allowed = {"__future__", "hashlib", "json", "math", "os", "re", "stat", "sys", "dataclasses"}
    assert _imports(REPO / "core" / "wire_seat_gate.py") <= allowed


def test_the_adapter_imports_the_gate_and_never_c1():
    names = _imports(REPO / "tools" / "aivengers_wire.py")
    assert "core" in names  # from core import wire_seat_gate
    text = (REPO / "tools" / "aivengers_wire.py").read_text(encoding="utf-8")
    assert "from core import wire_seat_gate" in text
    assert "suspension" not in text


# ── adapter enforcement ────────────────────────────────────────────────────

def _first_statement_is_gate(func) -> bool:
    body = [n for n in func.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    first = body[0]
    return (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and ast.unparse(first.value) == "self._gate()")


def test_every_choke_point_starts_with_the_gate():
    tree = ast.parse((REPO / "tools" / "aivengers_wire.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AivengersWireClient")
    funcs = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    for name in ("_connect", "_session", "_load_token", "watch"):
        assert _first_statement_is_gate(funcs[name]), name
    # every socket the adapter creates is inside _connect or watch
    creators = {f for f, node in funcs.items()
                if "socket.socket(" in ast.unparse(node)}
    assert creators == {"_connect", "watch"}
    readers = {f for f, node in funcs.items() if "token_path" in ast.unparse(node)
               and f != "token_path"}
    assert readers == {"_load_token"}


def test_no_product_module_reaches_the_relay_outside_the_adapter():
    offenders = []
    for base in ("core", "comms", "ui", "tools"):
        for path in (REPO / base).rglob("*.py"):
            if path.name == "aivengers_wire.py":
                continue
            text = path.read_text(errors="replace")
            if "wire.sock" in text or "lumina.token" in text or '"seats"' in text:
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


def test_no_constructor_switch_exists():
    import inspect
    params = list(inspect.signature(wire.AivengersWireClient.__init__).parameters)
    assert params == ["self", "state_dir", "timeout"]


def test_quarantine_error_is_an_unavailable_error_with_a_reason():
    exc = wire.WireSeatQuarantinedError(G.RECOVERED_UNRELEASED)
    assert isinstance(exc, wire.WireUnavailableError) and isinstance(exc, wire.WireError)
    assert exc.reason == G.RECOVERED_UNRELEASED and "lumina" in str(exc)


class _FakeRelay:
    """A tiny Unix-socket peer that answers every frame with {"ok": true}."""

    def __init__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="wb1-"))
        (self.dir / "seats").mkdir()
        (self.dir / "seats" / "lumina.token").write_text("tok-synthetic\n")
        self.path = self.dir / "wire.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(8)
        self.accepted = 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.accepted += 1
            with conn:
                buf = b""
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        _, buf = buf.split(b"\n", 1)
                        conn.sendall(b'{"ok":true,"channels":[]}\n')

    def close(self):
        self._stop = True
        self.server.close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def relay():
    r = _FakeRelay()
    yield r
    r.close()


@pytest.fixture
def spies(monkeypatch):
    made, reads = [], []
    real_socket, real_read = socket.socket, pathlib.Path.read_text

    def socket_spy(*a, **k):
        if "fileno" not in k:  # accept() wraps an existing fd; only count new sockets
            made.append(a)
        return real_socket(*a, **k)

    def read_spy(self, *a, **k):
        reads.append(str(self))
        return real_read(self, *a, **k)

    monkeypatch.setattr(wire.socket, "socket", socket_spy)
    monkeypatch.setattr(pathlib.Path, "read_text", read_spy)
    return SimpleNamespace(sockets=made, reads=reads)


def test_a_native_root_connects(tmp_path, monkeypatch, relay, spies):
    _bind(monkeypatch, _root(tmp_path))
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    assert client.ping() is True
    assert len(spies.sockets) == 1 and relay.accepted == 1
    assert str(relay.dir / "seats" / "lumina.token") in spies.reads


def _all_operations(client):
    return {
        "ping": lambda: client.ping(),
        "post": lambda: client.post("hello"),
        "read_unseen": lambda: client.read_unseen(),
        "channels": lambda: client.channels(),
        "activation_watermark": lambda: client.activation_watermark(),
        "watch": lambda: next(client.watch()),
        "_connect": lambda: client._connect(),
        "_load_token": lambda: client._load_token(),
        "_session": lambda: client._session().__enter__(),
    }


def _assert_refused_without_any_socket(client, relay, spies, reason):
    for name, op in _all_operations(client).items():
        with pytest.raises(wire.WireSeatQuarantinedError) as info:
            op()
        assert info.value.reason == reason, name
    assert spies.sockets == []
    assert relay.accepted == 0
    assert not any(r.endswith(".token") for r in spies.reads)


def test_a_restored_root_refuses_every_operation_before_any_socket_or_token(tmp_path, monkeypatch,
                                                                          relay, spies):
    root = _root(tmp_path)
    _real_trace(root)
    _bind(monkeypatch, root)
    monkeypatch.setenv("LUMINA_WIRE_WAKE", "1")  # the canary changes nothing here
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    _assert_refused_without_any_socket(client, relay, spies, G.RECOVERED_UNRELEASED)


def test_a_root_restored_by_the_real_engine_is_quarantined(v2, target, monkeypatch, relay, spies):
    data_dir, base = target
    plan, receipt = restore(v2, data_dir, base)
    assert receipt["state"] == "published_execution_fenced"
    assert c1.restore_trace_state(data_dir) == "absent_with_trace"
    _bind(monkeypatch, data_dir)
    # the owner's prefs saying ON change nothing (prefs never travel; set here anyway)
    (pathlib.Path(data_dir) / "memory" / "prefs.json").write_text(
        json.dumps({"wire_wake_listener": True, "wire_autonomy": True}))
    v = G.process_seat_verdict()
    assert (v.allowed, v.reason) == (False, G.RECOVERED_UNRELEASED)
    assert [r for r, _, _ in v.unreleased] == [receipt["restore_id"]]
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    _assert_refused_without_any_socket(client, relay, spies, G.RECOVERED_UNRELEASED)


def test_a_release_survives_a_later_destructive_recovery(v2, target, monkeypatch):
    """Restore -> owner Recover -> release -> pin loss -> second recovery:
    the trace is preserved byte-for-byte, so the release still pairs (lab C)."""
    data_dir, base = target
    restore(v2, data_dir, base)
    S.recover_destructively(data_dir, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unreadable_runtimes=True)
    assert S.probe(data_dir).status is S.StoreStatus.HEALTHY
    v = G.seat_verdict(data_dir)
    assert v.reason == G.RECOVERED_UNRELEASED
    rid, sha, _ = v.unreleased[0]
    _put(pathlib.Path(data_dir), G.RELEASE_NAME, _release_bytes(rid, sha))
    assert G.seat_verdict(data_dir).reason == G.RELEASED
    (pathlib.Path(data_dir) / "memory" / S.IDENTITY_NAME).unlink()
    S.recover_destructively(data_dir, ingress=oi.local_suspension_control("recover"),
                            acknowledge_unreadable_runtimes=True)
    assert G.seat_verdict(data_dir).reason == G.RELEASED


def test_unknown_or_mismatched_root_refuses_in_the_adapter(tmp_path, monkeypatch, relay, spies):
    root = _root(tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", str(root))
    monkeypatch.delenv("LUMINA_DATA_DIR", raising=False)
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    _assert_refused_without_any_socket(client, relay, spies, G.ROOT_UNKNOWN)
    monkeypatch.setenv("LUMINA_DATA_DIR", str(tmp_path / "other"))
    _assert_refused_without_any_socket(client, relay, spies, G.ROOT_MISMATCH)


def test_a_gate_exception_refuses_in_the_adapter(tmp_path, monkeypatch, relay, spies):
    _bind(monkeypatch, _root(tmp_path))

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(wire.wire_seat_gate, "process_seat_verdict", boom)
    client = wire.AivengersWireClient(state_dir=relay.dir, timeout=2)
    _assert_refused_without_any_socket(client, relay, spies, G.GATE_ERROR)


def test_gate_benchmark_is_cheap(tmp_path, capsys):
    root = _root(tmp_path)
    trace, sha = _real_trace(root)
    for i in range(49):
        (_evidence(root) / f"20261119T00000{i:02d}Z-feedface").mkdir(mode=0o700)
    _put(root, G.RELEASE_NAME, _release_bytes(RID_A, sha))
    runs = []
    for _ in range(200):
        t = time.perf_counter()
        assert G.seat_verdict(root).reason == G.RELEASED
        runs.append(time.perf_counter() - t)
    runs.sort()
    median_ms = runs[len(runs) // 2] * 1000
    with capsys.disabled():
        print(f"\n[W-B1 benchmark] seat_verdict median {median_ms:.3f} ms (1 trace, 51 evidence dirs)")
    assert median_ms < 50  # report, generous ceiling only


# ── Settings toggles ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab_env(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    from ui.main_window import COLORS
    from ui.settings.communications_tab import CommunicationsTab
    return lambda: CommunicationsTab(SimpleNamespace(), COLORS)


def _prefs_bytes():
    try:
        return pathlib.Path(persistence.PREFS_PATH).read_bytes()
    except FileNotFoundError:
        return None


def test_toggles_refuse_on_a_locked_root_and_persist_nothing(tmp_path, monkeypatch, tab_env):
    root = _root(tmp_path)
    _real_trace(root)
    _bind(monkeypatch, root)
    persistence.update({"unrelated": 1})
    tab = tab_env()
    emitted = []
    tab.wire_wake_toggle_changed.connect(emitted.append)
    tab.wire_autonomy_toggle_changed.connect(emitted.append)
    expected = G.describe(G.process_seat_verdict())
    assert tab.wire_wake_status.text() == expected
    assert tab.wire_autonomy_status.text() == expected
    assert not tab.wire_wake_toggle.isEnabled() and not tab.wire_autonomy_toggle.isEnabled()
    before = _prefs_bytes()
    tab._toggle_wire_wake()   # the handler itself refuses too (the button is only UI)
    tab._toggle_wire_autonomy()
    assert _prefs_bytes() == before
    assert emitted == []
    assert tab.wire_wake_status.text() == expected


def test_toggles_can_always_turn_wire_off(tmp_path, monkeypatch, tab_env):
    root = _root(tmp_path)
    _real_trace(root)
    _bind(monkeypatch, root)
    persistence.update({"wire_wake_listener": True, "wire_autonomy": True})
    tab = tab_env()
    assert tab.wire_wake_toggle.isEnabled() and tab.wire_wake_toggle.text() == "Turn off"
    emitted = []
    tab.wire_wake_toggle_changed.connect(emitted.append)
    tab.wire_autonomy_toggle_changed.connect(emitted.append)
    tab._toggle_wire_wake()
    tab._toggle_wire_autonomy()
    assert emitted == [False, False]
    assert persistence.load()["wire_wake_listener"] is False
    assert persistence.load()["wire_autonomy"] is False
    assert not tab.wire_wake_toggle.isEnabled()  # now off, and locked


def test_toggles_work_normally_on_a_native_root(tmp_path, monkeypatch, tab_env):
    _bind(monkeypatch, _root(tmp_path))
    tab = tab_env()
    assert tab.wire_wake_status.text() == "Wire wake listener: off"
    emitted = []
    tab.wire_wake_toggle_changed.connect(emitted.append)
    tab._toggle_wire_wake()
    assert emitted == [True] and persistence.load()["wire_wake_listener"] is True


# ── main window start paths ────────────────────────────────────────────────

def _window_ns():
    import ui.main_window as mw
    panel = SimpleNamespace(states=[], reasons=[])
    panel.set_connection_state = panel.states.append
    panel.set_lock_reason = panel.reasons.append
    ns = SimpleNamespace(_wire_listener=None, _wire_autonomy=None, wire_panel=panel,
                         _wire_agent_busy=lambda: False)
    ns._wire_seat_allowed = lambda: mw.LuminaWindow._wire_seat_allowed(ns)
    return mw, ns


def test_both_start_functions_refuse_on_a_locked_root(tmp_path, monkeypatch, qapp):
    root = _root(tmp_path)
    _real_trace(root)
    _bind(monkeypatch, root)
    mw, ns = _window_ns()
    built = []
    monkeypatch.setattr(mw, "WireWakeListener", lambda *a, **k: built.append("listener"))
    monkeypatch.setattr(mw, "WireAutonomy", lambda *a, **k: built.append("autonomy"))
    mw.LuminaWindow._start_wire_listener(ns)
    mw.LuminaWindow._start_wire_autonomy(ns, fresh=True)
    assert built == []
    assert ns.wire_panel.states == ["locked", "locked"]
    assert ns.wire_panel.reasons == [G.describe(G.process_seat_verdict())] * 2


def test_start_functions_proceed_on_a_native_root(tmp_path, monkeypatch, qapp):
    _bind(monkeypatch, _root(tmp_path))
    mw, ns = _window_ns()

    class Built(Exception):
        pass

    def stop(*a, **k):
        raise Built

    monkeypatch.setattr(mw, "WireWakeListener", stop)
    monkeypatch.setattr(mw, "WireAutonomy", stop)
    with pytest.raises(Built):
        mw.LuminaWindow._start_wire_listener(ns)
    with pytest.raises(Built):
        mw.LuminaWindow._start_wire_autonomy(ns, fresh=True)


def test_every_wire_start_path_goes_through_the_gated_start_functions():
    text = (REPO / "ui" / "main_window.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LuminaWindow")
    funcs = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    for name in ("_start_wire_listener", "_start_wire_autonomy"):
        src = ast.unparse(funcs[name])
        assert "if not self._wire_seat_allowed():\n        return" in src, name
    # the only constructors of the listener/consumer are inside those functions
    for node_name, ctor in (("_start_wire_listener", "WireWakeListener("),
                            ("_start_wire_autonomy", "WireAutonomy(")):
        owners = [f for f, n in funcs.items() if ctor in ast.unparse(n)]
        assert owners == [node_name], (ctor, owners)


def test_the_wire_panel_shows_locked_with_its_reason(qapp):
    from ui.main_window import COLORS
    from ui.wire_panel import WireWakePanel
    panel = WireWakePanel(COLORS)
    panel.set_lock_reason("Wire locked: this is a restored copy. X")
    panel.set_connection_state("locked")
    assert panel._summary.text().startswith("Wire seat locked · Wire locked: this is a restored copy. X")
    panel.set_connection_state("off")
    assert "restored copy" not in panel._summary.text()


# ── long-running loops (A1-E1 E1-4) ────────────────────────────────────────

class _LockableClient:
    """Stub client: every call refuses with the quarantine error while locked."""

    def __init__(self):
        self.locked = False
        self.calls = 0

    def _check(self):
        self.calls += 1
        if self.locked:
            raise wire.WireSeatQuarantinedError(G.RECOVERED_UNRELEASED)

    def sessions_page(self, limit=100):
        self._check()
        return {"sessions": []}

    def unregister_session(self, session_id):
        self._check()
        return {}

    def register_session(self, **kw):
        self._check()
        return {"session_id": "s-1", "lease_live": True, "generation": 1}

    def heartbeat_session(self, session_id):
        self._check()
        return {"session_id": session_id, "lease_live": True, "generation": 1}

    def watch(self, **kw):
        self._check()
        yield {"event": "subscribed", "watermark": 0}
        time.sleep(0.01)
        yield {"event": "timeout"}

    def interrupt_sockets(self):
        pass


def _wait(pred, timeout=5.0, app=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if app is not None:
            app.processEvents()  # cross-thread signals are queued to the GUI thread
        if pred():
            return True
        time.sleep(0.01)
    return False


def _pump(app, seconds):
    _wait(lambda: False, timeout=seconds, app=app)


def test_listener_reports_locked_once_and_does_not_rearm_catchup(qapp, monkeypatch):
    import comms.wire_wake_listener as L
    monkeypatch.setattr(L, "SEAT_LOCK_RECHECK_SECONDS", 0.03)
    client = _LockableClient()
    listener = L.WireWakeListener(client=client, lease_seconds=1, heartbeat_seconds=0.02,
                                  watch_timeout_ms=100)
    catchups, states = [], []
    listener._emit_catchup = lambda session_id: catchups.append(session_id)
    listener._refresh_tracked_wakes = lambda: None
    listener.signals.connection_state.connect(states.append)
    listener.start()
    try:
        assert _wait(lambda: catchups == ["s-1"] and "listening" in states, app=qapp)
        client.locked = True
        assert _wait(lambda: "locked" in states, app=qapp)
        calls_at_lock = client.calls
        _pump(qapp, 0.5)  # many watch + heartbeat re-checks while locked
        assert client.calls > calls_at_lock + 5  # both threads keep re-checking
        assert states.count("locked") == 1
        assert "disconnected" not in states
        assert not listener._catchup_needed.is_set()
        assert catchups == ["s-1"]
        client.locked = False
        assert _wait(lambda: len(catchups) == 2, app=qapp)
        _pump(qapp, 0.2)
        assert catchups == ["s-1", "s-1"]  # exactly one catch-up on unlock
        assert states.count("locked") == 1
        assert not listener._seat_locked.is_set()
    finally:
        listener.shutdown()


def test_a_lock_present_before_the_first_session_is_reported_once_without_sweep_noise(
        qapp, monkeypatch, capsys):
    """WB1-03: locked from the start (no session id yet): the stale-session
    sweep must not swallow the quarantine and log every cycle."""
    import comms.wire_wake_listener as L
    monkeypatch.setattr(L, "SEAT_LOCK_RECHECK_SECONDS", 0.03)
    client = _LockableClient()
    client.locked = True
    listener = L.WireWakeListener(client=client, lease_seconds=1, heartbeat_seconds=0.02,
                                  watch_timeout_ms=100)
    catchups, states = [], []
    listener._emit_catchup = lambda session_id: catchups.append(session_id)
    listener._refresh_tracked_wakes = lambda: None
    listener.signals.connection_state.connect(states.append)
    listener.start()
    try:
        assert _wait(lambda: "locked" in states, app=qapp)
        _pump(qapp, 0.4)
        assert client.calls >= 5 and states.count("locked") == 1
        assert "disconnected" not in states and catchups == []
        assert "stale-session sweep skipped" not in capsys.readouterr().out
        client.locked = False
        assert _wait(lambda: "listening" in states and catchups == ["s-1"], app=qapp)
    finally:
        listener.shutdown()


class _IdleWatchClient(_LockableClient):
    """The watch stays subscribed and idle, so only the heartbeat re-calls."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def watch(self, **kw):
        self._check()
        yield {"event": "subscribed", "watermark": 0}
        self.release.wait(10)
        yield {"event": "timeout"}


def test_the_heartbeat_alone_reports_the_lock_while_the_watch_is_idle(qapp, monkeypatch):
    import comms.wire_wake_listener as L
    monkeypatch.setattr(L, "SEAT_LOCK_RECHECK_SECONDS", 0.03)
    client = _IdleWatchClient()
    listener = L.WireWakeListener(client=client, lease_seconds=1, heartbeat_seconds=0.02,
                                  watch_timeout_ms=100)
    states = []
    listener._emit_catchup = lambda session_id: None
    listener._refresh_tracked_wakes = lambda: None
    listener.signals.connection_state.connect(states.append)
    listener.start()
    try:
        assert _wait(lambda: "listening" in states, app=qapp)
        client.locked = True
        assert _wait(lambda: "locked" in states, app=qapp)
        _pump(qapp, 0.3)
        assert states.count("locked") == 1 and "disconnected" not in states
    finally:
        client.release.set()
        listener.shutdown()


class _DictPrefs:
    def __init__(self):
        self.d = {}

    def load(self):
        return dict(self.d)

    def update(self, mapping):
        self.d.update(mapping)
        return True


def test_autonomy_loop_logs_locked_once_and_rechecks_without_backoff(monkeypatch):
    import comms.wire_autonomy as A
    monkeypatch.setattr(A, "SEAT_LOCK_RECHECK_SECONDS", 0.02)
    client = _LockableClient()
    client.locked = True
    logs = []
    auto = A.WireAutonomy(client=client, prefs=_DictPrefs(), log=logs.append, watch_timeout_ms=100)
    auto.start()
    try:
        time.sleep(0.4)
        locked_logs = [l for l in logs if "Wire seat locked" in l]
        assert len(locked_logs) == 1, logs
        assert not any(l.startswith("relay:") for l in logs)
        assert client.calls >= 8  # fixed 0.02 s cadence, not 0.5 s doubling backoff
        client.locked = False
        assert _wait(lambda: any("Wire seat available again" in l for l in logs))
    finally:
        auto.shutdown()
