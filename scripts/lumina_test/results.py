"""Reconcile native exit with pytest's collection and every execution phase."""
from collections import Counter
import json

REPORT_LIMIT = 32 * 1024 * 1024


def read_report(path):
    with path.open("rb") as stream:
        raw = stream.read(REPORT_LIMIT + 1)
    if len(raw) > REPORT_LIMIT or not raw.endswith(b"\n"):
        raise ValueError("structured pytest report is oversized or incomplete")
    return [json.loads(line) for line in raw.splitlines()]


def reconcile(records, native, expected=None, *, evidence=False, allowed_skips=None):
    if (len(records) < 3 or any(not isinstance(rec, dict) for rec in records)
            or records[0] != {"event": "sessionstart"}):
        raise ValueError("structured pytest session is incomplete")
    finish = records[-1]
    if (finish.get("event") != "sessionfinish" or type(finish.get("exitstatus")) is not int
            or finish["exitstatus"] != native):
        raise ValueError(f"structured pytest finish disagrees with native exit {native}")
    collection = [rec for rec in records if rec.get("event") == "collection"]
    if len(collection) != 1:
        raise ValueError("missing or repeated structured pytest collection")
    collected = collection[0]
    observed, selected = collected["observed"], collected["nodeids"]
    if (not isinstance(observed, list) or not isinstance(selected, list)
            or any(not isinstance(row, list) or len(row) != 2
                   or not all(isinstance(value, str) for value in row) for row in observed)
            or any(not isinstance(node, str) for node in selected)):
        raise ValueError("invalid structured pytest collection")
    problems = []
    observed_ids = {node for node, _ in observed}
    if len(observed_ids) != len(observed) or len(set(selected)) != len(selected):
        problems.append("duplicate collected or selected IDs")
    if not selected or not set(selected) <= observed_ids:
        problems.append("empty selection or selected ID absent from collection")
    phases = {}
    collection_issues = []
    collection_seen = False
    phase_order = []
    for record in records[1:-1]:
        event = record["event"]
        if event == "collection":
            collection_seen = True
            continue
        if record.get("outcome") not in {"passed", "failed", "skipped"}:
            raise ValueError("invalid structured pytest outcome")
        if not isinstance(record.get("nodeid"), str) or not isinstance(record.get("longrepr"), str):
            raise ValueError("invalid structured pytest node or detail")
        if event == "collectreport" and not collection_seen and record["outcome"] != "passed":
            collection_issues.append(record)
            continue
        if event != "testreport" or not collection_seen or type(record.get("wasxfail")) is not bool:
            raise ValueError(f"invalid or out-of-order structured pytest event {event!r}")
        phases.setdefault(record["nodeid"], []).append(record)
        phase_order.append(record["nodeid"])
    if collection_issues:
        problems.append("pytest collection errors or skips")
    result = {"observed": observed, "selected": selected, "collection_issues": collection_issues,
              "native_exit": native, "problems": problems}
    if expected is None:
        if native != 0 or phases or selected != [node for node, _ in observed]:
            problems.append("collection-only stage did not complete with native zero and zero phases")
        return result
    expected_phases = [node for node in expected for _ in phases.get(node, ())]
    if selected != expected or list(phases) != expected or phase_order != expected_phases:
        problems.append("planned, collected and executed order differ")
    outcomes = {}
    for node, reports in phases.items():
        first = reports[0]
        sequence = [report["when"] for report in reports]
        required = ["setup", "call", "teardown"] if first["outcome"] == "passed" else ["setup", "teardown"]
        if sequence != required:
            problems.append(f"incomplete or repeated phases: {node}")
        labels = {report["outcome"] for report in reports}
        if "failed" in labels:
            outcomes[node] = "failed"
        elif any(report["wasxfail"] for report in reports):
            outcomes[node] = "xfailed" if "skipped" in labels else "xpassed"
        elif "skipped" in labels:
            skip = next(report for report in reports if report["outcome"] == "skipped")
            approved = (allowed_skips or {}).get(node)
            outcomes[node] = "expected_skip" if approved and approved in skip["longrepr"] else "unexpected_skip"
        else:
            outcomes[node] = "passed"
    counts = dict(Counter(outcomes.values()))
    if native != (1 if counts.get("failed") else 0):
        problems.append(f"native pytest exit {native} contradicts completed outcomes")
    if counts.get("failed") and not evidence:
        problems.append("pytest failures")
    if any(counts.get(label) for label in ("unexpected_skip", "xfailed", "xpassed")):
        problems.append("unexpected pytest skip or xfail/xpass")
    result.update(counts=counts, outcomes=outcomes)
    return result
