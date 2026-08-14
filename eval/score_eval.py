"""
Eval Harness -- Scorer
Scores raw JSONL from run_eval.py against tasks.json's expected criteria.
"""
import json
import re
import argparse
import os

# Known garbled pseudo-tool-call patterns from the documented coherence
# cluster (S36/S38). Not exhaustive -- new garbling shapes need new patterns
# added here as they're spotted in the wild.
_GARBLED_PATTERNS = [
    re.compile(r"<\|?\s*DSML\s*\|?[^>]*>", re.IGNORECASE),
    re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|?\s*tool_calls?\s*\|?>", re.IGNORECASE),
]


def load_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_tasks_by_id(tasks_path):
    with open(tasks_path, "r", encoding="utf-8") as f:
        return {t["id"]: t for t in json.load(f)}


def _normalize_numbers(text: str) -> str:
    """Strip thousands-separator commas between digits so '16,000' matches
    a completion_keyword of '16000'. Only touches commas with a digit on
    both sides -- 'Paris, France' is untouched."""
    return re.sub(r"(?<=\d),(?=\d)", "", text)


def depth_bucket(n_calls: int) -> str:
    if n_calls <= 1:
        return "depth_0-1"
    if n_calls <= 3:
        return "depth_2-3"
    return "depth_4+"


def score_task(record: dict, task: dict) -> dict:
    all_tool_calls = []
    final_response = ""
    for turn in record["turns"]:
        if not turn.get("success"):
            continue
        final_response = turn.get("response", "")
        all_tool_calls.extend(turn.get("tool_calls", []))

    available = record["turns"][-1].get("available_tools", []) if record["turns"] else []
    syntax_clean = not any(p.search(final_response) for p in _GARBLED_PATTERNS)
    hallucinated = [c["name"] for c in all_tool_calls if c["name"] not in available]

    expected = task.get("expected_tools")
    called_names = {c["name"] for c in all_tool_calls}
    selection_correct = None
    if expected is not None:
        if len(expected) == 0:
            if task.get("category") == "no_matching_tool":
                selection_correct = len(hallucinated) == 0
            else:
                selection_correct = len(called_names) == 0
        else:
            selection_correct = bool(called_names & set(expected))

    # discriminating_tool: some tasks test a signal (e.g. content only one of
    # several valid tools actually surfaces) that a genuinely free tool choice
    # might not exercise on a given run. Without this, a run that never called
    # the tool needed to even see the tested content would score completed:
    # False -- indistinguishable in the report from the fix actually
    # regressing. Scoring it here as non-discriminating (completed: None,
    # excluded from completion_rate the same way an empty keywords list
    # already is) keeps the eval realistic -- real persona, free tool choice
    # -- while making a real False mean something whenever it fires.
    discriminating_tool = task.get("discriminating_tool")
    discriminating = discriminating_tool is None or discriminating_tool in called_names

    keywords = task.get("completion_keywords", [])
    normalized_response = _normalize_numbers(final_response).lower()
    if not discriminating:
        completed = None
    else:
        completed = any(kw.lower() in normalized_response for kw in keywords) if keywords else None

    return {
        "task_id": task["id"], "category": task.get("category"),
        "depth_bucket": depth_bucket(len(all_tool_calls)), "n_tool_calls": len(all_tool_calls),
        "syntax_clean": syntax_clean, "hallucinated_tools": hallucinated,
        "selection_correct": selection_correct, "completed": completed,
        "discriminating": discriminating,
        "final_response_preview": final_response[:200],
    }


def _rate(scored, key, pred=None):
    pool = [s for s in scored if pred is None or pred(s)]
    vals = [s[key] for s in pool if s[key] is not None]
    return (sum(1 for v in vals if v) / len(vals)) if vals else None


def summarize(scored: list) -> dict:
    summary = {
        "n_tasks": len(scored),
        "syntax_clean_rate": _rate(scored, "syntax_clean"),
        "selection_correct_rate": _rate(scored, "selection_correct"),
        "completion_rate": _rate(scored, "completed"),
        "hallucination_rate": (sum(1 for s in scored if s["hallucinated_tools"]) / len(scored)) if scored else None,
        "by_depth": {}, "by_category": {},
    }
    for bucket in ("depth_0-1", "depth_2-3", "depth_4+"):
        subset = [s for s in scored if s["depth_bucket"] == bucket]
        if subset:
            summary["by_depth"][bucket] = {
                "n": len(subset),
                "syntax_clean_rate": _rate(scored, "syntax_clean", lambda s: s["depth_bucket"] == bucket),
                "selection_correct_rate": _rate(scored, "selection_correct", lambda s: s["depth_bucket"] == bucket),
                "completion_rate": _rate(scored, "completed", lambda s: s["depth_bucket"] == bucket),
                "hallucination_rate": sum(1 for s in subset if s["hallucinated_tools"]) / len(subset),
            }
    for cat in sorted(set(s["category"] for s in scored if s["category"])):
        subset = [s for s in scored if s["category"] == cat]
        summary["by_category"][cat] = {"n": len(subset),
            "completion_rate": _rate(scored, "completed", lambda s: s["category"] == cat)}
    return summary


def _pct(x):
    return "n/a" if x is None else f"{x*100:.0f}%"


def write_report(summary, scored, out_path):
    lines = ["# Eval Harness Report", "",
              f"**Tasks run:** {summary['n_tasks']}",
              f"**Tool-call syntax clean rate:** {_pct(summary['syntax_clean_rate'])}",
              f"**Correct tool-selection rate:** {_pct(summary['selection_correct_rate'])}",
              f"**Hallucinated-tool rate:** {_pct(summary['hallucination_rate'])}",
              f"**Task completion rate:** {_pct(summary['completion_rate'])}", "",
              "## By depth (tool calls per task)"]
    for bucket, d in summary["by_depth"].items():
        lines.append(f"- **{bucket}** (n={d['n']}): completion {_pct(d['completion_rate'])}, "
                      f"syntax clean {_pct(d['syntax_clean_rate'])}, "
                      f"selection correct {_pct(d['selection_correct_rate'])}, "
                      f"hallucination {_pct(d['hallucination_rate'])}")
    lines += ["", "## By category"]
    for cat, d in summary["by_category"].items():
        lines.append(f"- **{cat}** (n={d['n']}): completion {_pct(d['completion_rate'])}")
    lines += ["", "## Failing tasks"]
    for s in scored:
        failed = (not s["syntax_clean"] or s["selection_correct"] is False
                  or s["hallucinated_tools"] or s["completed"] is False)
        if failed:
            lines.append(f"- **{s['task_id']}** ({s['category']}, {s['n_tool_calls']} calls): "
                          f"syntax_clean={s['syntax_clean']}, selection_correct={s['selection_correct']}, "
                          f"hallucinated={s['hallucinated_tools']}, completed={s['completed']}")
            lines.append(f"  > {s['final_response_preview']}")

    non_discriminating = [s for s in scored if not s.get("discriminating", True)]
    if non_discriminating:
        lines += ["", "## Non-discriminating tasks (discriminating_tool not called this run)",
                   "Not counted as pass or fail -- the code path being tested never fired this run, "
                   "so completed is n/a rather than False. Re-run if you need a real signal for these."]
        for s in non_discriminating:
            lines.append(f"- **{s['task_id']}** ({s['category']}, {s['n_tool_calls']} calls)")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True)
    parser.add_argument("--tasks", default=os.path.join(os.path.dirname(__file__), "tasks.json"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    records = load_jsonl(args.raw)
    tasks_by_id = load_tasks_by_id(args.tasks)
    scored = [score_task(r, tasks_by_id[r["task_id"]]) for r in records if r["task_id"] in tasks_by_id]
    summary = summarize(scored)

    out_path = args.out or args.raw.replace("_raw.jsonl", "_report.md")
    write_report(summary, scored, out_path)
    print(f"[EVAL] Report written to {out_path}")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
