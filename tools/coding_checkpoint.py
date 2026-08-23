"""Owner-only model adapters for durable coding checkpoints.

The adapter accepts workflow intent and Project-relative path names only.
All target, Git, file, freshness, and validation-binding facts come from
core.coding_checkpoint_observation. Registration is unconditional; central
tool-profile authority decides whether either tool is enabled.
"""

import json
import sqlite3
from typing import Optional

import config
import core.coding_checkpoint as checkpoint_store
import core.coding_checkpoint_observation as checkpoint_observation


def _live_budget() -> int:
    """Live char budget, clamped to >= 0 so a callee never has to.

    Read fresh on every call rather than cached at import, so a runtime
    change to config.TOOL_RESULT_MAX_CHARS (Settings UI, tests) takes
    effect on the next call. A negative or non-numeric configured value
    degrades safety-first to 0 rather than being treated as unlimited.
    """
    try:
        return max(0, int(config.TOOL_RESULT_MAX_CHARS))
    except (TypeError, ValueError):
        return 0


def _encode(payload) -> str:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _tiny_json(budget: int) -> str:
    """Last-resort output once no real candidate fits the live budget.

    budget >= 2 -> "{}", the smallest valid JSON object.
    budget == 1 -> "0", the smallest valid JSON document (a bare scalar).
    budget == 0 -> "", the documented exceptional case. No nonempty string
        is valid JSON at a zero-character ceiling, so this is a deliberate
        exception to the "always return valid JSON" guarantee rather than
        an attempt to synthesize output against it. _live_budget already
        clamps negative/unparsable configured values to 0, so 0 is the
        only budget this function receives that isn't a real character
        allowance.
    """
    if budget >= 2:
        return "{}"
    if budget == 1:
        return "0"
    return ""


def _bounded_candidates(candidates: list) -> str:
    """Return the first deterministic valid-JSON representation that fits."""
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


def _prefix(value, length: int = 12):
    return value[:length] if isinstance(value, str) and value else None


def _observation_summary(value, changed_count: Optional[int] = None) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, checkpoint_observation.LiveObservation):
        data = value.observation_payload()
        if changed_count is None:
            changed_count = len(value.changed_paths)
    else:
        data = value
    return {
        "target_kind": data.get("target_kind"),
        "target_key_prefix": _prefix(data.get("target_key")),
        "head_prefix": _prefix(data.get("head")),
        "branch": data.get("branch"),
        "detached": data.get("detached"),
        "unborn": data.get("unborn"),
        "operation_state": data.get("operation_state", []),
        "status_digest_prefix": _prefix(data.get("status_digest")),
        "status_complete": data.get("status_complete"),
        "changed_paths_stored": changed_count,
        "changed_paths_truncated": data.get("changed_paths_truncated"),
        "upstream_ref": data.get("upstream_ref"),
        "push_posture": data.get("push_posture"),
        "state_ref_prefix": _prefix(data.get("state_ref")),
        "binding_complete": data.get("binding_complete"),
    }


def _compact_observation(summary: Optional[dict]) -> Optional[dict]:
    if summary is None:
        return None
    return {
        "target_kind": summary.get("target_kind"),
        "target_key_prefix": summary.get("target_key_prefix"),
        "head_prefix": summary.get("head_prefix"),
        "branch": summary.get("branch"),
        "detached": summary.get("detached"),
        "unborn": summary.get("unborn"),
        "operation_state": summary.get("operation_state", []),
        "status_digest_prefix": summary.get("status_digest_prefix"),
        "state_ref_prefix": summary.get("state_ref_prefix"),
    }


def _render_validations(record, applicability) -> list:
    applies = list(applicability)
    rendered = []
    for index, validation in enumerate(record.validations):
        current = applies[index] if index < len(applies) else None
        rendered.append({
            "label": validation["label"],
            "outcome": validation["outcome"],
            "source": validation["source"],
            "exit_code": validation.get("exit_code"),
            "summary": validation.get("summary"),
            "reported_at": validation.get("timestamp"),
            "state_ref_prefix": _prefix(validation.get("state_ref")),
            "binding_complete": validation.get("binding_complete") is True,
            "applies_to_current_state": (
                current.applies_to_current_state if current is not None else False
            ),
            "applicability_reason": (
                current.reason if current is not None else "applicability_unavailable"
            ),
        })
    return rendered


def _compact_validations(validations: list) -> list:
    return [
        {
            "label": item["label"],
            "outcome": item["outcome"],
            "source": item["source"],
            "applies_to_current_state": item["applies_to_current_state"],
            "applicability_reason": item["applicability_reason"],
        }
        for item in validations
    ]


def _read_result(result, project_name: str) -> str:
    reasons = list(result.freshness.reasons)
    core = {
        "status": result.status,
        "project": project_name,
        "revision": result.record.revision if result.record is not None else None,
        "freshness": result.freshness.state,
        "mismatch_reasons": reasons,
    }

    if result.status in {"corrupt", "unsupported_schema"}:
        metadata = result.metadata
        core["revision"] = metadata.revision
        safe = {
            **core,
            "safe_metadata": {
                "target_key_prefix": metadata.target_key_prefix,
                "revision": metadata.revision,
                "schema_version": metadata.schema_version,
            },
            "output_truncated": False,
        }
        return _bounded_candidates([
            safe,
            {**core, "output_truncated": True},
            {"status": result.status, "freshness": result.freshness.state},
            {"status": result.status},
        ])

    recorded_summary = None
    current_summary = _observation_summary(result.current)
    workflow = None
    relevant_files = []
    validations = []
    updated_at = None
    if result.record is not None:
        recorded_summary = _observation_summary(
            result.record.observation, len(result.record.changed_paths)
        )
        workflow = result.record.workflow
        relevant_files = result.record.relevant_files
        validations = _render_validations(result.record, result.validation_applicability)
        updated_at = result.record.updated_at

    message = (
        "checkpoint state matches current measured repository state"
        if result.freshness.state == checkpoint_observation.FRESH
        else "checkpoint state does not match current measured repository state"
        if result.freshness.state == checkpoint_observation.STALE
        else "checkpoint freshness could not be established"
    )
    full = {
        **core,
        "updated_at_utc": updated_at,
        "message": message,
        "workflow": workflow,
        "relevant_files": relevant_files,
        "validations": validations,
        "recorded_observation": recorded_summary,
        "current_observation": current_summary,
        "output_truncated": False,
    }

    workflow_compact = None
    if workflow is not None:
        workflow_compact = {
            "task_id": workflow.get("task_id"),
            "phase": workflow.get("phase"),
            "summary": workflow.get("summary"),
            "next_steps": workflow.get("next_steps", []),
            "blockers": workflow.get("blockers", []),
        }
    compact_detail = {
        **core,
        "workflow": workflow_compact,
        "validations": _compact_validations(validations),
        "recorded_observation": _compact_observation(recorded_summary),
        "current_observation": _compact_observation(current_summary),
        "output_truncated": True,
    }
    workflow_priority = {
        **core,
        "workflow": workflow_compact,
        "recorded_observation": _compact_observation(recorded_summary),
        "current_observation": _compact_observation(current_summary),
        "output_truncated": True,
    }
    core_with_targets = {
        **core,
        "recorded_observation": _compact_observation(recorded_summary),
        "current_observation": _compact_observation(current_summary),
        "output_truncated": True,
    }
    freshness_core = {
        "status": result.status,
        "revision": core["revision"],
        "freshness": result.freshness.state,
        "mismatch_reasons": reasons,
        "output_truncated": True,
    }
    return _bounded_candidates([
        full,
        compact_detail,
        workflow_priority,
        core_with_targets,
        freshness_core,
        {
            "status": result.status,
            "revision": core["revision"],
            "freshness": result.freshness.state,
        },
        {"status": result.status, "revision": core["revision"]},
        {"status": result.status},
    ])


def _translate_error(exc: Exception) -> str:
    if isinstance(exc, checkpoint_observation.ProjectBindingChanged):
        return _bounded_error(
            "project_binding_changed",
            "active Project binding changed; refresh or reactivate the Project",
        )
    if isinstance(exc, checkpoint_observation.UnstableCapture):
        return _bounded_error(
            "unstable_capture", "target changed repeatedly during live observation"
        )
    if isinstance(exc, checkpoint_observation.RelevantPathError):
        return _bounded_error(
            "invalid_relevant_path", "one or more relevant paths are invalid"
        )
    if isinstance(exc, checkpoint_observation.GitProbeError):
        return _bounded_error(
            "repository_unavailable", "repository state could not be observed"
        )
    if isinstance(exc, checkpoint_store.RevisionConflict):
        return _bounded_error(
            "revision_conflict", "checkpoint revision changed; re-read and reconcile"
        )
    if isinstance(exc, checkpoint_store.CheckpointTooLarge):
        return _bounded_error(
            "checkpoint_too_large", "checkpoint exceeds its durable size limit"
        )
    if isinstance(exc, checkpoint_store.CheckpointCorrupt):
        return _bounded_error(
            "checkpoint_corrupt", "manual recovery is required before saving"
        )
    if isinstance(exc, checkpoint_store.UnsupportedSchema):
        return _bounded_error(
            "unsupported_schema", "manual recovery is required before saving"
        )
    if isinstance(exc, checkpoint_store.CheckpointNotFound):
        return _bounded_error("checkpoint_not_found", "checkpoint was not found")
    if isinstance(exc, checkpoint_store.CheckpointValidationError):
        return _bounded_error(
            "invalid_checkpoint", "checkpoint input violates the bounded schema"
        )
    if (
        isinstance(exc, sqlite3.OperationalError)
        and getattr(exc, "sqlite_errorcode", None)
        in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    ):
        return _bounded_error("checkpoint_busy", "checkpoint storage is busy")
    return _bounded_error(
        "checkpoint_operation_failed", "checkpoint operation failed"
    )


def register_coding_checkpoint_tools(registry, project_state=None):
    """Register read/save adapters bound to one agent's Project state."""
    _bound_project_state = project_state

    def read_coding_checkpoint(**unsupported_fields) -> str:
        if unsupported_fields:
            return _bounded_error(
                "invalid_checkpoint", "read_coding_checkpoint accepts no arguments"
            )
        project = (
            _bound_project_state.snapshot()
            if _bound_project_state is not None
            else None
        )
        if project is None:
            return _bounded_error(
                "project_required", "activate a Project before reading a checkpoint"
            )
        try:
            result = checkpoint_observation.load_live_checkpoint(project)
            return _read_result(result, project.name)
        except Exception as exc:
            return _translate_error(exc)

    def save_coding_checkpoint(
        expected_revision: Optional[int] = None,
        task_id: str = "",
        title: str = "",
        phase: str = "in_progress",
        slice_id: str = "",
        summary: str = "",
        next_steps: Optional[list] = None,
        blockers: Optional[list] = None,
        relevant_paths: Optional[list] = None,
        validations: Optional[list] = None,
        **unsupported_fields,
    ) -> str:
        if unsupported_fields:
            return _bounded_error(
                "invalid_checkpoint", "unsupported checkpoint fields were supplied"
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
            or not isinstance(task_id, str)
            or not task_id
        ):
            return _bounded_error(
                "invalid_checkpoint",
                "expected_revision and a non-empty task_id are required",
            )
        project = (
            _bound_project_state.snapshot()
            if _bound_project_state is not None
            else None
        )
        if project is None:
            return _bounded_error(
                "project_required", "activate a Project before saving a checkpoint"
            )
        workflow = {
            "task_id": task_id,
            "title": title or None,
            "phase": phase,
            "slice_id": slice_id or None,
            "summary": summary or None,
            "next_steps": next_steps if next_steps is not None else [],
            "blockers": blockers if blockers is not None else [],
        }
        try:
            saved = checkpoint_observation.save_live_checkpoint(
                project,
                workflow,
                relevant_paths=(
                    relevant_paths if relevant_paths is not None else []
                ),
                reported_validations=(validations if validations is not None else []),
                expected_revision=expected_revision,
            )
        except Exception as exc:
            return _translate_error(exc)
        return _bounded_candidates([
            {
                "status": "saved",
                "revision": saved.record.revision,
                "project": project.name,
                "target_key_prefix": _prefix(saved.record.target_key),
                "state_ref_prefix": _prefix(saved.observation.state_ref),
                "freshness_at_save": "matches_recorded",
                "message": (
                    "checkpoint revision was persisted against this measured state"
                ),
            },
            {
                "status": "saved",
                "revision": saved.record.revision,
                "project": project.name,
                "freshness_at_save": "matches_recorded",
                "output_truncated": True,
            },
            {"status": "saved", "revision": saved.record.revision},
            {"status": "saved"},
        ])

    registry.register(
        name="read_coding_checkpoint",
        fn=read_coding_checkpoint,
        description=(
            "Read the active Project's durable coding checkpoint and compare "
            "it with current subsystem-measured repository state. Reported "
            "validations are not verified execution or authorization."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )

    validation_schema = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "maxLength": checkpoint_store.MAX_VALIDATION_LABEL_LEN},
            "outcome": {"type": "string", "enum": sorted(checkpoint_store.VALID_VALIDATION_OUTCOMES)},
            "exit_code": {"type": "integer", "minimum": -1, "maximum": 255},
            "summary": {"type": "string", "maxLength": checkpoint_store.MAX_VALIDATION_SUMMARY_LEN},
            "timestamp": {"type": "string", "maxLength": checkpoint_store.MAX_VALIDATION_TIMESTAMP_LEN},
        },
        "required": ["label", "outcome"],
        "additionalProperties": False,
    }
    registry.register(
        name="save_coding_checkpoint",
        fn=save_coding_checkpoint,
        description=(
            "Persist workflow intent for the active Project using mandatory "
            "revision CAS. Repository and file facts are measured internally; "
            "a save does not authorize commit, push, or any engineering action."
        ),
        parameters={
            "type": "object",
            "properties": {
                "expected_revision": {"type": "integer", "minimum": 0},
                "task_id": {"type": "string", "minLength": 1, "maxLength": checkpoint_store.MAX_TASK_ID_LEN},
                "title": {"type": "string", "maxLength": checkpoint_store.MAX_TITLE_LEN},
                "phase": {"type": "string", "enum": sorted(checkpoint_store.VALID_PHASES)},
                "slice_id": {"type": "string", "maxLength": checkpoint_store.MAX_SLICE_ID_LEN},
                "summary": {"type": "string", "maxLength": checkpoint_store.MAX_SUMMARY_LEN},
                "next_steps": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": checkpoint_store.MAX_NEXT_STEP_LEN},
                    "maxItems": checkpoint_store.MAX_NEXT_STEPS,
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": checkpoint_store.MAX_BLOCKER_LEN},
                    "maxItems": checkpoint_store.MAX_BLOCKERS,
                },
                "relevant_paths": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": checkpoint_store.MAX_PATH_LEN},
                    "maxItems": checkpoint_store.MAX_RELEVANT_FILES,
                },
                "validations": {
                    "type": "array",
                    "items": validation_schema,
                    "maxItems": checkpoint_store.MAX_VALIDATIONS,
                },
            },
            "required": ["expected_revision", "task_id"],
            "additionalProperties": False,
        },
    )
