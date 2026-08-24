"""Five model adapters for the persistent-process manager.

Launch, input, and stop are active-control tools; read and list are observation
tools.  The registrar is integrated through core/agent.py.  Bound Project state
provides the default for an omitted launch cwd, while channel_id records launch
provenance only.  Process lifecycle, emergency authority, OS tree control,
stdin serialization, and registry ownership remain manager/kernel concerns.
"""

import json
import os

import config
from core import process_manager
from tools.guardrails import check_model_command


MAX_CHARS_PER_STREAM = 65536


def _live_budget() -> int:
    try:
        return max(0, int(config.TOOL_RESULT_MAX_CHARS))
    except (TypeError, ValueError):
        return 0


def _encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _tiny_fallback(budget: int) -> str:
    """Smallest structurally-valid truncation result that fits."""
    marker = _encode({"output_truncated": True})
    if len(marker) <= budget:
        return marker
    if budget >= 2:
        return "{}"
    return ""


def _bounded_error(code: str, message: str) -> str:
    budget = _live_budget()
    full = _encode({"error": code, "message": message})
    if len(full) <= budget:
        return full
    compact = _encode({"error": code})
    if len(compact) <= budget:
        return compact
    return _tiny_fallback(budget)


def _bounded_payload(payload: dict) -> str:
    budget = _live_budget()
    encoded = _encode(payload)
    if len(encoded) <= budget:
        return encoded
    return _tiny_fallback(budget)


def _manager_error(exc: Exception) -> str:
    """Translate manager failures without leaking raw OS/path details."""
    translations = (
        (process_manager.InvalidLaunchRequest,
         ("invalid_launch", "command or working directory is invalid")),
        (process_manager.CommandBlocked,
         ("command_blocked", "command was rejected by the safety guardrail")),
        (process_manager.LiveProcessLimitReached,
         ("process_limit_reached", "managed process limit has been reached")),
        (process_manager.LaunchRefused,
         ("launch_refused", "emergency execution authority is unavailable")),
        (process_manager.InvalidProcessInput,
         ("invalid_process_input", "process input arguments are invalid")),
        (process_manager.ProcessNotFound,
         ("process_not_found", "managed process was not found")),
        (process_manager.ProcessNotRunning,
         ("process_not_running", "managed process is not running")),
        (process_manager.ProcessInputClosed,
         ("process_input_closed", "managed process stdin is closed")),
        (process_manager.ProcessInputRefused,
         ("process_input_refused", "process input authority is stale")),
        (process_manager.ProcessInputError,
         ("process_input_failed", "managed process stdin operation failed")),
    )
    for error_type, (code, message) in translations:
        if isinstance(exc, error_type):
            return _bounded_error(code, message)
    return _bounded_error("process_operation_failed", "managed process operation failed")


def _validate_cursor(name: str, value, stream_end: int):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return _bounded_error(
            f"invalid_{name}", f"{name} must be a non-negative integer"
        )
    if value > stream_end:
        return _bounded_error(
            f"future_{name}", f"{name} exceeds the current stream end"
        )
    return None


def _stream_available(stream: dict, requested_cursor: int, requested_cap: int):
    start = stream["start_offset"]
    end = stream["end_offset"]
    effective = max(requested_cursor, start)
    retained_index = effective - start
    available = stream["text"][retained_index:]
    return {
        "requested_cursor": requested_cursor,
        "effective_cursor": effective,
        "buffer_start": start,
        "buffer_end": end,
        "history_evicted": requested_cursor < start,
        "desired_text": available[:requested_cap],
    }


def _render_read(snapshot: dict, stdout_info: dict, stderr_info: dict,
                 stdout_chars: int, stderr_chars: int) -> str:
    def _stream_payload(info, count):
        text = info["desired_text"][:count]
        next_cursor = info["effective_cursor"] + len(text)
        return {
            "requested_cursor": info["requested_cursor"],
            "effective_cursor": info["effective_cursor"],
            "next_cursor": next_cursor,
            "buffer_start": info["buffer_start"],
            "buffer_end": info["buffer_end"],
            "history_evicted": info["history_evicted"],
            "text": text,
            "output_truncated": next_cursor < info["buffer_end"],
        }

    return _encode({
        "process_id": snapshot["process_id"],
        "status": snapshot["status"],
        "returncode": snapshot["returncode"],
        "cwd": snapshot["cwd"],
        "stdout": _stream_payload(stdout_info, stdout_chars),
        "stderr": _stream_payload(stderr_info, stderr_chars),
    })


def _fit_read_result(snapshot: dict, stdout_info: dict, stderr_info: dict) -> str:
    """Fit complete stream metadata plus text inside the live result ceiling.

    Allocation is deterministic and fair-first: binary-search the largest
    equal character quota both streams can receive, then give any residual
    capacity to stdout followed by stderr.  Thus either stream can use spare
    room when its peer is short, while two non-empty streams share the primary
    allocation instead of stderr being permanently starved by stdout-first
    processing.  Every fit check encodes the real JSON, so escaping overhead is
    included and next_cursor always reflects text actually present.
    """
    budget = _live_budget()
    stdout_len = len(stdout_info["desired_text"])
    stderr_len = len(stderr_info["desired_text"])

    base = _render_read(snapshot, stdout_info, stderr_info, 0, 0)
    if len(base) > budget:
        return _tiny_fallback(budget)

    def _fits(out_count, err_count):
        return len(_render_read(
            snapshot, stdout_info, stderr_info, out_count, err_count
        )) <= budget

    low, high = 0, max(stdout_len, stderr_len)
    while low < high:
        mid = (low + high + 1) // 2
        if _fits(min(stdout_len, mid), min(stderr_len, mid)):
            low = mid
        else:
            high = mid - 1
    stdout_count = min(stdout_len, low)
    stderr_count = min(stderr_len, low)

    for which in ("stdout", "stderr"):
        current = stdout_count if which == "stdout" else stderr_count
        maximum = stdout_len if which == "stdout" else stderr_len
        low_extra, high_extra = current, maximum
        while low_extra < high_extra:
            mid = (low_extra + high_extra + 1) // 2
            out_count = mid if which == "stdout" else stdout_count
            err_count = mid if which == "stderr" else stderr_count
            if _fits(out_count, err_count):
                low_extra = mid
            else:
                high_extra = mid - 1
        if which == "stdout":
            stdout_count = low_extra
        else:
            stderr_count = low_extra

    return _render_read(
        snapshot, stdout_info, stderr_info, stdout_count, stderr_count
    )


def register_process_tools(registry, project_state=None, channel_id=""):
    """Register the five persistent-process model adapters.

    project_state supplies an omitted-cwd Project default; channel_id is bound
    only as launch provenance.  Lifecycle and control ownership stay behind the
    process_manager public boundary.
    """
    _bound_dependencies = (project_state, channel_id)

    def start_process(command: str, cwd: str = None) -> str:
        if not isinstance(command, str) or not command.strip():
            return _bounded_error(
                "invalid_launch", "command must be a non-empty string"
            )
        if cwd is not None and not isinstance(cwd, str):
            return _bounded_error(
                "invalid_launch", "cwd must be a string or null"
            )

        block_reason = check_model_command(command)
        if block_reason:
            return _bounded_error(
                "command_blocked", "command was rejected by the safety guardrail"
            )

        if cwd is not None:
            # Explicit caller intent always wins. Relative paths resolve from
            # Lumina's own current process cwd -- never from active Project.
            concrete_cwd = os.path.abspath(os.path.expanduser(cwd))
        else:
            active = project_state.get() if project_state is not None else None
            concrete_cwd = (
                active.root if active is not None else os.path.expanduser("~")
            )

        try:
            process_id = process_manager.launch(
                command, cwd=concrete_cwd, channel_id=channel_id
            )
        except Exception as exc:
            return _manager_error(exc)

        snapshot = process_manager.get_job_snapshot(process_id)
        return _bounded_payload({
            "process_id": process_id,
            "status": snapshot["status"] if snapshot is not None else "snapshot_unavailable",
            "cwd": concrete_cwd,
        })

    def read_process(process_id: str, stdout_cursor: int = 0,
                     stderr_cursor: int = 0,
                     max_chars_per_stream: int = 4000) -> str:
        _ = _bound_dependencies  # bound, never model-visible
        if not isinstance(process_id, str) or not process_id:
            return _bounded_error(
                "invalid_process_id", "process_id must be a non-empty string"
            )
        if (isinstance(max_chars_per_stream, bool)
                or not isinstance(max_chars_per_stream, int)
                or not (1 <= max_chars_per_stream <= MAX_CHARS_PER_STREAM)):
            return _bounded_error(
                "invalid_max_chars_per_stream",
                f"max_chars_per_stream must be an integer between 1 and {MAX_CHARS_PER_STREAM}",
            )

        snapshot = process_manager.get_job_snapshot(process_id)
        if snapshot is None:
            return _bounded_error(
                "process_not_found", "managed process was not found"
            )

        for name, value, stream in (
            ("stdout_cursor", stdout_cursor, snapshot["stdout"]),
            ("stderr_cursor", stderr_cursor, snapshot["stderr"]),
        ):
            error = _validate_cursor(name, value, stream["end_offset"])
            if error is not None:
                return error

        stdout_info = _stream_available(
            snapshot["stdout"], stdout_cursor, max_chars_per_stream
        )
        stderr_info = _stream_available(
            snapshot["stderr"], stderr_cursor, max_chars_per_stream
        )
        return _fit_read_result(snapshot, stdout_info, stderr_info)

    def list_processes() -> str:
        _ = _bound_dependencies  # bound, never model-visible
        snapshots = []
        for process_id in process_manager.list_job_ids():
            snapshot = process_manager.get_job_snapshot(process_id)
            if snapshot is not None:
                snapshots.append({
                    "process_id": snapshot["process_id"],
                    "command": snapshot["command"],
                    "cwd": snapshot["cwd"],
                    "status": snapshot["status"],
                    "returncode": snapshot["returncode"],
                    "termination_reason": snapshot["termination_reason"],
                    "start_at": snapshot["start_at"],
                    "finish_at": snapshot["finish_at"],
                })

        budget = _live_budget()
        total = len(snapshots)
        for count in range(total, -1, -1):
            payload = {
                "processes": snapshots[:count],
                "total": total,
                "output_truncated": count < total,
            }
            encoded = _encode(payload)
            if len(encoded) <= budget:
                return encoded
        return _tiny_fallback(budget)

    def send_process_input(process_id: str, text: str = "",
                           close_stdin: bool = False) -> str:
        try:
            result = process_manager.send_input(
                process_id, text=text, close_stdin=close_stdin
            )
        except Exception as exc:
            return _manager_error(exc)
        return _bounded_payload(result)

    def stop_process(process_id: str) -> str:
        if not isinstance(process_id, str) or not process_id:
            return _bounded_error(
                "invalid_process_id", "process_id must be a non-empty string"
            )
        before = process_manager.get_job_snapshot(process_id)
        if before is None:
            return _bounded_error(
                "process_not_found", "managed process was not found"
            )

        try:
            requested = process_manager.request_stop(process_id)
        except Exception as exc:
            return _manager_error(exc)
        immediate = process_manager.get_job_snapshot(process_id) or before
        status = immediate["status"]
        return _bounded_payload({
            "process_id": process_id,
            "stop_requested": bool(requested),
            "status": status,
            "terminal_observed": status != "running",
        })

    registry.register(
        name="start_process",
        fn=start_process,
        description=(
            "Start a managed persistent shell process. An omitted cwd uses "
            "the active Project root when present, otherwise the user home."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to launch persistently.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory; explicit relative paths use Lumina's cwd.",
                },
            },
            "required": ["command"],
        },
    )

    registry.register(
        name="read_process",
        fn=read_process,
        description=(
            "Read incremental stdout/stderr from a managed persistent process "
            "using absolute character cursors. Reports history eviction and "
            "whether more retained output remains."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {
                    "type": "string",
                    "description": "Opaque Lumina process identifier.",
                },
                "stdout_cursor": {
                    "type": "integer",
                    "description": "Absolute stdout character cursor; default 0.",
                },
                "stderr_cursor": {
                    "type": "integer",
                    "description": "Absolute stderr character cursor; default 0.",
                },
                "max_chars_per_stream": {
                    "type": "integer",
                    "description": "Maximum characters requested from each stream; default 4000.",
                },
            },
            "required": ["process_id"],
        },
    )
    registry.register(
        name="send_process_input",
        fn=send_process_input,
        description=(
            "Send exact text to a managed process stdin and optionally close "
            "stdin to deliver EOF. No newline is added automatically."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {
                    "type": "string",
                    "description": "Opaque Lumina process identifier.",
                },
                "text": {
                    "type": "string",
                    "description": "Exact text to write; default empty.",
                },
                "close_stdin": {
                    "type": "boolean",
                    "description": "Close stdin after writing to deliver EOF; default false.",
                },
            },
            "required": ["process_id"],
        },
    )
    registry.register(
        name="stop_process",
        fn=stop_process,
        description=(
            "Request whole-tree termination of a managed process and report "
            "the immediate observed status without claiming death early."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {
                    "type": "string",
                    "description": "Opaque Lumina process identifier.",
                },
            },
            "required": ["process_id"],
        },
    )
    registry.register(
        name="list_processes",
        fn=list_processes,
        description=(
            "List managed persistent processes in deterministic launch order "
            "with compact lifecycle metadata and no stream payloads."
        ),
        parameters={"type": "object", "properties": {}},
    )
