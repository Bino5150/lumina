"""
Eval Harness -- Runner
Replays eval/tasks.json through run_headless_turn(trace=True) against an
ISOLATED data directory -- never the real Skynet memory/chat DB.

CRITICAL: LUMINA_DATA_DIR must be set before any project import happens --
config.py reads it at module load time. This is why it's set at the very
top of this file, before the core.headless import.
"""
import os
import sys

EVAL_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scratch_data")

# MB-23: every run must start from a genuinely empty scratch dir, not an
# assumption that the last run cleaned up after itself -- see
# eval/scratch_dir.py's docstring for the historical contamination bug this
# fixes (a stale prefs.json silently leaked into two cited eval runs before
# anyone noticed). Bare import works here because Python auto-prepends a
# directly-run script's own directory to sys.path -- this file's sibling,
# not a package import, since running `python eval/run_eval.py` directly
# (not `python -m eval.run_eval`) means relative imports aren't available.
from scratch_dir import reset_scratch_dir
reset_scratch_dir(EVAL_DATA_DIR)

os.environ["LUMINA_DATA_DIR"] = EVAL_DATA_DIR

import json
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.headless import run_headless_turn, reset_headless_agent
import config


def load_tasks(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_task(task: dict) -> dict:
    channel_id = f"eval_{task['id']}"
    prompts = task["prompt"] if isinstance(task["prompt"], list) else [task["prompt"]]
    owner = task.get("owner", True)
    tools_profile = task.get("tools_profile")

    turns = []
    for prompt in prompts:
        result = run_headless_turn(
            task=prompt, channel_id=channel_id, owner=owner,
            tools_profile=tools_profile, trace=True,
        )
        turns.append(result)

    reset_headless_agent(channel_id)

    return {"task_id": task["id"], "category": task.get("category"),
            "prompts": prompts, "turns": turns, "backend": config.LLM_BACKEND}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default=os.path.join(os.path.dirname(__file__), "tasks.json"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--backend", default=None, help="Override config.LLM_BACKEND for this run (e.g. 'omniroute'). Default: unset, resolves to 'llamacpp' since the scratch prefs are always empty.")
    args = parser.parse_args()

    tasks = load_tasks(args.tasks)
    if args.backend:
        config.LLM_BACKEND = args.backend.lower()
        config.LLM_BACKEND_URL = None  # let the backend's own default_url win, not the llamacpp-shaped prefs default
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.out or os.path.join(os.path.dirname(__file__), "results", f"{timestamp}_{config.LLM_BACKEND}_raw.jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print(f"[EVAL] Data dir isolated to: {EVAL_DATA_DIR}", flush=True)
    print(f"[EVAL] Running {len(tasks)} tasks -> {out_path}", flush=True)

    with open(out_path, "w", encoding="utf-8") as f:
        for i, task in enumerate(tasks):
            print(f"[EVAL] ({i+1}/{len(tasks)}) {task['id']}...", flush=True)
            t0 = time.time()
            record = run_task(task)
            record["elapsed_s"] = round(time.time() - t0, 1)
            f.write(json.dumps(record) + "\n")
            f.flush()  # crash-safe -- each task's result is durable the moment it finishes

    print(f"[EVAL] Done. Raw results: {out_path}")
    print(f"[EVAL] Score with: python eval/score_eval.py --raw {out_path}")


if __name__ == "__main__":
    main()
