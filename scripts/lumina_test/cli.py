"""Plan, dispatch and report contained Lumina pytest runs."""
import argparse
from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile

import boundary
from boundary_child import atomic_json
from inventory import PROFILES, compare, groups, inventory_text, read_inventory, update_inventory
from results import REPORT_LIMIT, read_report, reconcile
from source import project, snapshot, stage_relays

def arguments():
    parser = argparse.ArgumentParser(prog="lumina-test", description=__doc__)
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    parser.add_argument("--results", type=Path, default=state / "lumina-test")
    parser.add_argument("--relay-repo", type=Path, help="local Git repository holding both pinned Wire generations")
    parser.add_argument("--profile", action="append", choices=PROFILES,
                        help="repeat to combine core, Qt or Wire; evidence runs alone")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="collect and reconcile all IDs without execution")
    listing.add_argument("--write-inventory", type=Path, help="write a proposed inventory to a new file")
    commands.add_parser("smoke", help="run a small exact selection")
    shard = commands.add_parser("shard", help="run one maintained shard")
    shard.add_argument("number", type=int)
    commands.add_parser("full", help="run applicable maintained shards in order")
    commands.add_parser("report", help="display the latest receipt and check source identity")
    return parser.parse_args()


def harvest(src, dst, limit):
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError(f"unsafe or oversized workload artifact: {src.name}")
        with dst.open("xb") as output:
            shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
    return True


def execute_stage(root, saved, projected, relays, env, rules, entries, selected, runtime, receipt,
                  *, collection_targets=None):
    saved.mkdir()
    receipt["artifacts"] = saved.name
    collecting = selected is None
    require_qt = (collection_targets is None if collecting else
                  any(row.profile != "core" for row in selected))
    if collecting:
        targets = ["tests"] if collection_targets is None else collection_targets
        if not targets:
            raise ValueError("no test files for inventory discovery")
        expected, selection = None, None
    else:
        targets = sorted({row.file for row in selected})
        expected = [row.node for row in selected]
        selection = {"inventory": {node: row.profile for node, row in entries.items() if row.file in targets},
                     "execute": expected}
    receipt["planned_ids"] = expected
    evidence = not collecting and selected[0].profile == "evidence"
    scratch = Path(tempfile.mkdtemp(prefix="s-", dir=root))
    try:
        controls = scratch / "controls"
        controls.mkdir()
        output = scratch / "out"
        output.mkdir()
        for name in ("boundary_child.py", "pytest_entry.py"):
            shutil.copyfile(projected / "scripts/lumina_test" / name, controls / name)
        atomic_json(controls / "pytest.json", {"targets": targets, "selection": selection, "rules": rules,
                                               "collect": collecting, "require_qt": require_qt})
        settings = {**runtime, "project": str(projected), "controls": str(controls), "output": str(output),
                    "relays": str(relays) if relays else None, "require_qt": require_qt}
        try:
            smoke = receipt["shard"] == "smoke"
            memory_mib = 768 if smoke else 1024 if collecting else 2000
            timeout = 120 if collecting or smoke else 600
            native = boundary.run(env, scratch, settings, saved / "pytest.log", receipt,
                                  timeout=timeout, memory=memory_mib * 1024**2)
        finally:
            if receipt.get("cleanup", {}).get("verified_empty"):
                harvest(output / "report.jsonl", saved / "report.jsonl", REPORT_LIMIT)
                harvest(output / "namespace.json", saved / "namespace.json", 64 * 1024)
        if not (saved / "namespace.json").is_file():
            raise ValueError("namespace proof missing; see pytest.log")
        result = reconcile(read_report(saved / "report.jsonl"), native, expected,
                           evidence=evidence, allowed_skips=rules["allowed_skips"])
        if not collecting:
            covered = {node: row for node, row in entries.items() if row.file in targets}
            result["drift"] = compare(covered, result["observed"])
            if any(result["drift"].values()):
                result["problems"].append("file inventory differs from reviewed selectors")
        receipt["result"] = result
        return result
    finally:
        if "unit" in receipt and not receipt.get("cleanup", {}).get("verified_empty"):
            receipt["scratch_retained"] = str(scratch)
        else:
            shutil.rmtree(scratch)
            receipt["scratch_removed"] = not scratch.exists()


def print_receipt(path, data):
    print(f"{data['status']} · {data['command']} · {path}")
    if data.get("error"):
        print(data["error"])
    if data["command"] == "full" and data.get("excluded_profiles"):
        excluded = ", ".join(f"{name}={count}" for name, count in data["excluded_profiles"].items())
        print(f"Outside the requested profiles: {excluded}")
    if data.get("planned_shards"):
        ran = [stage["shard"] for stage in data["stages"] if stage["shard"] != "inventory"]
        missing = [shard for shard in data["planned_shards"] if shard not in ran]
        if missing:
            print("Unattempted shards:", ", ".join(missing))
    for stage in data["stages"]:
        result = stage.get("result", {})
        counts = ", ".join(f"{name}={count}" for name, count in sorted(result.get("counts", {}).items()))
        peak = stage.get("kernel", {}).get("peak_bytes")
        native = stage.get("native_exit")
        duration = stage.get("duration_seconds")
        clean = stage.get("cleanup", {}).get("verified_empty")
        print(f"{stage['shard']}: native={native}  time={duration}s  peak={peak} bytes  cleanup={clean}  {counts}")
        for reason in result.get("problems", ()):
            print(f"  {reason}")
    drift = data.get("inventory_drift", {})
    for kind, items in drift.items():
        if items:
            print(f"{kind}: {len(items)} (exact IDs in receipt)")
            for node in list(items)[:8]:
                print(f"  {node}")


def latest(results, repo):
    candidates = sorted(results.glob("run-*/receipt.json"))
    if not candidates:
        raise ValueError(f"no receipts under {results}")
    path = candidates[-1]
    data = json.loads(path.read_text())
    print_receipt(path, data)
    if data.get("source") != snapshot(repo):
        print("STALE: the current source differs from the receipt")
        return 1
    return 0 if data["status"] in ("PASS", "INVENTORY_MATCH") else 1


def core_discovery(projected, entries):
    """Include new modules and all IDs of files that contribute core tests."""
    known = {row.file for row in entries.values()}
    existing = {file.relative_to(projected).as_posix() for file in (projected / "tests").rglob("test_*.py")}
    files = {row.file for row in entries.values() if row.profile == "core"} | (existing - known)
    return sorted(files & existing), {node: row for node, row in entries.items() if row.file in files}


def run(args, repo, profiles):
    args.results.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = Path(tempfile.mkdtemp(prefix=f"run-{stamp}-", dir=args.results))
    path = directory / "receipt.json"
    data = {"schema": 1, "command": args.command, "profiles": sorted(profiles),
            "status": "INCOMPLETE", "stages": [], "repo": str(repo)}
    atomic_json(path, data)
    root = None
    try:
        with boundary.admission_lock() as env:
            root = Path(tempfile.mkdtemp(prefix="lt-", dir="/tmp"))
            projected = root / "source"
            data["source"] = project(repo, projected)
            data["profile_counts"] = {}
            data["started_utc"] = stamp
            runtime = boundary.runtime_settings()
            data["runtime"] = runtime
            relays = None
            if "wire" in profiles:
                relays = root / "relays"
                data["wire_source"] = stage_relays(args.relay_repo.resolve(strict=True), relays)
            entries = read_inventory(projected / "scripts/lumina_test/inventory.tsv")
            rules = json.loads((projected / "scripts/lumina_test/profiles.json").read_text())
            data["profile_counts"] = dict(Counter(row.profile for row in entries.values()))
            data["excluded_profiles"] = {name: count for name, count in data["profile_counts"].items()
                                         if name not in profiles and args.command != "list"}
            if args.command in ("list", "full"):
                stage = {"shard": "inventory"}
                data["stages"].append(stage)
                targets, inventory = None, entries
                if args.command == "full" and profiles == {"core"}:
                    targets, inventory = core_discovery(projected, entries)
                outcome = execute_stage(root, directory / "inventory", projected, relays,
                                        env, rules, inventory, None, runtime, stage,
                                        collection_targets=targets)
                data["inventory_drift"] = compare(inventory, outcome["observed"])
                if outcome["problems"]:
                    raise ValueError("collection did not qualify; see the inventory stage")
                if any(data["inventory_drift"].values()):
                    if args.command == "list" and args.write_inventory:
                        candidate = update_inventory(entries, outcome["observed"])
                        with args.write_inventory.open("x", encoding="utf-8") as stream:
                            stream.write(inventory_text(candidate))
                        data["proposed_inventory"] = str(args.write_inventory)
                    raise ValueError("maintained inventory drift; review the exact ID changes")
                data["status"] = "INVENTORY_MATCH"
                atomic_json(path, data)
            if args.command != "list":
                batches = groups(entries, args.command, getattr(args, "number", None), profiles)
                data["planned_shards"] = [f"shard-{number:03d}" if number else "smoke" for number in batches]
                data["status"] = "INCOMPLETE"
                atomic_json(path, data)
                all_clean = True
                for label, selected in zip(data["planned_shards"], batches.values(), strict=True):
                    stage = {"shard": label}
                    data["stages"].append(stage)
                    outcome = execute_stage(root, directory / label, projected, relays, env, rules,
                                            entries, selected, runtime, stage)
                    all_clean = all_clean and not outcome["problems"]
                    atomic_json(path, data)
                    if any(outcome["drift"].values()) or outcome["collection_issues"]:
                        raise ValueError(f"{label} inventory did not qualify")
                data["status"] = ("EVIDENCE_RECORDED" if profiles == {"evidence"} else "PASS") if all_clean else "FAIL"
            if data["source"] != snapshot(repo):
                raise ValueError("working source changed during this run; receipt is stale")
    except KeyboardInterrupt:
        data.update(status="INTERRUPTED", error="test command interrupted")
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        data.update(status="FAIL", error=str(exc) or type(exc).__name__)
    finally:
        if root is not None:
            if any("scratch_retained" in stage for stage in data["stages"]):
                data["scratch_retained"] = str(root)
            else:
                try:
                    shutil.rmtree(root)
                    data["scratch_removed"] = not root.exists()
                except OSError as exc:
                    data.update(status="FAIL", scratch_retained=str(root))
                    data["scratch_error"] = str(exc)
        atomic_json(path, data)
    print_receipt(path, data)
    return 130 if data["status"] == "INTERRUPTED" else int(data["status"] not in ("PASS", "INVENTORY_MATCH"))


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(repo):
    args = arguments()
    defaults = {"core"} if args.command == "smoke" else {"core", "qt"}
    if args.relay_repo:
        defaults.add("wire")
    profiles = set(args.profile or defaults)
    if args.command == "list":
        # Discovery describes all four profiles even without a local relay.
        profiles = set()
    try:
        if args.command == "report":
            return latest(args.results, repo)
        if "evidence" in profiles and len(profiles) != 1:
            raise ValueError("the evidence profile runs on its own")
        if "wire" in profiles and not args.relay_repo:
            raise ValueError("the Wire profile requires --relay-repo with both pinned generations")
        signal.signal(signal.SIGTERM, interrupt)
        return run(args, repo, profiles)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"lumina-test: {exc}")
        return 1
