"""Small controls for maintained selector and receipt accounting."""
import json

import pytest

from scripts.lumina_test.inventory import Entry, compare, read_inventory, update_inventory
from scripts.lumina_test.results import read_report, reconcile

NODE = "tests/test_fixture.py::test_one"
OTHER = "tests/test_fixture.py::test_two"


def test_inventory_drift_is_explicit():
    expected = {NODE: Entry(3, "core", NODE)}
    assert compare(expected, [(OTHER, "core"), (OTHER, "core")]) == {
        "missing": [NODE], "new": [OTHER], "duplicates": [OTHER], "profile_changes": {},
    }


def test_new_ids_keep_existing_assignments_and_overflow_deterministically():
    previous = {NODE: Entry(3, "core", NODE)}
    new_file = "tests/test_other.py::test_one"
    rows = [(new_file, "qt"), (OTHER, "core"), (NODE, "core")]
    forward = update_inventory(previous, rows)
    assert forward == update_inventory(previous, list(reversed(rows)))
    assert forward == {**previous, OTHER: Entry(3, "core", OTHER),
                       new_file: Entry(4, "qt", new_file)}


def test_duplicate_manifest_rows_are_rejected(tmp_path):
    path = tmp_path / "inventory.tsv"
    path.write_text(f"3\tcore\t{NODE}\n4\tqt\t{NODE}\n")
    with pytest.raises(ValueError, match="duplicate inventory"):
        read_inventory(path)


def records(outcome="passed", native=0):
    rows = [{"event": "sessionstart"},
            {"event": "collection", "observed": [[NODE, "core"]], "nodeids": [NODE]}]
    for phase, result in (("setup", "passed"), ("call", outcome), ("teardown", "passed")):
        rows.append({"event": "testreport", "nodeid": NODE, "when": phase,
                     "outcome": result, "wasxfail": False, "longrepr": "skip reason"})
    return rows + [{"event": "sessionfinish", "exitstatus": native}]


def test_exact_passing_session():
    result = reconcile(records(), 0, [NODE])
    assert result["problems"] == []
    assert result["counts"] == {"passed": 1}


def test_another_selector_cannot_get_credit():
    assert reconcile(records(), 0, [OTHER])["problems"]


def test_native_nonzero_rejects_green_report():
    with pytest.raises(ValueError, match="disagrees with native exit"):
        reconcile(records(), 7, [NODE])


def test_assertion_failure_is_preserved():
    result = reconcile(records("failed", 1), 1, [NODE])
    assert result["counts"] == {"failed": 1}
    assert result["problems"] == ["pytest failures"]


def test_unexpected_skip_blocks_and_expected_skip_requires_exact_id_and_reason():
    skipped = records("skipped")
    assert reconcile(skipped, 0, [NODE])["counts"] == {"unexpected_skip": 1}
    assert reconcile(skipped, 0, [NODE])["problems"]
    expected = reconcile(skipped, 0, [NODE], allowed_skips={NODE: "skip reason"})
    assert expected["counts"] == {"expected_skip": 1}
    assert expected["problems"] == []


def test_missing_teardown_is_incomplete():
    assert reconcile(records()[:-2] + records()[-1:], 0, [NODE])["problems"]


def test_truncated_jsonl_is_rejected(tmp_path):
    report = tmp_path / "report.jsonl"
    report.write_text("\n".join(json.dumps(record) for record in records()))
    with pytest.raises(ValueError, match="incomplete"):
        read_report(report)


def test_phases_before_collection_are_invalid():
    rows = records()
    with pytest.raises(ValueError, match="out-of-order"):
        reconcile([rows[0], *rows[2:-1], rows[1], rows[-1]], 0, [NODE])
