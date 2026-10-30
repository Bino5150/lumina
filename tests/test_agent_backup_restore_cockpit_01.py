"""AGENT-BACKUP-RESTORE-UI-01: the Qt-free cockpit core, against the real engine.

Disposable archives, destinations and install folders only (the fixtures are
the engine campaign's own). Nothing here touches a live Lumina, and this file
needs no PySide6, so it runs in the plain CI job as well.
"""
import ast
import errno
import json
import os
import shutil
import unicodedata
from pathlib import Path

import pytest

import core.agent_backup_restore as ar
import core.agent_backup_restore_cockpit as cp
import core.agent_backup_restore_intake as ri
from test_agent_backup_restore_02 import (  # noqa: F401  (fixtures)
    REPO, SENTINELS, entries, files_of, leftovers, legacy, repack, source, target, v1, v2, _killed_restore)


def preview(archive, target, selection=None, intake=None):
    data_dir, base = target
    intake = intake or cp.inspect_archive(str(archive))["intake"]
    selection = cp.default_selection(intake) if selection is None else selection
    return cp.build_plan(str(archive), str(data_dir), str(base), selection)


def recover(archive, target, selection=None):
    result = preview(archive, target, selection)
    assert result["ok"], result
    plan = result["plan"]
    return plan, cp.execute_recovery(plan, str(archive), plan["plan_digest"])


# --------------------------------------------------------------------------
# Drift guards: the cockpit cannot silently disagree with the engine
# --------------------------------------------------------------------------

def test_every_engine_and_intake_refusal_has_cockpit_wording():
    assert set(cp.REFUSAL_GUIDE) == set(ar.REFUSALS) | set(ri.REFUSALS)
    categories = {cp.UNSAFE_DESTINATION, cp.INVALID_ARCHIVE, cp.INCOMPLETE_COVERAGE, cp.INTERRUPTED_PENDING,
                  cp.PLAN_STALE, cp.INSUFFICIENT_RESOURCES, cp.RESTORE_FAILED}
    for code, (category, title, guidance) in cp.REFUSAL_GUIDE.items():
        assert category in categories and title and guidance, code


def test_class_mapping_and_choices_track_the_engine():
    for kind, restore_class in cp.CLASS_FOR_KIND.items():
        assert ar._restore_class(kind) == restore_class and restore_class in cp.CLASS_TEXT
    assert set(cp.CHOICE_TEXT) == set(ar.CHOICES)
    assert ar.VERIFIED_AGENT_BACKUP["available"] is False


def test_core_module_is_qt_free():
    tree = ast.parse(Path(cp.__file__).read_text())
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        assert not any("PySide" in (n or "") or "shiboken" in (n or "") for n in names)


def test_cockpit_never_reaches_the_engines_private_publishing_or_path_guards():
    # The cockpit relays the engine's refusals; it must not re-implement or
    # route around them.
    text = Path(cp.__file__).read_text()
    for name in ("_refuse_production", "_rename_noreplace", "_write_new", "_rollback_install",
                 "_production_roots", "monkeypatch"):
        assert name not in text, name


def test_safe_text_strips_control_and_bidi_and_bounds_length():
    nasty = "ok\x00\r\n\u202eevil\u2066x\x1b[31m\u200bend"
    clean = cp.safe_text(nasty)
    assert clean == "ok????" "evil?x?[31m?end"
    assert not cp._BAD_CHARS.search(clean) and "\n" not in clean and "\u202e" not in clean
    assert len(cp.safe_text("x" * 1000, 50)) == 50 and cp.safe_text("x" * 1000, 50).endswith("\u2026")
    assert cp.safe_text(None) == "" and cp.safe_text({"a": 1}) == "<dict>" and cp.safe_text(7) == "7"


# Each of these can reorder, hide or imitate text when it is drawn. U+061C, the
# soft hyphen, U+180E, the Hangul fillers and the tag characters were not
# covered by the first, hand-written range list.
INVISIBLE_OR_REORDERING = [
    "\u061c",                                  # ARABIC LETTER MARK: a bidi control
    "\u00ad", "\u180e", "\u2063", "\u2064",    # soft hyphen, MONGOLIAN VOWEL SEPARATOR, invisible separators
    "\u3164", "\u115f", "\u1160", "\uffa0",    # Hangul fillers: draw as blanks
    "\U000e0001", "\U000e0041", "\U000e007f",  # tag characters
    "\u0085", "\u2028", "\u2029",              # NEL, line and paragraph separators
    "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
]


@pytest.mark.parametrize("ch", INVISIBLE_OR_REORDERING, ids=lambda c: f"U+{ord(c):04X}")
def test_safe_text_replaces_every_invisible_or_reordering_character_one_for_one(ch):
    assert cp.safe_text(f"a{ch}b") == "a?b"
    assert cp.safe_text(ch * 5) == "?????"
    assert len(cp.safe_text("é" + ch + "ü")) == 3


def test_cockpit_sources_hold_no_raw_invisible_or_bidi_characters():
    """Trojan-Source hygiene: the characters are written as escapes, so a
    reviewer's editor shows what the code really contains."""
    root = Path(__file__).resolve().parent.parent
    for rel in ("core/agent_backup_restore_cockpit.py", "ui/settings/restore_cockpit.py",
                "tests/test_agent_backup_restore_cockpit_01.py", "tests/test_agent_backup_restore_cockpit_qt.py"):
        for number, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            for ch in line:
                category = unicodedata.category(ch)
                assert not (category in ("Cf", "Zl", "Zp") or (category == "Cc" and ch != "\t")
                            or ch in "\u115f\u1160\u3164\uffa0"), f"{rel}:{number} holds a raw U+{ord(ch):04X}"


def test_safe_text_bounds_length_before_and_after_replacement():
    assert cp.safe_text("\u061c" * 500, 50) == "?" * 49 + "…"
    assert cp.safe_text("ab\u3164" + "c" * 100, 10) == "ab?" + "c" * 6 + "…"


def test_safe_text_leaves_ordinary_international_text_alone():
    for text in ("Résumé – naïve", "مرحبا بالعالم",
                 "שלום עולם", "日本語のファイル名.txt", "한글 파일",
                 "folder/ünïcode (1).txt", "\U0001f600 ok", "100% sure"):
        assert cp.safe_text(text) == text


def test_invisible_and_reordering_characters_never_reach_the_rendered_intake():
    bad = "".join(INVISIBLE_OR_REORDERING)
    intake = {"schema": ri.INTAKE_SCHEMA, "kind": ri.VERIFIED, "format": "agent_export_v2.1" + bad,
              "source_path": "/tmp/a" + bad + "rchive.zip", "archive_sha256": "0" * 64, "archive_size": 5,
              "verification": {"status": "PASS", "error_count": 0},
              "coverage": {"installation_history": "x" + bad, "c1_at_source": "y" + bad,
                           "unsupported_required_features": ["f" + bad], "format_coverage": "q" + bad},
              "units": [{"unit": "project:ev" + bad + "il", "disposition": "recover", "availability": "partial",
                         "description": "d" + bad, "recoverable": [], "provenance": "p" + bad,
                         "skipped": [{"archive_path": "overlay/x" + bad + ".md", "reason": "weird" + bad + "reason"}]}],
              "units_not_in_archive": [{"unit": "u" + bad, "reason": "r" + bad}]}
    text = cp.render_intake_text(cp.summarize_intake(intake))
    assert "reported problem: weird" + "?" * len(INVISIBLE_OR_REORDERING) + "reason" in text
    for ch in text:
        assert ch == "\n" or (unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp")
                              and ch not in "\u3164\u115f\u1160\uffa0"), ascii(ch)


# --------------------------------------------------------------------------
# Inspect: read-only identity, classification, coverage
# --------------------------------------------------------------------------

def test_inspect_clean_export_shows_identity_class_and_honest_limits(v2):
    import hashlib
    result = cp.inspect_archive(str(v2))
    view = result["view"]
    assert result["ok"] and view["sha256"] == hashlib.sha256(v2.read_bytes()).hexdigest()
    assert view["size"] == v2.stat().st_size and view["name"] == "v2.zip"
    assert view["restore_class"] == ar.INCOMPLETE and view["class_label"] == "Incomplete State Export recovery"
    text = cp.render_intake_text(view)
    for must in ("Verified Agent Backup is unavailable", "Verification PASS covers ZIP structure",
                 "C1 safety state is not captured and is never restored", "credentials",
                 "not proof of origin", "intact, not where it came from"):
        assert must in text, must
    assert "Verified Agent Backup created" not in text
    units = {u["unit"]: u for u in view["units"]}
    assert units["primary_database"]["selectable"] and units["custom_tools"]["inactive_history"]
    assert all(u["selectable"] for u in units.values() if u["availability"] != "unavailable")


def test_installation_history_wording_matches_where_the_engine_puts_the_marker(v2, target):
    """The recovered marker lands where Lumina's Knowledge Pack startup step
    reads it, so the wording must not call it inert 'history only'."""
    from core import knowledge_bootstrap as kb
    data_dir, _base = target
    intake = cp.inspect_archive(str(v2))["intake"]
    assert intake["coverage"]["installation_history"] == "present"
    text = cp.render_intake_text(cp.summarize_intake(intake))
    assert "history only" not in text.lower() and "inert" not in text.lower()
    for must in ("historical installation state", "influence whether official packs are installed again",
                 "grants no authority and no official-package trust"):
        assert must in " ".join(text.split()), must
    plan, result = recover(v2, target)
    assert result["ok"]
    marker = kb._marker_path(str(data_dir))
    assert marker == str(data_dir / "knowledge_packs_installed.json") and os.path.isfile(marker)
    assert isinstance(kb._load_marker(str(data_dir)), dict)       # the file bootstrap would act on
    assert not os.path.exists(data_dir / ar.HISTORY_DIRNAME / plan["restore_id"] / "knowledge_packs_installed.json")


def test_inspect_legacy_is_limited_and_unknown_provenance(legacy):
    view = cp.inspect_archive(str(legacy))["view"]
    assert view["restore_class"] == ar.LEGACY and view["class_label"] == "Legacy Recovery"
    text = cp.render_intake_text(view)
    assert "no manifest or hashes" in text and "Provenance is unknown" in text
    assert "are not in a legacy ZIP" in text and "evidence only" in text


def test_inspect_tampered_archive_is_selective_with_reasons_and_skip_defaults(v2):
    repack(v2, [(n, b'{"name": "TAMPERED"}' if n == "overlay/personas/lumina.json" else d)
                for n, d in entries(v2)])
    result = cp.inspect_archive(str(v2))
    assert result["ok"] and result["view"]["restore_class"] == ar.SELECTIVE
    units = {u["unit"]: u for u in result["view"]["units"]}
    assert not units["personas"]["selectable"]
    assert any("SHA-256" in r for r in units["personas"]["reasons"])
    assert "FAILED" in cp.render_intake_text(result["view"])
    defaults = cp.default_selection(result["intake"])
    assert defaults and set(defaults.values()) == {"skip"} and "personas" not in defaults


@pytest.mark.parametrize("kind,category", [("not_a_zip", cp.INVALID_ARCHIVE), ("traversal", cp.INVALID_ARCHIVE),
                                           ("malformed_manifest", cp.INVALID_ARCHIVE),
                                           ("newer_format", cp.INVALID_ARCHIVE)])
def test_inspect_rejects_invalid_archives_without_units(kind, category, v2):
    from test_agent_backup_restore_02 import _hostile
    _hostile(kind, v2)
    result = cp.inspect_archive(str(v2))
    assert not result["ok"] and result["category"] == category and result["intake"]["units"] == []
    assert result["sha256"] and "recover" not in result["title"].lower()


def test_inspect_missing_and_unexpected_inputs_never_raise_or_leak(v2, monkeypatch):
    missing = cp.inspect_archive(str(v2) + ".nope")
    assert not missing["ok"] and missing["category"] == cp.INVALID_ARCHIVE
    monkeypatch.setattr(ri, "intake_archive", lambda p: (_ for _ in ()).throw(ValueError("<b>SECRET-ARCHIVE-TEXT</b>")))
    boom = cp.inspect_archive(str(v2))
    assert not boom["ok"] and boom["category"] == cp.UNEXPECTED_ERROR
    assert "SECRET-ARCHIVE-TEXT" not in json.dumps(boom) and boom["detail"] == "ValueError"


def test_archive_derived_text_is_made_display_safe():
    intake = {"schema": ri.INTAKE_SCHEMA, "kind": ri.VERIFIED, "format": "agent_export_v2.1\n\u202e",
              "source_path": "/tmp/a\u202erchive\n.zip", "archive_sha256": "0" * 64, "archive_size": 5,
              "verification": {"status": "PASS", "error_count": 0},
              "coverage": {"installation_history": "x\ny\u202e", "c1_at_source": "<a href=file:///etc/passwd>",
                           "unsupported_required_features": ["f\n1"], "format_coverage": "q"},
              "units": [{"unit": "project:ev\nil\u202e", "disposition": "recover", "availability": "partial",
                         "description": "d\x00", "recoverable": [], "provenance": "p",
                         "skipped": [{"archive_path": "overlay/x\u202e\n.md", "reason": "weird\nreason"}]}],
              "units_not_in_archive": [{"unit": "u\n", "reason": "r\u202e"}]}
    view = cp.summarize_intake(intake)
    text = cp.render_intake_text(view)
    assert not cp._BAD_CHARS.search(text.replace("\n", " "))
    assert "\u202e" not in text and "reported problem: weird?reason" in text


# --------------------------------------------------------------------------
# Preview: exact changes, conflicts, unsafe destinations, stale inputs
# --------------------------------------------------------------------------

def test_preview_describes_every_change_and_writes_nothing(v2, target):
    data_dir, base = target
    before = (files_of(base), files_of(data_dir.parent))
    result = preview(v2, target)
    assert result["ok"]
    plan = result["plan"]
    text = cp.render_plan_text(plan, result["warnings"])
    assert plan["plan_digest"] in text and "nothing has been written" in text
    assert "READY for your approval" in text and "THESE ALWAYS HOLD" in text
    assert "[data] memory/lumina.db: create" in text and f"restore_history/{plan['restore_id']}" in text
    assert (files_of(base), files_of(data_dir.parent)) == before and not data_dir.exists() and not leftovers(data_dir)


def test_conflicts_block_approval_until_the_owner_chooses(v2, target):
    data_dir, base = target
    (base / "personas/lumina.json").write_text('{"name": "Destination"}')
    plan = preview(v2, target)["plan"]
    text = cp.render_plan_text(plan)
    assert plan["status"] == "needs_owner_conflict_choice"
    assert "NEEDS YOUR DECISION" in text and "INSTALL-FILE CONFLICTS" in text and "personas/lumina.json" in text
    assert cp.recovery_conflicts(plan) == ["personas/lumina.json"]
    intake = cp.inspect_archive(str(v2))["intake"]
    keep = {**cp.default_selection(intake), "personas": "recover_keep_destination"}
    kept = preview(v2, target, keep)["plan"]
    assert kept["status"] == "ready" and "your file is kept" in cp.render_plan_text(kept)
    skip = {**cp.default_selection(intake), "personas": "skip"}
    assert preview(v2, target, skip)["plan"]["status"] == "ready"
    assert (base / "personas/lumina.json").read_text() == '{"name": "Destination"}'


@pytest.mark.parametrize("where", ["home_production", "env_data_dir", "running_install", "not_empty",
                                   "parent_missing", "install_missing", "install_is_link"])
def test_unsafe_destinations_are_relayed_as_refusals(where, v2, target, tmp_path, monkeypatch):
    data_dir, base = target
    expected = cp.UNSAFE_DESTINATION
    if where == "home_production":
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home/.local/share/lumina").mkdir(parents=True)
        data_dir = tmp_path / "home/.local/share/lumina/restored"
        code = "PRODUCTION_ROOT"
    elif where == "env_data_dir":
        live = tmp_path / "live_data"
        (live / "sub").mkdir(parents=True)
        monkeypatch.setenv("LUMINA_DATA_DIR", str(live))
        data_dir = live / "sub" / "restored"
        code = "PRODUCTION_ROOT"
    elif where == "running_install":
        base = REPO
        code = "PRODUCTION_ROOT"
    elif where == "not_empty":
        data_dir.mkdir()
        (data_dir / "x").write_text("owner file")
        code = "DESTINATION_NOT_EMPTY"
    elif where == "parent_missing":
        data_dir = tmp_path / "no_such_parent" / "data"
        code = "DESTINATION_PARENT_UNSAFE"
    elif where == "install_missing":
        base = tmp_path / "no_install"
        code = "INSTALL_FOLDER_UNSAFE"
    else:
        (tmp_path / "linked").symlink_to(base, target_is_directory=True)
        base = tmp_path / "linked"
        code = "INSTALL_FOLDER_UNSAFE"
    intake = cp.inspect_archive(str(v2))["intake"]
    result = cp.build_plan(str(v2), str(data_dir), str(base), cp.default_selection(intake))
    assert not result["ok"] and result["code"] == code and result["category"] == expected
    assert result["title"] and result["guidance"]
    if where == "not_empty":
        assert (data_dir / "x").read_text() == "owner file"


def test_a_selection_naming_a_damaged_unit_is_refused_not_adjusted(v2, target):
    repack(v2, [(n, b'{"name": "TAMPERED"}' if n == "overlay/personas/lumina.json" else d)
                for n, d in entries(v2)])
    result = preview(v2, target, {"personas": "recover", "primary_database": "recover"})
    assert not result["ok"] and result["code"] == "SELECTION_INVALID" and result["category"] == cp.INCOMPLETE_COVERAGE
    ok = preview(v2, target, {"primary_database": "recover", "skills": "recover"})
    assert ok["ok"] and ok["plan"]["restore_class"] == ar.SELECTIVE
    assert "Selective Recovery" in cp.render_plan_text(ok["plan"])


def test_legacy_plan_with_pending_wal_needs_an_explicit_choice(legacy, target):
    repack(legacy, entries(legacy) + [("memory/lumina.db-wal", b"uncheckpointed frames")], canonical=False)
    inspected = cp.inspect_archive(str(legacy))
    assert inspected["ok"]
    units = {u["unit"]: u for u in inspected["view"]["units"]}
    assert not units["primary_database"]["selectable"]
    assert any("pending WAL" in r for r in units["primary_database"]["reasons"])
    defaults = cp.default_selection(inspected["intake"])
    assert set(defaults.values()) == {"skip"} and "primary_database" not in defaults
    # With the table untouched nothing is selected, and the session says so.
    session = cp.RestoreSession()
    session.accept_intake(session.set_archive(str(legacy)), inspected)
    session.set_folders(str(target[0]), str(target[1]))
    assert session.preview_blocker() == "Nothing is selected to recover."
    # Naming the damaged unit is refused; naming a valid one plans.
    assert preview(legacy, target, {"primary_database": "recover"})["code"] == "SELECTION_INVALID"
    chosen = preview(legacy, target, {"preferences": "recover"})
    assert chosen["ok"] and chosen["plan"]["restore_class"] == ar.LEGACY
    assert "Legacy Recovery" in cp.render_plan_text(chosen["plan"])


def test_a_preview_refusal_is_called_an_archive_change_only_when_the_bytes_changed(v2, v1, target):
    inspected = cp.inspect_archive(str(v2))["intake"]
    selection = cp.default_selection(inspected)
    data_dir, base = target
    # Archive swapped after inspection: the stale selection is refused by the engine; the cockpit says why.
    shutil.copy(v1, v2)
    changed = cp.build_plan(str(v2), str(data_dir), str(base), selection, inspected["archive_sha256"])
    assert not changed["ok"] and changed["code"] == "ARCHIVE_CHANGED" and changed["category"] == cp.PLAN_STALE
    assert inspected["archive_sha256"][:16] in changed["detail"]
    # Same bytes, genuinely invalid selection: the engine's own refusal is shown, not relabelled.
    same = cp.inspect_archive(str(v2))["intake"]
    bogus = cp.build_plan(str(v2), str(data_dir), str(base), {"no_such_unit": "recover"}, same["archive_sha256"])
    assert bogus["code"] == "SELECTION_INVALID" and bogus["category"] == cp.INCOMPLETE_COVERAGE


def test_unwritable_parent_is_flagged_as_a_heads_up(v2, target, monkeypatch):
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    result = preview(v2, target)
    assert result["ok"] and any("parent folder does not look writable" in w for w in result["warnings"])


# --------------------------------------------------------------------------
# Execute: success, stale plans, failures, post-publication truth
# --------------------------------------------------------------------------

def test_recovery_publishes_verifies_and_reports_without_overclaiming(v2, target):
    data_dir, base = target
    plan, result = recover(v2, target)
    assert result["ok"] and result["outcome"] == "published_verified"
    assert result["verification"]["passed"] and all(result["verification"]["checks"].values())
    receipt_path = Path(result["receipt_path"])
    assert receipt_path == data_dir / "restore_history" / plan["restore_id"] / "receipt.json" and receipt_path.is_file()
    assert json.loads(receipt_path.read_text())["restore_class"] == ar.INCOMPLETE
    text = cp.describe_success(result)
    assert "ISOLATED RECOVERY PUBLISHED AND VERIFIED" in text and "Incomplete State Export recovery" in text
    assert "has not been activated" in text and "not yet a supported or verified path" in text
    assert "Verified Agent Backup: not available" in text
    for banned in ("Verified Agent Backup created", "disaster recovery complete", "Lumina restored"):
        assert banned.lower() not in text.lower()
    assert not leftovers(data_dir)


def test_authority_and_credentials_are_not_resurrected_through_the_cockpit(legacy, v1, source, target, tmp_path):
    for archive, name in ((legacy, "legacy"), (v1, "v1")):
        data_dir = tmp_path / f"restore_{name}" / "data"
        data_dir.parent.mkdir()
        install = tmp_path / f"inst_{name}"
        (install / "skills").mkdir(parents=True)
        plan, result = recover(archive, (data_dir, install))
        assert result["ok"], result
        # Not vacuous: the history really was recovered.
        assert (data_dir / "memory/lumina.db").is_file() and (data_dir / "memory/prefs.json").is_file()
        assert plan["restore_class"] == (ar.LEGACY if name == "legacy" else ar.INCOMPLETE)
        blob = b"".join(p.read_bytes() for p in data_dir.rglob("*") if p.is_file())
        for sentinel in SENTINELS:
            assert sentinel.encode() not in blob, (name, sentinel)
        for rel in ("memory/suspension.db", "memory/soul_identity.json", "memory/ledger.db", "credentials.json",
                    "memory/pending_actions.json", "custom_tools"):
            assert not (data_dir / rel).exists(), (name, rel)
        checks = result["verification"]["checks"]
        for key in ("no_live_authority_names", "prefs_fenced", "c1_restore_trace", "skill_authority_fenced"):
            assert checks[key] is True, (name, key)
        prefs = json.loads((data_dir / "memory/prefs.json").read_text())
        assert prefs["telegram_remote_access"] is False
        assert not any(k.startswith(("pin", "openai", "telegram_owner")) for k in prefs)


def test_archive_changed_between_preview_and_execute_is_stale_and_writes_nothing(v2, target):
    data_dir, base = target
    plan = preview(v2, target)["plan"]
    before = files_of(base)
    repack(v2, [(n, d + b" " if n == "overlay/personas/lumina.json" else d) for n, d in entries(v2)])
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])
    assert not result["ok"] and result["category"] == cp.PLAN_STALE and result["code"] == "ARCHIVE_CHANGED"
    assert "SHA-256" in result["guidance"] and "Inspect it again" in result["guidance"]
    assert not data_dir.exists() and not leftovers(data_dir) and files_of(base) == before
    assert result["rollback"] is None and "Nothing was written" in result["guidance"]
    assert cp.describe_failure(result)


def test_archive_swapped_for_a_valid_different_one_is_plan_changed(v2, v1, target):
    data_dir, base = target
    plan = preview(v2, target)["plan"]
    shutil.copy(v1, v2)
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])
    # The engine re-plans before it writes; its refusal is confirmed against the SHA-256 and named.
    assert not result["ok"] and result["category"] == cp.PLAN_STALE and result["code"] == "ARCHIVE_CHANGED"
    assert "SHA-256" in result["guidance"] and result["rollback"] is None
    assert not data_dir.exists() and not leftovers(data_dir)


def test_destination_changed_after_preview_is_refused_and_untouched(v2, target):
    data_dir, base = target
    plan = preview(v2, target)["plan"]
    data_dir.mkdir()
    (data_dir / "late").write_text("arrived after the preview")
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])
    assert not result["ok"] and result["category"] == cp.UNSAFE_DESTINATION and result["code"] == "DESTINATION_NOT_EMPTY"
    assert files_of(data_dir) == {"late": b"arrived after the preview"} and not leftovers(data_dir)


def test_install_file_changed_after_preview_is_plan_changed(v2, target):
    data_dir, base = target
    plan = preview(v2, target)["plan"]
    (base / "personas").mkdir(exist_ok=True)
    (base / "personas/lumina.json").write_text('{"name": "appeared later"}')
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])
    assert not result["ok"] and result["code"] == "PLAN_CHANGED" and result["category"] == cp.PLAN_STALE
    assert not data_dir.exists() and (base / "personas/lumina.json").read_text() == '{"name": "appeared later"}'


def test_a_genuine_engine_refusal_at_execute_time_is_not_relabelled_as_an_archive_change(v2, target, monkeypatch):
    data_dir, _base = target
    plan = preview(v2, target)["plan"]
    # Same archive bytes, but the engine's pre-write re-plan refuses for another reason.
    monkeypatch.setattr(ar, "plan_restore", lambda *a, **k: (_ for _ in ()).throw(
        ar.RestoreRefused("ARCHIVE_REJECTED", "INPUT_UNAVAILABLE")))
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])
    assert not result["ok"] and result["code"] == "ARCHIVE_REJECTED" and result["category"] == cp.INVALID_ARCHIVE
    assert "ARCHIVE_CHANGED" not in json.dumps(result) and not data_dir.exists()


def test_wrong_or_missing_digest_never_executes(v2, target):
    data_dir, _base = target
    plan = preview(v2, target)["plan"]
    for digest in ("0" * 64, "", None):
        result = cp.execute_recovery(plan, str(v2), digest)
        assert not result["ok"] and result["code"] == "PLAN_NOT_APPROVED" and result["category"] == cp.PLAN_STALE
    tampered = json.loads(json.dumps(plan))
    tampered["selection"]["skills"] = "skip"
    assert cp.execute_recovery(tampered, str(v2), plan["plan_digest"])["code"] == "PLAN_NOT_APPROVED"
    assert not data_dir.exists()
    assert cp.execute_recovery({"not": "a plan"}, str(v2), "x")["category"] == cp.UNEXPECTED_ERROR


def test_a_conflicted_plan_cannot_execute(v2, target):
    data_dir, base = target
    (base / "personas/lumina.json").write_text('{"name": "Destination"}')
    plan = preview(v2, target)["plan"]
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])
    assert result["code"] == "OWNER_CONFLICT_CHOICE_REQUIRED" and result["category"] == cp.INCOMPLETE_COVERAGE
    assert not data_dir.exists()


def test_failure_after_staging_reports_the_engines_rollback(v2, target, monkeypatch):
    data_dir, base = target
    before = files_of(base)

    def disk_full(stage):
        if stage == "install_file_created":
            raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(ar, "_checkpoint", disk_full)
    _plan, result = recover(v2, target)
    assert not result["ok"] and result["category"] == cp.RESTORE_FAILED and result["code"] == "WRITE_FAILED"
    assert result["rollback"]["destination_untouched"] and result["rollback"]["staging_removed"]
    text = cp.describe_failure(result)
    assert "Destination untouched: yes" in text and "Staging folder removed: yes" in text
    assert not data_dir.exists() and not leftovers(data_dir) and files_of(base) == before


def test_unexpected_error_before_publish_hides_its_text_and_says_what_exists(v2, target, monkeypatch):
    data_dir, _base = target

    def boom(stage):
        if stage == "before_publish":
            raise ValueError("<img src=x onerror=alert(1)>ARCHIVE-DERIVED")
    monkeypatch.setattr(ar, "_checkpoint", boom)
    _plan, result = recover(v2, target)
    assert not result["ok"] and result["category"] == cp.UNEXPECTED_ERROR and result["destination_exists"] is False
    assert "ARCHIVE-DERIVED" not in json.dumps(result) and result["detail"] == "ValueError"
    assert "The destination does not exist" in result["guidance"] and not leftovers(data_dir)


def test_error_after_publication_is_reported_as_published_and_verified(v2, target, monkeypatch):
    data_dir, _base = target

    def late(stage):
        if stage == "published":
            raise OSError(errno.EIO, "I/O error on the final directory sync")
    monkeypatch.setattr(ar, "_checkpoint", late)
    _plan, result = recover(v2, target)
    assert result["ok"] and result["outcome"] == "published_verified"
    assert any("after publishing (OSError)" in w for w in result["warnings"])
    assert "NOTE: The engine reported an error after publishing" in cp.describe_success(result)
    assert (data_dir / "memory/lumina.db").is_file()


@pytest.mark.parametrize("stored", ["this_plans_receipt", "foreign_plan_digest", "no_plan_digest",
                                    "another_recovery_id", "files_changed_after_publish"])
def test_error_after_publication_needs_the_approved_plans_own_receipt_and_a_passing_verification(
        v2, target, monkeypatch, stored):
    """After the engine raises past the publishing rename the cockpit may say
    'published and verified' only when the folder's stored receipt belongs to
    the plan the owner approved AND the folder still verifies against it. A
    receipt that merely sits at that path with another plan digest (or none,
    or another recovery id) is not evidence, and neither is a folder whose
    files no longer match the genuine receipt."""
    data_dir, _base = target
    plan = preview(v2, target)["plan"]
    path = data_dir / ar.HISTORY_DIRNAME / plan["restore_id"] / ar.RECEIPT_NAME

    def late(stage):
        if stage != "published":
            return
        receipt = json.loads(path.read_text())
        if stored == "foreign_plan_digest":
            receipt["plan_digest"] = "f" * 64
        elif stored == "no_plan_digest":
            del receipt["plan_digest"]
        elif stored == "another_recovery_id":
            receipt["restore_id"] = "e" * 32
        elif stored == "files_changed_after_publish":
            (data_dir / "memory/prefs.json").write_text('{"telegram_remote_access": true}')
        path.write_text(json.dumps(receipt))
        raise OSError(errno.EIO, "I/O error on the final directory sync")
    monkeypatch.setattr(ar, "_checkpoint", late)
    result = cp.execute_recovery(plan, str(v2), plan["plan_digest"])

    if stored == "this_plans_receipt":                       # the control: same setup, genuine receipt
        assert result["ok"] and result["outcome"] == "published_verified"
        return
    if stored == "files_changed_after_publish":              # genuine receipt, but the folder no longer matches it
        assert not result["ok"] and result["outcome"] == "published_unverified"
        assert result["category"] == cp.PUBLISHED_UNVERIFIED and result["verification"]["passed"] is False
        return
    assert not result["ok"] and result["category"] == cp.UNEXPECTED_ERROR
    assert result.get("outcome") != "published_verified" and "verification" not in result
    assert not any("after publishing" in w for w in result.get("warnings", []))
    assert result["destination_exists"] is True and "was not verified" in result["guidance"]
    assert "PUBLISHED AND VERIFIED" not in cp.describe_failure(result).upper()
    loads = cp.load_stored_receipt(str(data_dir), plan["restore_id"]) is not None
    # Only the digest case isolates the digest comparison: that receipt is
    # otherwise acceptable, so nothing but the comparison refuses it.
    assert loads == (stored in ("foreign_plan_digest", "no_plan_digest"))


def test_failed_post_publication_verification_is_never_success(v2, target, monkeypatch):
    real = ar.verify_restored_root
    monkeypatch.setattr(ar, "verify_restored_root",
                        lambda d, r: {**real(d, r), "passed": False, "checks": {**real(d, r)["checks"], "data_files_match": False}})
    _plan, result = recover(v2, target)
    assert not result["ok"] and result["outcome"] == "published_unverified" and result["category"] == cp.PUBLISHED_UNVERIFIED
    text = cp.describe_success(result)
    assert "PUBLISHED - VERIFICATION FAILED" in text and "[FAIL] Every recovered data file" in text
    assert "do not use this folder" in cp.describe_failure(result).lower()

    monkeypatch.setattr(ar, "verify_restored_root", lambda d, r: (_ for _ in ()).throw(RuntimeError("SECRET")))
    data_dir = target[0].parent / "second"
    _plan, again = recover(v2, (data_dir, target[1]))
    assert not again["ok"] and again["outcome"] == "published_unverified"
    assert "SECRET" not in json.dumps(again, default=str) and "RuntimeError" in again["verification"]["error"]


def test_a_deliberately_damaged_published_root_fails_verification(v2, target):
    data_dir, _base = target
    _plan, result = recover(v2, target)
    assert result["ok"]
    assert cp.verify_recovered_folder(str(data_dir))["ok"]
    (data_dir / "memory/prefs.json").write_text('{"telegram_remote_access": true}')
    bad = cp.verify_recovered_folder(str(data_dir))
    assert not bad["ok"] and bad["outcome"] == "published_unverified"
    assert bad["verification"]["checks"]["data_files_match"] is False
    assert "[FAIL]" in cp.describe_success(bad)


def test_standalone_verify_is_read_only_and_states_its_limit(v2, target):
    data_dir, _base = target
    recover(v2, target)
    before = files_of(data_dir)
    good = cp.verify_recovered_folder(str(data_dir))
    assert good["ok"] and "cannot prove the receipt itself is the original" in good["caveat"]
    assert files_of(data_dir) == before


def test_standalone_verify_refuses_foreign_or_hostile_shapes(v2, target, tmp_path):
    data_dir, _base = target
    recover(v2, target)
    (empty := tmp_path / "plain").mkdir()
    assert cp.verify_recovered_folder(str(empty))["code"] == "NO_RECEIPT"
    (rid,) = [p.name for p in (data_dir / "restore_history").iterdir()]
    # Two receipts: ambiguous.
    shutil.copytree(data_dir / "restore_history" / rid, data_dir / "restore_history" / ("a" * 32))
    assert cp.verify_recovered_folder(str(data_dir))["code"] == "NO_RECEIPT"
    shutil.rmtree(data_dir / "restore_history" / ("a" * 32))
    # A receipt whose paths climb out of the folders is never handed to the engine.
    path = data_dir / "restore_history" / rid / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["data_files"][0]["rel"] = "../../outside"
    path.write_text(json.dumps(receipt))
    result = cp.verify_recovered_folder(str(data_dir))
    assert not result["ok"] and result["code"] == "RECEIPT_UNREADABLE"
    # A symlinked receipt is not followed.
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(receipt))
    path.unlink()
    path.symlink_to(real)
    assert cp.verify_recovered_folder(str(data_dir))["code"] == "RECEIPT_UNREADABLE"


def test_a_receipt_reached_through_a_linked_folder_is_never_read(v2, target, tmp_path):
    data_dir, _base = target
    plan, result = recover(v2, target)
    rid = plan["restore_id"]
    assert result["ok"] and cp.load_stored_receipt(data_dir, rid) is not None
    history = data_dir / "restore_history"
    shutil.move(str(history / rid), str(tmp_path / "moved_id"))
    (history / rid).symlink_to(tmp_path / "moved_id", target_is_directory=True)
    assert cp.read_stored_receipt_bytes(data_dir, rid) is None and cp.load_stored_receipt(data_dir, rid) is None
    (history / rid).unlink()
    shutil.move(str(tmp_path / "moved_id"), str(tmp_path / "moved_history"))
    shutil.rmtree(history)
    history.symlink_to(tmp_path / "moved_history", target_is_directory=True)
    assert cp.read_stored_receipt_bytes(data_dir, rid) is None
    assert cp.verify_recovered_folder(str(data_dir))["code"] == "NO_RECEIPT"


def test_receipt_shape_check_rejects_malformed_receipts():
    good = {"schema": ar.RECEIPT_SCHEMA, "restore_id": "a" * 32, "receipt_digest": "x",
            "destination": {"install_dir": "/tmp/i"}, "data_files": [{"rel": "memory/a", "sha256": "0" * 64}],
            "install_files": [{"rel": "skills/a.md", "sha256": "0" * 64, "action": "create"}],
            "sqlite": {"memory/lumina.db": "ok"}}
    assert cp.receipt_problem(good) is None
    for mutate in (lambda r: r.update(schema="x"), lambda r: r.update(restore_id="../" + "a" * 29),
                   lambda r: r["destination"].update(install_dir="relative"),
                   lambda r: r["data_files"][0].update(rel="/etc/passwd"),
                   lambda r: r["data_files"][0].update(rel="a/../../b"),
                   lambda r: r["data_files"][0].update(sha256="zz"),
                   lambda r: r["install_files"][0].pop("action"),
                   lambda r: r.update(sqlite={"../x": 1}), lambda r: r.pop("destination"),
                   lambda r: r.update(data_files="no")):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        assert cp.receipt_problem(bad), bad
    assert cp.receipt_problem([]) and cp.receipt_problem(None)


# --------------------------------------------------------------------------
# Interrupted recovery
# --------------------------------------------------------------------------

def test_interrupted_restore_is_named_then_rolled_back_by_the_owners_action(v2, target, tmp_path):
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    assert leftovers(data_dir)
    blocked = preview(v2, target)
    assert not blocked["ok"] and blocked["category"] == cp.INTERRUPTED_PENDING
    assert "Check for interrupted recovery" in blocked["guidance"]
    report = cp.recover_interrupted(str(data_dir))
    assert report["ok"] and report["report"]["rolled_back"] and not report["needs_quarantine_decision"]
    assert any("Rolled back an interrupted recovery" in line for line in report["lines"])
    assert not leftovers(data_dir) and preview(v2, target)["ok"]
    nothing = cp.recover_interrupted(str(data_dir))
    assert nothing["lines"] == ["No interrupted recovery was found for this destination."]


def test_unrecognised_staging_is_left_alone_until_a_second_explicit_action(v2, target):
    data_dir, base = target
    rogue = data_dir.parent / f".{data_dir.name}.restore-{'e' * 32}.staging"
    rogue.mkdir(mode=0o770)
    (rogue / "keep.txt").write_text("not ours")
    rogue.chmod(0o770)
    first = cp.recover_interrupted(str(data_dir))
    assert first["ok"] and first["needs_quarantine_decision"] and rogue.exists()
    assert any("LEFT ALONE" in line for line in first["lines"])
    assert preview(v2, target)["category"] == cp.INTERRUPTED_PENDING
    second = cp.recover_interrupted(str(data_dir), quarantine_unrecognized=True)
    assert second["ok"] and second["op"] == "quarantine" and second["report"]["quarantined"]
    moved = Path(second["report"]["quarantined"][0]["to"])
    assert moved.exists() and (moved / "keep.txt").read_text() == "not ours" and not rogue.exists()


def test_interrupted_recovery_refuses_production_destinations(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home/.local/share/lumina-release").mkdir(parents=True)
    result = cp.recover_interrupted(str(tmp_path / "home/.local/share/lumina-release"))
    assert not result["ok"] and result["code"] == "PRODUCTION_ROOT" and result["category"] == cp.UNSAFE_DESTINATION


# --------------------------------------------------------------------------
# RestoreSession: owner consent bound to the plan digest
# --------------------------------------------------------------------------

@pytest.fixture
def session(v2, target):
    s = cp.RestoreSession()
    gen = s.set_archive(str(v2))
    assert s.accept_intake(gen, cp.inspect_archive(str(v2)))
    s.set_folders(str(target[0]), str(target[1]))
    return s


def planned(session):
    request = session.request_preview()
    result = cp.build_plan(request["archive_path"], request["data_dir"], request["base_dir"], request["selection"],
                           request["inspected_sha256"])
    assert session.accept_plan(request["generation"], request, result) is None
    return result


def test_approval_is_bound_to_the_displayed_digest(session):
    planned(session)
    assert not session.can_execute
    assert not session.approve("0" * 64) and not session.approve(None) and not session.can_execute
    assert session.approve(session.plan["plan_digest"]) and session.can_execute


@pytest.mark.parametrize("change", ["data_dir", "base_dir", "choice", "archive"])
def test_any_input_change_drops_the_plan_and_the_approval(change, session, tmp_path, v1):
    planned(session)
    assert session.approve(session.plan["plan_digest"])
    gen = session.generation
    if change == "data_dir":
        session.set_folders(session.data_dir + "x", session.base_dir)
    elif change == "base_dir":
        session.set_folders(session.data_dir, session.base_dir + "x")
    elif change == "choice":
        session.set_choice("skills", "skip")
    else:
        session.set_archive(str(v1))
    assert session.plan is None and session.approved_digest is None and not session.can_execute
    assert session.generation > gen and session.invalidated_because
    with pytest.raises(cp.SessionError):
        session.begin_execution()


@pytest.mark.parametrize("drift", ["data_dir", "base_dir", "archive_path", "selection"])
def test_a_plan_cannot_run_if_an_input_drifted_without_going_through_the_session(drift, session):
    # Defence in depth behind _drop_plan: whatever touches a field directly, the approval no longer fits.
    planned(session)
    assert session.approve(session.plan["plan_digest"]) and session.can_execute
    if drift == "selection":
        session.selection["skills"] = "skip"
    else:
        setattr(session, drift, getattr(session, drift) + "x")
    assert not session.can_execute
    with pytest.raises(cp.SessionError):
        session.begin_execution()


def test_unchanged_inputs_do_not_invalidate(session):
    planned(session)
    session.approve(session.plan["plan_digest"])
    session.set_folders(session.data_dir, session.base_dir)
    session.set_choice("skills", session.selection["skills"])
    assert session.can_execute


def test_late_results_for_older_generations_are_dropped(session):
    request = session.request_preview()
    result = cp.build_plan(request["archive_path"], request["data_dir"], request["base_dir"], request["selection"])
    session.set_choice("skills", "skip")
    assert session.accept_plan(request["generation"], request, result) == "stale"
    assert session.plan is None
    gen = session.set_archive(session.archive_path)
    session.set_archive(session.archive_path)
    assert not session.accept_intake(gen, {"ok": True, "intake": {"units": []}})


def test_a_plan_for_a_different_archive_or_selection_is_never_accepted(session, v1, target):
    request = session.request_preview()
    other = cp.build_plan(str(v1), request["data_dir"], request["base_dir"],
                          cp.default_selection(cp.inspect_archive(str(v1))["intake"]))
    assert other["ok"]
    assert "archive changed" in session.accept_plan(request["generation"], request, other)
    assert session.plan is None
    request = session.request_preview()
    fewer = cp.build_plan(request["archive_path"], request["data_dir"], request["base_dir"],
                          {**request["selection"], "skills": "skip"})
    assert "selection differs" in session.accept_plan(request["generation"], request, fewer)


def test_conflicted_plan_cannot_be_approved(session, target):
    (target[1] / "personas/lumina.json").write_text("{}")
    planned(session)
    assert session.plan["status"] == "needs_owner_conflict_choice"
    assert not session.approve(session.plan["plan_digest"]) and not session.can_execute


def test_execution_is_single_flight_and_consumes_the_approval(session):
    planned(session)
    session.approve(session.plan["plan_digest"])
    ticket = session.begin_execution()
    assert ticket["digest"] == ticket["plan"]["plan_digest"] and session.executing
    with pytest.raises(cp.SessionError):
        session.begin_execution()
    for blocked in (lambda: session.set_archive("x"), lambda: session.set_folders("a", "b"),
                    lambda: session.set_choice("skills", "skip")):
        with pytest.raises(cp.SessionError):
            blocked()
    session.finish_execution("failed")
    assert not session.executing and session.plan is None and session.approved_digest is None
    with pytest.raises(cp.SessionError):
        session.begin_execution()
    assert session.invalidated_because == "failed"


def test_the_ticket_is_a_private_copy(session):
    planned(session)
    session.approve(session.plan["plan_digest"])
    ticket = session.begin_execution()
    ticket["plan"]["selection"]["skills"] = "skip"
    assert session.plan["selection"]["skills"] == "recover"


def test_preview_blockers_and_choice_validation(v2, target):
    s = cp.RestoreSession()
    assert "Inspect" in s.preview_blocker()
    gen = s.set_archive(str(v2))
    s.accept_intake(gen, cp.inspect_archive(str(v2)))
    assert "data folder" in s.preview_blocker()
    s.set_folders(str(target[0]), "")
    assert "install folder" in s.preview_blocker()
    s.set_folders(str(target[0]), str(target[1]))
    assert s.preview_blocker() is None
    for unit in list(s.selection):
        s.set_choice(unit, "skip")
    assert s.preview_blocker() == "Nothing is selected to recover."
    with pytest.raises(cp.SessionError):
        s.request_preview()
    for bad in (("nope", "recover"), ("skills", "maybe")):
        with pytest.raises(cp.SessionError):
            s.set_choice(*bad)


def test_refused_intake_leaves_no_selectable_state(v2):
    from test_agent_backup_restore_02 import _hostile
    _hostile("not_a_zip", v2)
    s = cp.RestoreSession()
    gen = s.set_archive(str(v2))
    assert not s.accept_intake(gen, cp.inspect_archive(str(v2)))
    assert s.intake is None and s.selection == {} and s.preview_blocker()


def test_end_to_end_session_flow_matches_the_engine(session, target):
    data_dir, _base = target
    planned(session)
    assert session.approve(session.plan["plan_digest"])
    ticket = session.begin_execution()
    result = cp.execute_recovery(ticket["plan"], ticket["archive_path"], ticket["digest"])
    session.finish_execution("done")
    assert result["ok"] and (data_dir / "memory/lumina.db").is_file()
    # The same approval cannot run again.
    assert not session.can_execute


def test_Qt_cockpit_guards_are_in_blocking_CI_selection():
    source = (REPO / ".github/workflows/tests.yml").read_text()
    qt_job = source.split("  qt-security-guards:", 1)[1]
    step = qt_job.split("Run Qt-bound isolated recovery cockpit guards (blocking)", 1)[1]
    assert 'LUMINA_REQUIRE_QT: "1"' in qt_job and "tests/test_agent_backup_restore_cockpit_qt.py" in step
    assert "continue-on-error" not in step.split("- name:", 1)[0]
