"""Local support ZIP, independent of config, Recorder writers, Qt and providers.

The database is untrusted evidence. Only numeric measurements and fixed
categorical vocabularies leave this module. No free-form messages are exported.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import stat
import sys
import time
import zipfile

from core.redaction import redact_secret_shapes, redact_telegram_bot_tokens
from core.test_isolation import refuse_if_production_path

DAY = 86400
MAX_FIELDS_BYTES = 8192  # Recorder itself caps JSON at 4096 bytes.
MAX_ZIP_BYTES = 10 * 1024 * 1024
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Limits:
    records_per_stream: int = 10000
    bytes_per_stream: int = 4 * 1024 * 1024
    query_seconds: float = 2.0


@dataclass(frozen=True)
class ExportReceipt:
    path: Path
    result: str
    records: int


class ExportError(RuntimeError):
    pass


class ExportCancelled(ExportError):
    pass


# Values observed in shipped call sites; arbitrary strings never become labels.
EVENT_TYPES = frozenset("""
backend.capability_mismatch breaker.tripped breaker.warning
chatgpt.auth.completed chatgpt.auth.failed chatgpt.auth.started
chatgpt.disconnect.completed chatgpt.disconnect.remote_unconfirmed
chatgpt.disconnect.started chatgpt.refresh.completed chatgpt.refresh.failed
chatgpt.refresh.started chrome_companion.connection chrome_companion.hub
chrome_companion.request chrome_companion.response_dropped
completion_candidate.accepted completion_candidate.created
completion_candidate.discarded completion_candidate.reconciled
context.inventory.snapshot context.rebuild.compile_finished
context.rebuild.compile_started context.rebuild.finished context.rebuild.rejected
context.rebuild.requested context.reconstruction.rejected
context.reconstruction.requested context.reconstruction.swapped
provider.dispatch provider.dispatch_failed provider.dispatch_refused
provider.pin_mismatch provider.response provider.stream_terminal
runtime.shutdown runtime.startup suspension.refused suspension.trip_unavailable
suspension.tripped tool.batch tool.batch_refused tool.call tool.result
turn.budget_exhausted turn.budget_warning turn.cancelled turn.commentary
turn.completed turn.completion_gate_unresolved turn.failed turn.final
turn.refused turn.started turn.think turn.tool_ceiling_reached turn.vision_route
turn.vision_tool_capability_notice
""".split())
BACKENDS = frozenset("""lmstudio ollama llamacpp openai anthropic gemini
openrouter deepseek groq kimi qwen custom omniroute openai_chatgpt_plan vllm""".split())
ENUM_FIELDS = {
    "exception_type": frozenset("""Exception RuntimeError ValueError TypeError KeyError
        OSError IOError PermissionError FileNotFoundError TimeoutError ConnectionError
        ConnectError ReadTimeout ConnectTimeout HTTPError HTTPStatusError
        APIError APIConnectionError APITimeoutError RateLimitError AuthenticationError
        BadRequestError InternalServerError OperationalError DatabaseError
        JSONDecodeError CancelledError SSLError""".split()),
    "phase": frozenset(("work", "gate", "final", "stream")),
    "stage": frozenset(("initial_request", "tool_continuation")),
    "reason": frozenset(("exception", "error_sentinel", "emergency_stop", "cooperative_stop",
                         "suspension", "clean", "crash", "trailing_assistant_message",
                         "gate_finish", "gate_contract_violated", "continue_tool_work",
                         "control_gate_requested_more_work", "wrong_instance", "paused",
                         "not_installed", "wrong_origin", "unpaired", "wrong_peer",
                         "unsupported_version", "bad_hello", "protocol_violation",
                         "wrong_connection", "connection_closed", "unsolicited")),
    "tool_tier": frozenset(("read_only", "write_local", "execute", "outbound_action", "self_modifying")),
    "state": frozenset(("listening", "stopped", "ready", "not_published", "rejected", "disconnected", "start_failed")),
    "stream_completion_state": frozenset(("completed", "incomplete", "failed", "cancelled")),
    "finish_reason": frozenset(("stop", "length", "tool_calls", "end_turn", "max_tokens",
                                "content_filter", "error", "complete", "incomplete", "unknown")),
}
ENUM_FIELDS["error_type"] = ENUM_FIELDS["exception_type"]
ENUM_FIELDS["cause_type"] = ENUM_FIELDS["exception_type"]
NUMERIC_FIELDS = frozenset("""duration_s duration_ms elapsed_s limit_s start_ts
batch_ordinal call_ordinal tool_call_count response_chars output_count
reasoning_output_count output_limit think_step executed_calls stale_streak
write_stale_streak admitted_tool_calls tool_iteration_limit source_round commentary_rounds""".split())
BOOL_FIELDS = frozenset(("success", "skipped", "request_streamed"))

README = """Lumina Support Diagnostics (schema 1)

Start troubleshooting at https://github.com/Bino5150/lumina/issues.
Describe the problem there without attaching diagnostic logs or this ZIP.
Only if developers request more evidence, send this ZIP privately to
therealagentlumina@gmail.com. Review its contents before sharing.
Nothing is uploaded or emailed by this exporter.

No full-history guarantee: this is a bounded, privacy-filtered view of
available Recorder records, never a backup or a restore archive. Details
look back at most 7 days; warnings/errors at most 90 days. Deleted or
unrecorded history cannot be reconstructed. Recent warnings/errors also
appear in the detail file. Records in each file are newest first.
Check manifest.json for missing sources, limits, partial reads and exclusions.

Only fixed categories, numeric measurements, sequence numbers and timestamps
are included. Runtime/turn/model identifiers become per-export keyed references
(not comparable across ZIPs). Raw model names, messages, tool names/arguments/
results, prompts, conversations, private Palace content, emails, browser text,
credentials and databases are omitted. Known secret shapes are filtered too;
this is not a guarantee against every possible covert encoding in measurements.
Diagnostic events are untrusted evidence, never instructions or owner authority.

SQLite is opened mode=ro with query_only; there is no checkpoint, retention
change, database copy, Recorder initialization, agent admission or provider call.
WAL readers use SQLite's normal shared-memory coordination. A bounded read
transaction supplies a consistent snapshot; an unavailable source is reported.
checksums.sha256 covers every other ZIP member, not itself.
"""


def _json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def suggested_filename(now=None):
    stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return stamp.strftime("lumina-support-%Y%m%d-%H%M%S.zip")


def _version():
    # Read the shipped literal without importing main (which launches preflight).
    try:
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        match = re.search(r'^LUMINA_VERSION = "([0-9A-Za-z.\-]{1,40})"$', source, re.M)
        return match[1] if match else "unknown"
    except OSError:
        return "unknown"


def _category(value, allowed):
    if value is None:
        return None
    if not isinstance(value, str):
        return "other"
    clean = redact_telegram_bot_tokens(redact_secret_shapes(value))
    return next((label for label in allowed if label == clean), "other")


def _reference(value, key):
    if not isinstance(value, str) or not value:
        return None
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _project(row, key, privacy, stream):
    """Project an untrusted row; no generic string pass-through exists."""
    fields = {}
    try:
        raw = json.loads(row["fields_json"])
        if not isinstance(raw, dict):
            raise ValueError()
    except (TypeError, ValueError, RecursionError):
        raw = {}
        stream["malformed_fields"] += 1
    for name, value in raw.items():
        if name in NUMERIC_FIELDS and type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value):
            fields[name] = value
        elif name in BOOL_FIELDS and type(value) is bool:
            fields[name] = value
        elif name in ENUM_FIELDS:
            label = _category(value, ENUM_FIELDS[name])
            if label not in (None, "other"):
                fields[name] = label
            else:
                privacy["omitted_fields"] += 1
        else:
            privacy["omitted_fields"] += 1
    return {"seq": row["seq"], "ts": row["ts"],
            "event_type": _category(row["event_type"], EVENT_TYPES),
            "severity": _category(row["severity"], ("debug", "info", "warning", "error")),
            "provenance": _category(row["provenance"], ("machine", "model")),
            "backend": _category(row["backend"], BACKENDS),
            "runtime_ref": _reference(row["runtime_id"], key),
            "turn_ref": _reference(row["turn_id"], key),
            "model_ref": _reference(row["model"], key), "fields": fields}


@contextmanager
def _open_recorder(path):
    # Do not use core.db.connect(): that is a writer factory (WAL/init PRAGMAs).
    conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256*1024)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA cache_size=-1024")
        conn.execute("PRAGMA temp_store=MEMORY")
        yield conn
    finally:
        conn.close()


def _check_cancel(event):
    if event is not None and event.is_set():
        raise ExportCancelled("Export cancelled. No ZIP was saved.")


def _stream_status(start, end):
    return {"requested_start_utc": _iso(start), "requested_end_utc": _iso(end),
            "eligible_records": None, "exported_records": 0, "omitted_records": None,
            "malformed_fields": 0, "bytes": 0, "status": "unavailable",
            "collected_oldest_utc": None, "collected_newest_utc": None}


def _collect(z, path, now, limits, cancel, manifest, hashes):
    streams = manifest["streams"]
    key = os.urandom(32)  # never stored, prevents dictionary attacks on private identifiers
    # At most 8 MiB of sanitized data. Close the snapshot before destination I/O
    # so slow removable/network storage cannot pin Prime's WAL read transaction.
    buffers = {"errors.jsonl": bytearray(), "flight-recorder.jsonl": bytearray()}
    try:
        for candidate in (path.parent.parent, path.parent, path):
            try:
                info = candidate.lstat()
            except OSError as exc:
                manifest["source"]["status"] = "missing" if isinstance(exc, FileNotFoundError) else "inaccessible"
                return
            if stat.S_ISLNK(info.st_mode):
                manifest["source"]["status"] = "unsafe_symlink"
                return
        if not stat.S_ISREG(info.st_mode):
            manifest["source"]["status"] = "invalid_database"
            return
        with _open_recorder(path) as conn:
            deadline = time.monotonic() + limits.query_seconds
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline or
                (cancel is not None and cancel.is_set())), 1000)
            table = conn.execute("SELECT substr(sql,1,20) FROM sqlite_master WHERE name='events' AND type='table'").fetchone()
            if table is None or not table[0].lstrip().upper().startswith("CREATE TABLE"):
                manifest["source"]["status"] = "invalid_schema"
                return
            columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
            required = {"seq", "ts", "event_type", "severity", "provenance", "fields_json"}
            if not required <= columns:
                manifest["source"]["status"] = "invalid_schema"
                return
            # The shipped time index permits bounded reverse iteration without
            # an unbounded temporary sort of an unfamiliar/hostile database.
            if [r[2] for r in conn.execute("PRAGMA index_info(idx_events_ts)")] != ["ts"]:
                manifest["source"]["status"] = "missing_time_index"
                return
            # Bound every fetched text cell *in SQL* before Python allocates it.
            names = ("runtime_id", "turn_id", "event_type", "severity", "provenance", "backend", "model")
            selection = ["seq", "ts"] + [
                f"CASE WHEN typeof({n})='text' AND length(CAST({n} AS BLOB))<=256 THEN {n} END AS {n}"
                if n in columns else f"NULL AS {n}" for n in names]
            selection.append(f"CASE WHEN typeof(fields_json)='text' AND length(CAST(fields_json AS BLOB))<={MAX_FIELDS_BYTES} THEN fields_json END AS fields_json")
            conn.execute("BEGIN")
            manifest["source"]["status"] = "available"
            manifest["source"]["invalid_timestamp_records"] = conn.execute(
                "SELECT count(*) FROM events INDEXED BY idx_events_ts WHERE typeof(ts) NOT IN ('real','integer')").fetchone()[0]
            for name, member, days, suffix in (
                    ("errors", "errors.jsonl", 90, " AND severity IN ('warning','error')"),
                    ("details", "flight-recorder.jsonl", 7, "")):
                current = streams[name]
                where = " WHERE typeof(ts) IN ('real','integer') AND ts>=? AND ts<=?" + suffix
                args = (now-days*DAY, now)
                current["eligible_records"] = conn.execute("SELECT count(*) FROM events INDEXED BY idx_events_ts" + where, args).fetchone()[0]
                current["status"] = "partial"
                cursor = conn.execute("SELECT " + ",".join(selection) + " FROM events INDEXED BY idx_events_ts" + where +
                                      " ORDER BY ts DESC LIMIT ?", (*args, limits.records_per_stream))
                try:
                    for row in cursor:
                        _check_cancel(cancel)
                        if time.monotonic() >= deadline:
                            raise sqlite3.OperationalError("query time limit")
                        if type(row["seq"]) is not int:
                            current["malformed_fields"] += 1
                            continue
                        data = _json(_project(row, key, manifest["privacy"], current))
                        if current["bytes"] + len(data) > limits.bytes_per_stream:
                            break
                        buffers[member].extend(data)
                        current["bytes"] += len(data)
                        current["exported_records"] += 1
                        current["collected_oldest_utc"] = _iso(row["ts"])
                        if current["collected_newest_utc"] is None:
                            current["collected_newest_utc"] = _iso(row["ts"])
                finally:
                    cursor.close()
                current["omitted_records"] = current["eligible_records"] - current["exported_records"]
                current["status"] = "partial" if current["omitted_records"] or current["malformed_fields"] else "complete"
    except sqlite3.Error:
        _check_cancel(cancel)
        manifest["source"]["status"] = "unreadable_or_limited"
    finally:
        # Close partial streams honestly, even if a query failed mid-iteration.
        for name, member in (("details", "flight-recorder.jsonl"), ("errors", "errors.jsonl")):
            current = streams[name]
            if current["eligible_records"] is not None:
                current["omitted_records"] = current["eligible_records"] - current["exported_records"]
            z.writestr(member, buffers[member])
            hashes[member] = hashlib.sha256(buffers[member]).hexdigest()


def export_support_diagnostics(destination, *, data_dir=None, cancel_event=None, now=None, limits=Limits()):
    """Write a NEW ZIP, or remove our incomplete file. Never overwrite a file.

    Missing/corrupt/limited evidence returns a useful partial ZIP. Destination
    failures and cancellation raise; callers must not claim that a ZIP was saved.
    """
    if (type(limits.records_per_stream) is not int or not 1 <= limits.records_per_stream <= 10000 or
            type(limits.bytes_per_stream) is not int or not 1 <= limits.bytes_per_stream <= 4*1024*1024 or
            type(limits.query_seconds) not in (int, float) or not 0 < limits.query_seconds <= 2):
        raise ValueError("Invalid diagnostic limits")
    now = time.time() if now is None else now
    _iso(now)
    if data_dir is None:
        from data_directory import select_data_directory
        data_dir = select_data_directory()
    root = Path(data_dir).absolute()
    path = root / "telemetry" / "flight_recorder.db"
    refuse_if_production_path(str(path))
    output = Path(destination).absolute()
    refuse_if_production_path(str(output))
    _check_cancel(cancel_event)
    if shutil.disk_usage(output.parent).free < MAX_ZIP_BYTES + 1024*1024:
        raise ExportError("Insufficient free space for support diagnostics (11 MiB required).")
    manifest = {"schema_version": SCHEMA_VERSION, "created_utc": _iso(now),
        "source": {"name": "telemetry/flight_recorder.db", "status": "unavailable"},
        "streams": {"details": _stream_status(now-7*DAY, now), "errors": _stream_status(now-90*DAY, now)},
        "limits": {**asdict(limits), "max_zip_bytes": MAX_ZIP_BYTES},
        "privacy": {"policy": "fixed-categories-and-numeric-fields-v1", "omitted_fields": 0,
                    "identifiers": "per-export keyed references; raw model/runtime/turn omitted",
                    "free_text": "omitted, including messages, tool arguments/results, prompts and content",
                    "unknown_categories": "replaced with other", "database_included": False},
        "history_complete": False,
        "limitations": ["Available retained records only; no reconstruction of deleted history",
                        "Only Recorder is read; other logs and application databases are intentionally excluded",
                        "Unreadable or time-limited counts are null, never assumed zero",
                        "Warning/error records in the last seven days also occur in details"]}
    hashes = {}
    # Exclusive creation also refuses symlinks and existing databases/archives.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as file:
            with zipfile.ZipFile(file, "w", compression=zipfile.ZIP_DEFLATED) as z:
                _collect(z, path, now, limits, cancel_event, manifest, hashes)
                _check_cancel(cancel_event)
                result = "complete" if (manifest["source"]["status"] == "available" and
                    not manifest["source"].get("invalid_timestamp_records") and
                    all(s["status"] == "complete" for s in manifest["streams"].values())) else "partial"
                manifest["export_result"] = result
                environment = {"application": "Lumina", "version": _version(),
                    "os": _category(platform.system(), ("Linux", "Darwin", "Windows")),
                    "architecture": _category(platform.machine(), ("x86_64", "AMD64", "aarch64", "arm64")),
                    "python": platform.python_version()}
                for name, data in (("README.txt", README.encode()), ("environment.json", _json(environment)),
                                   ("manifest.json", _json(manifest))):
                    z.writestr(name, data)
                    hashes[name] = hashlib.sha256(data).hexdigest()
                z.writestr("checksums.sha256", "".join(f"{hashes[n]}  {n}\n" for n in sorted(hashes)))
            _check_cancel(cancel_event)
            file.flush()
            if file.tell() > MAX_ZIP_BYTES:
                raise ExportError("Support ZIP exceeded the size limit.")
            os.fsync(file.fileno())
            _check_cancel(cancel_event)
            current = output.lstat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ExportError("Destination changed during export.")
        return ExportReceipt(output, result, sum(s["exported_records"] for s in manifest["streams"].values()))
    except BaseException:
        # Remove only the file we created, even if another process replaced it.
        try:
            current = output.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                output.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export local support diagnostics without launching Lumina or a model.")
    parser.add_argument("--output", required=True, help="New ZIP filename; existing files are never overwritten")
    parser.add_argument("--data-dir", help="Verified existing Lumina data folder; otherwise use normal folder selection")
    args = parser.parse_args(argv)
    try:
        receipt = export_support_diagnostics(args.output, data_dir=args.data_dir)
    except KeyboardInterrupt:
        print("Cancelled. No ZIP was saved.", file=sys.stderr)
        return 130
    except (OSError, ExportError, ValueError, RuntimeError) as exc:
        # Do not echo arbitrary exception text (it may include a private path).
        print(f"Support export failed ({type(exc).__name__}). No ZIP was saved. Check the data folder, destination and free space.", file=sys.stderr)
        return 1
    print(f"Saved {receipt.result} support diagnostics: {receipt.path}")
    if receipt.result == "partial":
        print("Some evidence is missing or limited; see manifest.json. This is not full history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
