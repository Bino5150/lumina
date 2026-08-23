"""Owner-only model adapter for the structured pytest kernel (CODING-06A2).

run_tests is the ONLY model-facing surface onto core.test_runner. The model
supplies which tests to run (selectors), an optional cwd override, and a
timeout -- Lumina owns executable construction, environment, lifecycle,
result classification, repository observation, and evidence applicability.

No durable checkpoint evidence is written here. checkpoint_bindable is a
truthful description of whether THIS run's observed pre/post repository
state could later qualify as machine evidence -- it is never persisted,
never authorizes commit/push, and is independent of whether the tests
themselves passed.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import config
import core.coding_checkpoint as checkpoint_store
import core.coding_checkpoint_observation as checkpoint_observation
from core import emergency_stop, test_runner


DEFAULT_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600

# Bounded, minimal presentation hygiene only -- no secrets-classification
# engine (CODING-06A2 section 23). Strips ANSI CSI sequences and C0 control
# bytes other than tab/newline from repository-controlled text (node IDs,
# excerpts, parent-observed diagnostics) before it is ever JSON-encoded.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    value = _ANSI_ESCAPE_RE.sub("", value)
    return _CONTROL_CHAR_RE.sub("", value)


# ---------------------------------------------------------------------------
# Bounded JSON output -- same local convention as tools/coding_checkpoint.py
# and tools/processes.py: no shared helper module, each adapter owns its own
# copy so a change to one tool's fitting behavior can never silently ripple
# into another's.
# ---------------------------------------------------------------------------

def _live_budget() -> int:
    try:
        return max(0, int(config.TOOL_RESULT_MAX_CHARS))
    except (TypeError, ValueError):
        return 0


def _encode(payload) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _tiny_json(budget: int) -> str:
    if budget >= 2:
        return "{}"
    if budget == 1:
        return "0"
    return ""


def _bounded_candidates(candidates: list) -> str:
    budget = _live_budget()
    for payload in candidates:
        encoded = _encode(payload)
        if len(encoded) <= budget:
            return encoded
    return _tiny_json(budget)


def _bounded_error(code: str, message: str) -> str:
    return _bounded_candidates([
        {"status": "error", "error": code, "message": message},
        {"status": "error", "error": code},
        {"error": code},
        {"status": "error"},
    ])


# ---------------------------------------------------------------------------
# Model-facing input validation. A1B (core/test_runner.py) validates again
# independently -- this layer owns the model-facing schema/bounds contract
# (CODING-06A2 section 15/16), not a bypass of A1B's own checks.
# ---------------------------------------------------------------------------

def _validate_selectors_arg(value):
    """Returns (tuple_of_selectors, error_message_or_None)."""
    if value is None:
        return (), None
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, list):
        return None, "selectors must be a list of strings"
    if len(value) > test_runner.MAX_SELECTORS:
        return None, f"at most {test_runner.MAX_SELECTORS} selectors are allowed"
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            return None, f"selectors[{index}] must be a string"
        if not item:
            return None, f"selectors[{index}] must not be empty"
        if len(item) > test_runner.MAX_SELECTOR_CHARS:
            return None, f"selectors[{index}] exceeds {test_runner.MAX_SELECTOR_CHARS} characters"
        if "\0" in item:
            return None, f"selectors[{index}] must not contain NUL"
        normalized.append(item)
    return tuple(normalized), None


def _validate_timeout_arg(value):
    """Returns (int_timeout, error_message_or_None). Stricter than A1B's own
    float-accepting validator on purpose -- the model-facing contract is a
    plain bounded integer (CODING-06A2 section 15), not "whatever Python
    considers numeric"."""
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "timeout_seconds must be an integer"
    if not (MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS):
        return None, (
            f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
        )
    return value, None


def _selector_path_part(selector: str) -> str:
    return selector.split("::", 1)[0]


def _selector_is_in_scope(selector: str, cwd: str, canonical_root: str) -> bool:
    """Conservative, non-heroic scope check (CODING-06A2 section 12).

    Only returns True when the selector's path portion can be confidently
    resolved to a location inside canonical_root. Anything ambiguous (empty,
    flag-like, unresolvable, cross-device) fails CLOSED as out-of-scope --
    this only affects checkpoint_bindable, never execution itself.
    """
    path_part = _selector_path_part(selector)
    if not path_part or path_part.startswith("-"):
        return False
    candidate = path_part if os.path.isabs(path_part) else os.path.join(cwd, path_part)
    try:
        resolved = os.path.realpath(candidate)
        root = os.path.realpath(canonical_root)
        return os.path.commonpath([root, resolved]) == root
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Evidence layering (CODING-06A2 section 17): A1B's TestRunResult stays pure
# execution truth. This module combines it with cwd/Project/observation
# facts into one evidence object, then a dedicated renderer fits it to the
# live TOOL_RESULT_MAX_CHARS budget.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestExecutionEvidence:
    result: object  # core.test_runner.TestRunResult
    cwd: str
    project_name: Optional[str]
    stable_project_binding: Optional[bool]
    target_matches_project: Optional[bool]
    pre_state_ref: Optional[str]
    post_state_ref: Optional[str]
    stable_repository_state: Optional[bool]
    checkpoint_bindable: bool


def _capture_observation(active_project):
    """Best-effort live observation; never raises. Returns (observation, ok)."""
    try:
        return checkpoint_observation.capture_live_observation(active_project, ()), True
    except Exception:
        return None, False


def _build_evidence(*, result, cwd, active_project, selectors,
                     pre_observation, pre_ok, post_observation, post_ok) -> TestExecutionEvidence:
    if active_project is None:
        return TestExecutionEvidence(
            result=result, cwd=cwd, project_name=None,
            stable_project_binding=None, target_matches_project=None,
            pre_state_ref=None, post_state_ref=None,
            stable_repository_state=None, checkpoint_bindable=False,
        )

    target_matches = None
    external_selector = False
    if pre_observation is not None:
        try:
            cwd_identity = checkpoint_store.resolve_target_identity(cwd)
            target_matches = cwd_identity.target_key == pre_observation.identity.target_key
        except Exception:
            target_matches = False
        if selectors:
            external_selector = any(
                not _selector_is_in_scope(s, cwd, pre_observation.identity.canonical_root)
                for s in selectors
            )

    # capture_live_observation() re-verifies the durable binding against the
    # SAME immutable snapshot both at its own start and end (see
    # core/coding_checkpoint_observation.py's verify_project_binding()). Pre
    # and post each succeeding is therefore already proof the binding never
    # drifted across the whole window -- no separate re-check call is needed.
    stable_binding = pre_ok and post_ok and pre_observation is not None and post_observation is not None

    pre_ref = pre_observation.state_ref if pre_observation is not None else None
    post_ref = post_observation.state_ref if post_observation is not None else None
    stable_state = None
    if pre_ref is not None and post_ref is not None:
        stable_state = pre_ref == post_ref

    checkpoint_bindable = bool(
        target_matches is True
        and stable_binding
        and pre_ref is not None
        and post_ref is not None
        and stable_state is True
        and not external_selector
    )

    return TestExecutionEvidence(
        result=result, cwd=cwd, project_name=active_project.name,
        stable_project_binding=stable_binding,
        target_matches_project=target_matches,
        pre_state_ref=pre_ref, post_state_ref=post_ref,
        stable_repository_state=stable_state,
        checkpoint_bindable=checkpoint_bindable,
    )


def _failure_payload(detail: dict, excerpt_limit=None) -> dict:
    excerpt = _sanitize_text(detail["excerpt"]) or ""
    truncated = False
    if excerpt_limit is not None and len(excerpt) > excerpt_limit:
        excerpt = excerpt[:excerpt_limit]
        truncated = True
    return {
        "node_id": _sanitize_text(detail["node_id"]),
        "phase": detail["phase"],
        "kind": detail["kind"],
        "excerpt": excerpt,
        "excerpt_truncated": truncated,
    }


def _render_result(evidence: TestExecutionEvidence) -> str:
    result = evidence.result
    failures_full = [_failure_payload(d) for d in result.relevant_failures]
    stripped = [
        {"node_id": f["node_id"], "phase": f["phase"], "kind": f["kind"]}
        for f in failures_full
    ]
    diagnostic = _sanitize_text(result.diagnostic)

    def _core(*, include_diagnostic=True, include_metadata=True,
              failures=None, diagnostic_limit=None):
        payload = {
            "status": result.status,
            "exit_code": result.exit_code,
            "counts": result.counts,
            "duration_seconds": round(result.duration_seconds, 3),
            "report_state": result.report_state,
            "termination_reason": result.termination_reason,
            "checkpoint_bindable": evidence.checkpoint_bindable,
        }
        if include_metadata:
            payload["stable_repository_state"] = evidence.stable_repository_state
            payload["stable_project_binding"] = evidence.stable_project_binding
            payload["project"] = evidence.project_name
            payload["cwd"] = evidence.cwd
        if failures is not None:
            payload["failures"] = failures
            payload["failure_details_truncated"] = result.failure_details_truncated
        if include_diagnostic and diagnostic:
            text = diagnostic if diagnostic_limit is None else diagnostic[:diagnostic_limit]
            payload["diagnostic"] = text
        return payload

    candidates = [
        _core(failures=failures_full),
        _core(failures=failures_full, diagnostic_limit=512),
        _core(failures=failures_full, include_diagnostic=False),
    ]
    for excerpt_limit in (1024, 256, 64, 0):
        shrunk = [_failure_payload(d, excerpt_limit=excerpt_limit) for d in result.relevant_failures]
        candidates.append(_core(failures=shrunk, include_diagnostic=False))
    candidates.append(_core(failures=stripped, include_diagnostic=False))
    for keep in (10, 5, 1, 0):
        candidates.append(_core(failures=stripped[:keep], include_diagnostic=False))
    candidates.append(_core(failures=None, include_diagnostic=False))
    candidates.append(_core(failures=None, include_diagnostic=False, include_metadata=False))
    candidates.append({
        "status": result.status,
        "exit_code": result.exit_code,
        "checkpoint_bindable": evidence.checkpoint_bindable,
    })
    candidates.append({"status": result.status, "exit_code": result.exit_code})
    candidates.append({"status": result.status})

    return _bounded_candidates(candidates)


def register_tests_tools(registry, project_state=None, cancel_state=None):
    """Register run_tests, the sole model-facing surface onto core.test_runner.

    project_state: the constructing agent's core.project_context.ProjectContextState
    -- same holder threaded into every other Project-aware registrar.
    cancel_state: the constructing agent's per-turn cooperative-cancellation
    holder (core.agent.TurnCancellation). Read at call time, never cached --
    a blocking run_tests call must observe whichever cancel_event is active
    for the turn actually running it.
    """

    def run_tests(selectors=None, cwd=None, timeout_seconds=None, **unsupported_fields) -> str:
        if unsupported_fields:
            return _bounded_error(
                "invalid_arguments", "run_tests accepts no additional arguments"
            )

        normalized_selectors, selector_error = _validate_selectors_arg(selectors)
        if selector_error is not None:
            return _bounded_error("invalid_selectors", selector_error)

        normalized_timeout, timeout_error = _validate_timeout_arg(timeout_seconds)
        if timeout_error is not None:
            return _bounded_error("invalid_timeout", timeout_error)

        active_project = project_state.snapshot() if project_state is not None else None

        if cwd is not None:
            if not isinstance(cwd, str) or not cwd:
                return _bounded_error("invalid_cwd", "cwd must be a non-empty string or null")
            expanded = os.path.expanduser(cwd)
            concrete_cwd = expanded if os.path.isabs(expanded) else os.path.abspath(expanded)
        elif active_project is not None:
            # CODING-06A2 corrective 1: cwd is being DERIVED from the
            # snapshot (caller gave no explicit intent), so the durable
            # binding for this Project must still match that snapshot
            # before any child process launches. A stale/repointed/corrupt
            # binding rejects right here -- it must never silently execute
            # at the (possibly now-wrong) snapshot root, and it must never
            # follow whatever the durable binding was repointed to instead;
            # the only recovery is to reactivate the Project or pass cwd
            # explicitly. This gate does not apply when cwd is explicit
            # (caller intent stands on its own; see the branch above).
            try:
                checkpoint_observation.verify_project_binding(active_project)
            except checkpoint_observation.ProjectBindingChanged:
                return _bounded_error(
                    "stale_project_binding",
                    f"active Project {active_project.name!r}'s durable binding no "
                    "longer matches this session's snapshot; reactivate the "
                    "Project or pass cwd explicitly",
                )
            concrete_cwd = active_project.root
        else:
            return _bounded_error(
                "project_required",
                "no active Project and no cwd was given; activate a Project or pass cwd",
            )

        if not os.path.isdir(concrete_cwd):
            return _bounded_error("invalid_cwd", "cwd does not exist or is not a directory")

        pre_observation = pre_ok = None
        if active_project is not None:
            pre_observation, pre_ok = _capture_observation(active_project)

        cancel_event = cancel_state.get() if cancel_state is not None else None
        result = test_runner.run_pytest(
            cwd=concrete_cwd, selectors=normalized_selectors,
            timeout=float(normalized_timeout), cancel_event=cancel_event,
        )

        post_observation = post_ok = None
        if active_project is not None and pre_observation is not None:
            # No subprocess (including the Git probes observation uses) is
            # launched once emergency stop has latched -- CODING-06A2 section
            # 14. A latch mid-run still yields a truthful, already-classified
            # execution result; only post-run evidence is skipped.
            if emergency_stop.is_latched():
                post_ok = False
            else:
                post_observation, post_ok = _capture_observation(active_project)

        evidence = _build_evidence(
            result=result, cwd=concrete_cwd, active_project=active_project,
            selectors=normalized_selectors,
            pre_observation=pre_observation, pre_ok=bool(pre_ok),
            post_observation=post_observation, post_ok=bool(post_ok),
        )
        return _render_result(evidence)

    registry.register(
        name="run_tests",
        fn=run_tests,
        description=(
            "Run the repository's pytest suite (or specific selectors) through "
            "Lumina's structured, managed pytest kernel and report a truthful "
            "execution result. Owner-only. Does not authorize commit, push, or "
            "any durable checkpoint -- checkpoint_bindable only describes "
            "whether this run's observed repository state could later qualify "
            "as machine evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "selectors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": test_runner.MAX_SELECTORS,
                    "description": (
                        "Optional literal pytest selectors (paths, ::node ids). "
                        "Omit or leave empty to run the repository's normal "
                        "configured suite."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Optional absolute or ~-relative working directory. "
                        "Omit to use the active Project root."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": MIN_TIMEOUT_SECONDS,
                    "maximum": MAX_TIMEOUT_SECONDS,
                    "description": f"Execution timeout in seconds; default {DEFAULT_TIMEOUT_SECONDS}.",
                },
            },
            "additionalProperties": False,
        },
    )
