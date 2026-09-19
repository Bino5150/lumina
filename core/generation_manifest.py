"""
core/generation_manifest.py -- MULTIMODAL-M4-GENERATION-MANIFEST-LAW-01 (2026-09-17)

Per-artifact provenance manifest law for generated media: the durable
contract between specialist generation agents and the rest of the studio.
Every GeneratedArtifact that leaves the specialist layer ships with exactly
one manifest, and the manifest is the ONLY place provenance lives after chat
context dies. Pure, stdlib-only, Qt-free, zero-I/O, nothing executes at
import -- same discipline as core.capability_router.py and
core.generation_spending_policy.py.

EXTEND, DO NOT CLONE (Sol's boundary directive via Bino, 2026-09-17): this
module defines ONE new vocabulary -- the provider/manifest identity axis --
and consumes every other law from the module that already owns it:

- Capability axis: core.capability_router.Capability (frozen M1 vocabulary,
  exact names only). This module never names a capability of its own.
- Ingestion axis: core.generation_job's INGESTION_* constants, snapshotted
  VERBATIM. This module never derives, upgrades, or summarizes them.
- Secret defense: core.redaction.is_secret_key() + redact_secret_shapes(),
  the same primitives Flight Recorder and the continuity compiler use.
- Cost/authorization shape: mirrors core.generation_spending_policy's
  fail-closed posture (an unknown cost is never silently free; here, a
  cost-bearing artifact with no owner-authorization reference is refused).

The ten frozen laws (each proven 1:1 by a named test in
tests/test_generation_manifest_law_01.py):

  L1  exact allowed fields        -- exact-set schema; missing OR extra fails
  L2  capability/provider axes    -- two independent vocabularies, never merged
  L3  missing provider fails      -- unregistered provider => hard refusal
  L4  unknown capability fails    -- non-canonical capability => hard refusal
  L5  cost-bearing needs auth ref -- no authorization_ref => no cost manifest
  L6  extra fields fail closed    -- exact-set is the law, not a suggestion
  L7  lifecycle never collapses   -- provider-completion, local ingestion, and
                                     owner review stay independently observable;
                                     no single "success" field exists
  L8  partial is never laundered  -- INGESTION_PARTIAL snapshots verbatim;
                                     the builder has no override for it
  L9  estimate != actual          -- two fields, never summed, never merged
  L10 no secret material enters   -- key-marker + value-shape scan on every
                                     free-text field and the whole params body

PROVIDER VOCABULARY IS REGISTERED DELIBERATELY, ONE ENTRY AT A TIME.
CANONICAL_PROVIDERS shipped as an empty frozenset at freeze time
(MULTIMODAL-M4-GENERATION-MANIFEST-LAW-01). A provider entry is added by a
deliberate, owner-authorized production edit as part of that provider's own
integration rollout -- never by a test, never to make a failing check pass.
Until an entry exists, every manifest naming that provider is refused; that
refusal is the guard working, not a bug ("you gave my hands a new tool, but
nobody updated the paperwork").

Higgsfield's entry landed via MULTIMODAL-M4-HIGGSFIELD-PROVIDER-
REGISTRATION-01 (2026-09-17), once its real server-REST adapter
(core.higgsfield_adapter.HiggsfieldAdapter) had landed and was verified --
the exact canonical identity that adapter's own describe_model() already
names as `"provider": "higgsfield"`. No alias, no model name (e.g.
"nano-banana"), and no CLI-only identity was registered alongside it; the
provider axis and core.capability_router's capability axis remain
completely independent (L2) -- registering a provider identity says
nothing about which capabilities it is wired to elsewhere.

Most tests use fake provider identities injected via monkeypatch so every
other law can be exercised without depending on production registration; a
small group opts out via the `real_provider_vocabulary` fixture to prove
the real production vocabulary directly -- both that Higgsfield is
registered and that every other identity still fails closed.

Amendment (MULTIMODAL-M4-GENERATION-MANIFEST-PERSISTENCE-01, 2026-09-19):
validate_manifest()/build_manifest()/serialize_manifest() remain exactly as
frozen above -- pure, stdlib-only, zero-I/O. persist_manifest() (below) is
the one deliberate exception: it performs real filesystem I/O to make an
already-validated manifest durable under Lumina's DATA_DIR, mirroring
core.generation_artifact.ingest_artifact()'s own "validate/hash first,
write via the same temp-file+fsync+os.replace() idiom, never leave a
partial file behind" discipline. It is additive only -- the manifest
schema, every one of the ten frozen laws, and every existing function's
behavior are unchanged. See persist_manifest()'s own docstring for the
storage design and ManifestPersistenceError for why a durability failure
is deliberately never conflated with a lawfulness (ManifestValidationError)
failure.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from core.capability_router import Capability
from core.generation_job import (
    INGESTION_FAILED,
    INGESTION_INGESTED,
    INGESTION_PARTIAL,
    INGESTION_PENDING,
)
from core.redaction import is_secret_key, redact_secret_shapes
from core.test_isolation import refuse_if_production_path

__all__ = [
    "CANONICAL_PROVIDERS",
    "REVIEW_SUBMITTED",
    "REVIEW_COMPLETED",
    "REVIEW_REVIEWED",
    "MANIFEST_FIELDS",
    "MAX_PARAMS_BYTES",
    "ManifestError",
    "ManifestValidationError",
    "UnknownManifestProvider",
    "UnknownManifestCapability",
    "MissingAuthorizationRef",
    "SecretMaterialInManifest",
    "ManifestPersistenceError",
    "validate_manifest",
    "build_manifest",
    "serialize_manifest",
    "persist_manifest",
]

# ---------------------------------------------------------------------------
# Provider / manifest identity vocabulary (the one NEW axis this module owns)
# ---------------------------------------------------------------------------

# Higgsfield is the first (and, at this freeze, only) deliberately
# registered provider identity -- added by MULTIMODAL-M4-HIGGSFIELD-
# PROVIDER-REGISTRATION-01 once core.higgsfield_adapter.HiggsfieldAdapter
# (the real server-REST adapter) had landed and was verified. See module
# docstring: every other provider entry is added the same way -- a
# deliberate, owner-authorized production edit as part of that provider's
# own integration rollout. Never extended by tests (tests inject fakes via
# monkeypatch), never extended to make a check pass, never extended with an
# alias.
CANONICAL_PROVIDERS: frozenset = frozenset({"higgsfield"})

# Owner-facing lifecycle axis for the ARTIFACT (motion-design law, adopted):
# provider completion, local ingestion, and owner review are three different
# events on three different axes and are never collapsed into one state.
REVIEW_SUBMITTED = "submitted"
REVIEW_COMPLETED = "completed"
REVIEW_REVIEWED = "reviewed"
_REVIEW_STATES = frozenset({REVIEW_SUBMITTED, REVIEW_COMPLETED, REVIEW_REVIEWED})

_VALID_INGESTION_SNAPSHOTS = frozenset({
    INGESTION_PENDING, INGESTION_INGESTED, INGESTION_FAILED, INGESTION_PARTIAL,
})

# Exact-set schema. EVERY field must be present; fields whose value may be
# absent say so by carrying None explicitly (absence of the KEY is always a
# violation). There is deliberately no "success"/"status" aggregate field.
MANIFEST_FIELDS = frozenset({
    "capability",            # canonical M1 capability name (L2/L4)
    "provider",              # registered provider/manifest identity (L2/L3)
    "model",                 # live-discovered model identifier actually used
    "lumina_job_id",         # Lumina-minted correlation id
    "provider_job_id",       # provider's own request/job id (reattachment)
    "artifact_id",           # GeneratedArtifact.artifact_id
    "artifact_path",         # LOCAL path; a remote URL is never durable
    "provider_output_index", # Lumina-assigned ordinal (int >= 0)
    "job_ingestion_state",   # VERBATIM snapshot of job.ingestion_state (L8)
    "review_state",          # submitted | completed | reviewed (L7)
    "cost_estimate",         # provider estimate or None (L9)
    "cost_actual",           # actual charge or None (L9) -- separate field
    "cost_unit",             # required when either cost is present
    "authorization_ref",     # owner budget-envelope/job-class ref (L5)
    "created_utc",           # tz-aware ISO-8601
    "params",                # mapping echo of generation settings (scanned)
    "failure_class",         # provider failure class or None
})

MAX_PARAMS_BYTES = 16 * 1024  # mirrors core.generation_job's settings cap


# ---------------------------------------------------------------------------
# Errors -- all validation refusals are ManifestValidationError subclasses
# with the specific refusal preserved in .errors
# ---------------------------------------------------------------------------

class ManifestError(Exception):
    """Base class for every error this module raises."""


class ManifestValidationError(ManifestError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class UnknownManifestProvider(ManifestValidationError):
    """L3: provider is not in CANONICAL_PROVIDERS. The vocabulary entry must
    be added deliberately -- this refusal is the paperwork working."""


class UnknownManifestCapability(ManifestValidationError):
    """L4: capability is not in core.capability_router's frozen vocabulary."""


class MissingAuthorizationRef(ManifestValidationError):
    """L5: cost-bearing artifact has no owner-authorization reference."""


class SecretMaterialInManifest(ManifestValidationError):
    """L10: secret-shaped material detected; the manifest is refused rather
    than silently redacted (a silent redaction would hide how the secret got
    there). Best-effort known-shape detection, same posture as
    core.redaction itself -- defense in depth, not a guarantee."""


class ManifestPersistenceError(ManifestError):
    """The manifest was LAWFUL -- it passed validate_manifest() in full --
    but could not be made durable (a filesystem failure: permission denied,
    disk full, an unwritable path). Deliberately NOT a ManifestValidationError
    subclass: a caller must be able to tell "this manifest is unlawful" apart
    from "this manifest is lawful but durability failed," since the correct
    response to each is different (the former is a caller/config bug that
    should never be retried as-is; the latter is an operational condition a
    caller may legitimately retry). Never raised for a lawfulness problem --
    persist_manifest() lets validate_manifest()'s own exceptions propagate
    unwrapped for that case."""

    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_real_number(value) -> bool:
    # bool is an int subclass in Python; a boolean cost or ordinal is junk.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _non_empty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _assert_no_secret_material(field_name: str, text: str) -> None:
    if redact_secret_shapes(text) != text:
        raise SecretMaterialInManifest([
            f"secret-shaped material refused in manifest field {field_name!r}: "
            "no credential/secret material may enter a manifest (L10)"
        ])


def _scan_params(params: Mapping[str, Any]) -> None:
    for key, value in params.items():
        if is_secret_key(str(key)):
            raise SecretMaterialInManifest([
                f"params key {key!r} matches a secret key-name marker; refusing "
                "the manifest rather than redacting silently (L10)"
            ])
        if isinstance(value, str):
            _assert_no_secret_material(f"params[{key!r}]", value)


# ---------------------------------------------------------------------------
# validate_manifest -- the single law path (build_manifest routes through it)
# ---------------------------------------------------------------------------

def validate_manifest(manifest: Mapping[str, Any]) -> dict:
    """Validate one per-artifact manifest against the exact-set law.

    Returns a normalized plain-dict copy on success. Raises a
    ManifestValidationError subclass on the first refusal; every refusal is
    fail-closed -- nothing is coerced, defaulted, upgraded, or dropped.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestValidationError([
            f"manifest must be a mapping, got {type(manifest).__name__}"
        ])

    keys = set(manifest.keys())
    missing = sorted(MANIFEST_FIELDS - keys)
    extra = sorted(keys - MANIFEST_FIELDS)
    if missing:
        raise ManifestValidationError([f"missing required manifest field(s): {missing}"])
    if extra:
        raise ManifestValidationError([
            f"unknown extra manifest field(s): {extra} (L1/L6: exact set is the law)"
        ])

    # -- capability axis (consumes M1's frozen vocabulary; never a local copy)
    capability = manifest["capability"]
    if isinstance(capability, Capability):
        capability = capability.value
    elif isinstance(capability, str) and not isinstance(capability, Enum):
        try:
            capability = Capability(capability).value
        except ValueError:
            raise UnknownManifestCapability([
                f"unknown capability: {capability!r} is not in the frozen M1 "
                "capability vocabulary (L4)"
            ])
    else:
        raise UnknownManifestCapability([
            f"capability must be a canonical capability name or Capability, "
            f"got {type(capability).__name__} (L4)"
        ])

    # -- provider axis (the vocabulary THIS module owns; exact strings only)
    provider = manifest["provider"]
    if type(provider) is not str:
        raise UnknownManifestProvider([
            f"provider must be a plain string provider identity, got "
            f"{type(provider).__name__} (L2/L3)"
        ])
    if provider not in CANONICAL_PROVIDERS:
        raise UnknownManifestProvider([
            f"unknown provider: {provider!r} -- no entry in the canonical "
            "provider/manifest vocabulary; add the entry deliberately as part "
            "of that provider's integration rollout, never to make a check "
            "pass (L3)"
        ])

    # -- identity strings
    for field in ("model", "lumina_job_id", "provider_job_id", "artifact_id"):
        if not _non_empty_str(manifest[field]):
            raise ManifestValidationError([
                f"manifest field {field!r} must be a non-empty string"
            ])

    # -- artifact_path: local only. A remote URL is not a durable artifact.
    artifact_path = manifest["artifact_path"]
    if not _non_empty_str(artifact_path):
        raise ManifestValidationError([
            "manifest field 'artifact_path' must be a non-empty local path"
        ])
    lowered = artifact_path.strip().lower()
    if lowered.startswith(("http://", "https://", "//", "ftp://")):
        raise ManifestValidationError([
            "manifest field 'artifact_path' must be a LOCAL path; a remote URL "
            "is untrusted data and never a durable artifact (substrate law)"
        ])

    # -- provider_output_index: Lumina-assigned ordinal, int >= 0, never bool
    index = manifest["provider_output_index"]
    if not _is_real_number(index) or isinstance(index, float) or index < 0:
        raise ManifestValidationError([
            f"manifest field 'provider_output_index' must be an int >= 0, got {index!r}"
        ])

    # -- job_ingestion_state: VERBATIM snapshot from the substrate constants.
    # Never derived, never upgraded (L8). None is honest (never snapshotted).
    snapshot = manifest["job_ingestion_state"]
    if snapshot is not None and snapshot not in _VALID_INGESTION_SNAPSHOTS:
        raise ManifestValidationError([
            f"manifest field 'job_ingestion_state' must be None or one of "
            f"{sorted(_VALID_INGESTION_SNAPSHOTS)} (verbatim substrate "
            f"vocabulary), got {snapshot!r}"
        ])

    # -- review_state: the owner-facing axis, distinct from everything else (L7)
    review_state = manifest["review_state"]
    if review_state not in _REVIEW_STATES:
        raise ManifestValidationError([
            f"manifest field 'review_state' must be one of "
            f"{sorted(_REVIEW_STATES)}, got {review_state!r} (L7)"
        ])

    # -- costs: separate fields, never merged (L9)
    cost_estimate = manifest["cost_estimate"]
    cost_actual = manifest["cost_actual"]
    for name, value in (("cost_estimate", cost_estimate), ("cost_actual", cost_actual)):
        if value is not None and (not _is_real_number(value) or value < 0):
            raise ManifestValidationError([
                f"manifest field {name!r} must be a non-negative number or None, got {value!r}"
            ])
    cost_unit = manifest["cost_unit"]
    cost_bearing = cost_estimate is not None or cost_actual is not None
    if cost_bearing and not _non_empty_str(cost_unit):
        raise ManifestValidationError([
            "manifest field 'cost_unit' is required when either cost field is present"
        ])
    if not cost_bearing and cost_unit is not None and not _non_empty_str(cost_unit):
        raise ManifestValidationError([
            "manifest field 'cost_unit' must be None or a non-empty string"
        ])

    # -- authorization reference: required exactly when cost-bearing (L5).
    # A present-but-blank reference on a cost-bearing manifest IS a missing
    # reference -- the law is about substance, not key presence.
    authorization_ref = manifest["authorization_ref"]
    if cost_bearing and not _non_empty_str(authorization_ref):
        raise MissingAuthorizationRef([
            "cost-bearing artifact (cost_estimate or cost_actual present) has no "
            "owner-authorization reference; generation spend is never ambient (L5)"
        ])
    if not cost_bearing and authorization_ref is not None and not _non_empty_str(authorization_ref):
        raise ManifestValidationError([
            "manifest field 'authorization_ref' must be None or a non-empty string"
        ])

    # -- created_utc: tz-aware ISO-8601
    created_utc = manifest["created_utc"]
    if not isinstance(created_utc, str):
        raise ManifestValidationError([
            "manifest field 'created_utc' must be a tz-aware ISO-8601 string"
        ])
    try:
        parsed = datetime.fromisoformat(created_utc)
    except ValueError:
        raise ManifestValidationError([
            f"manifest field 'created_utc' is not ISO-8601: {created_utc!r}"
        ])
    if parsed.utcoffset() is None:
        raise ManifestValidationError([
            "manifest field 'created_utc' must be timezone-aware (substrate "
            "timestamps are UTC); a naive timestamp is refused"
        ])

    # -- params: mapping, JSON-serializable, size-capped, secret-scanned
    params = manifest["params"]
    if not isinstance(params, Mapping):
        raise ManifestValidationError([
            f"manifest field 'params' must be a mapping, got {type(params).__name__}"
        ])
    try:
        encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError([
            f"manifest field 'params' is not JSON-serializable: {exc}"
        ])
    if len(encoded.encode("utf-8")) > MAX_PARAMS_BYTES:
        raise ManifestValidationError([
            f"manifest field 'params' exceeds the {MAX_PARAMS_BYTES}-byte cap"
        ])
    _scan_params(params)
    _assert_no_secret_material("params", encoded)

    # -- failure_class: free text, secret-scanned
    failure_class = manifest["failure_class"]
    if failure_class is not None:
        if not _non_empty_str(failure_class):
            raise ManifestValidationError([
                "manifest field 'failure_class' must be None or a non-empty string"
            ])
        _assert_no_secret_material("failure_class", failure_class)

    # -- remaining free-text fields: secret-scanned (L10)
    for field in ("model", "lumina_job_id", "provider_job_id", "artifact_id",
                  "artifact_path", "authorization_ref", "cost_unit"):
        value = manifest[field]
        if isinstance(value, str):
            _assert_no_secret_material(field, value)

    return {
        "capability": capability,
        "provider": provider,
        "model": manifest["model"],
        "lumina_job_id": manifest["lumina_job_id"],
        "provider_job_id": manifest["provider_job_id"],
        "artifact_id": manifest["artifact_id"],
        "artifact_path": manifest["artifact_path"],
        "provider_output_index": index,
        "job_ingestion_state": snapshot,
        "review_state": review_state,
        "cost_estimate": cost_estimate,
        "cost_actual": cost_actual,
        "cost_unit": cost_unit,
        "authorization_ref": authorization_ref,
        "created_utc": created_utc,
        "params": json.loads(encoded),
        "failure_class": failure_class,
    }


def build_manifest(job, artifact, *, provider: str, review_state: str,
                   params: Optional[Mapping[str, Any]] = None,
                   failure_class: Optional[str] = None,
                   authorization_ref: Optional[str] = None,
                   created_utc: Optional[str] = None) -> dict:
    """Build + validate a manifest from substrate records.

    Derives every derivable field FROM the job/artifact records so the
    manifest cannot disagree with them; the caller supplies only what the
    records cannot know (provider identity, review state, params echo,
    authorization reference). Deliberately has NO job_ingestion_state
    parameter: the snapshot is verbatim from the job record (L8). Also has
    no capability/model/cost parameter: those come from the job record or
    not at all.

    `job` must expose the GenerationJob attribute surface; `artifact` the
    GeneratedArtifact attribute surface. Nothing here performs I/O.
    """
    provider_job_id = getattr(job, "provider_job_id", None)
    if not _non_empty_str(provider_job_id):
        raise ManifestValidationError([
            "job has no provider_job_id; an artifact of a never-submitted job "
            "cannot be manifested"
        ])
    ordinal = getattr(artifact, "provider_output_index", None)
    if ordinal is None:
        raise ManifestValidationError([
            "artifact has no provider_output_index; assign the Lumina-assigned "
            "ordinal before manifesting (retrieval determinism is the caller's "
            "discipline per the substrate amendment)"
        ])
    candidate = {
        "capability": job.capability,
        "provider": provider,
        "model": job.model,
        "lumina_job_id": job.lumina_job_id,
        "provider_job_id": provider_job_id,
        "artifact_id": artifact.artifact_id,
        "artifact_path": artifact.local_path,
        "provider_output_index": ordinal,
        "job_ingestion_state": job.ingestion_state,
        "review_state": review_state,
        "cost_estimate": job.cost_estimate,
        "cost_actual": job.cost_actual,
        "cost_unit": job.cost_unit,
        "authorization_ref": authorization_ref,
        "created_utc": created_utc if created_utc is not None else artifact.created_at,
        "params": dict(params) if params is not None else {},
        "failure_class": failure_class if failure_class is not None else job.failure_class,
    }
    return validate_manifest(candidate)


def serialize_manifest(manifest: Mapping[str, Any]) -> str:
    """Canonical JSON serialization of an already-lawful manifest. Routes
    through validate_manifest so the write path can never emit what the law
    refuses -- serialize IS validate."""
    validated = validate_manifest(manifest)
    return json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _atomic_write_manifest_text(path: str, text: str) -> None:
    """Same temp-file + fsync + os.replace() idiom as core.generation_
    artifact._write_atomic()/core.secrets._save() -- a crash or exception
    mid-write can never leave a half-written manifest at `path`, and an
    existing manifest at `path` is left byte-for-byte untouched unless the
    full write+fsync succeeds. `refuse_if_production_path()` is the same
    TEST-ISOLATION backstop every other durable-write path in this repo
    already calls, and is left to raise its own RuntimeError uncaught (it
    is a test-safety guard, not a durability failure)."""
    refuse_if_production_path(path)
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        # Filesystem error text (a path, an errno string) is not secret-
        # shaped, but redact_secret_shapes() is run anyway as the same
        # defense-in-depth every other diagnostic path in this codebase
        # already applies -- never trust an OS-supplied string by default.
        detail = redact_secret_shapes(f"{type(exc).__name__}: {exc}")
        raise ManifestPersistenceError([
            f"failed to persist manifest durably: {detail}"
        ]) from exc


def persist_manifest(manifest: Mapping[str, Any]) -> str:
    """Validate, then atomically persist ONE manifest as the durable JSON
    sibling of its GeneratedArtifact's own bytes -- the production answer
    to the gap MULTIMODAL-M4-HIGGSFIELD-LIVE-SMOKE-02 exposed: build_
    manifest() alone leaves a validated manifest sitting only in memory.

    Storage design (MULTIMODAL-M4-GENERATION-MANIFEST-PERSISTENCE-01):
    core.generation_artifact.artifact_manifest_path(artifact_id) -- a
    sibling of the artifact's own bytes, under the exact same DATA_DIR/
    sharding convention (see that function's docstring). No second storage
    root, no database table or column: the manifest's own artifact_id
    field (already required and validated) is the only identity needed to
    both write and later rediscover this file, restart-independent.

    Routes through validate_manifest() on every call, regardless of
    whether `manifest` already came from build_manifest() -- "never
    persist an unvalidated pre-manifest object" is enforced here too, not
    just trusted from an earlier call. Returns the absolute local path
    written. Raises validate_manifest()'s own ManifestValidationError
    subclasses UNCHANGED for a lawfulness refusal (never caught or
    reinterpreted here); raises ManifestPersistenceError -- a distinct,
    non-validation exception -- for a filesystem failure on an otherwise
    lawful manifest. Never silently swallows either."""
    from core.generation_artifact import artifact_manifest_path

    validated = validate_manifest(manifest)
    encoded = json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path = artifact_manifest_path(validated["artifact_id"])
    _atomic_write_manifest_text(path, encoded)
    return path
