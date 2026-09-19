# Skill: generated-artifact-manifest

**Description:** Govern and inspect Lumina's durable generated-artifact provenance — canonical 17-field manifest law (L1–L10), ingestion/review truthfulness, provider_output_index semantics, no-secret rule. Provider-neutral governance; never a generation engine.

## Provenance

- source: Lumina Creative Skills Pack draft (2026-09-19) + live source-vet of landed manifest law
- authored_by: Lumina Prime
- ownership_class: OFFICIAL
- pack: Lumina Creative Studio
- scope: provider-neutral governance (all capabilities, all providers)
- authority: describes landed law (`core/generation_manifest.py`, `core/generation_job.py`, `core/generation_artifact.py`); it does not define, extend, or re-implement that law

## USE WHEN

- Inspecting, auditing, or reasoning about a generated artifact's provenance.
- Answering "is this artifact durable, truthful, reviewed?" or "where did this come from?"
- Explaining manifest law to the owner or team (craft-night governance).
- Reviewing whether a manifest is lawful before trusting a delivery.
- Interpreting or auditing recorded review outcomes on generated artifacts.

## NOT FOR

- Generating anything. This skill never submits, polls, or produces media — generation belongs to the capability lane's own skill (e.g. `media-generation`).
- Re-implementing, patching, or working around manifest validation.
- Adding provider vocabulary entries. `CANONICAL_PROVIDERS` changes only by deliberate owner-authorized production edit during that provider's integration rollout — never to make a failing check pass.
- Handling credentials, Authorization headers, signed/presigned URLs, or secret-bearing provider metadata. Secret material in a manifest is REFUSED, not redacted.

## What a manifest is

Fully successful artifact delivery requires one canonical durable manifest per
GeneratedArtifact. The manifest is the canonical durable per-artifact provenance
record; durable job and artifact records also participate in provenance. A
manifest_failed outcome may truthfully leave durable artifact bytes without a
durable manifest. Canonical field set — 17 fields, exact: every key must be
present; optional values carry None explicitly; absence of a KEY is always a
violation.

| field | meaning |
|---|---|
| `capability` | canonical M1 capability name |
| `provider` | registered provider/manifest identity |
| `model` | model identifier actually used |
| `lumina_job_id` | Lumina-minted correlation id |
| `provider_job_id` | provider's own request/job id (reattachment) |
| `artifact_id` | GeneratedArtifact.artifact_id |
| `artifact_path` | LOCAL path — a remote URL is never durable |
| `provider_output_index` | Lumina-assigned ordinal (int >= 0) |
| `job_ingestion_state` | VERBATIM snapshot of job.ingestion_state |
| `review_state` | submitted \| completed \| reviewed |
| `cost_estimate` | provider estimate or None |
| `cost_actual` | actual charge or None — separate field, never merged |
| `cost_unit` | required when either cost field is present |
| `authorization_ref` | owner budget-envelope/job-class ref |
| `created_utc` | tz-aware ISO-8601 |
| `params` | mapping echo of generation settings (secret-scanned, <= 16 KB) |
| `failure_class` | provider failure class or None |

## The ten laws (as landed, each test-proven)

- **L1 exact set** — the 17 fields above; nothing missing, nothing extra.
- **L2 separate axes** — capability and provider are independent fields; a provider is not a capability.
- **L3 provider fails closed** — `provider` must belong to the current `CANONICAL_PROVIDERS` registry (deliberately registered, one entry per provider integration rollout); an unknown provider is refused, and the refusal is the guard working, not a bug.
- **L4 capability fails closed** — `capability` must be in the frozen M1 vocabulary: vision_understanding, image_generation, audio_understanding, speech_synthesis, video_understanding, video_generation.
- **L5 authorization_ref** — required whenever the artifact is cost-bearing. A blank/whitespace ref on a cost-bearing artifact IS a missing ref (substance over key-presence).
- **L6 no extra fields** — the exact set is the law.
- **L7 no aggregate success field** — `review_state` and `job_ingestion_state` are independent observables; "reviewed + partial" is lawful. Never collapse them into a success claim.
- **L8 ingestion snapshot verbatim** — `job_ingestion_state` mirrors the job's ingestion_state exactly; the manifest builder has no override parameters for ingestion, capability, model, or cost.
- **L9 estimate ≠ actual** — `cost_estimate` and `cost_actual` are two fields, never summed, never merged. Estimate differing from actual is expected and honest.
- **L10 no secret material** — key-shaped params keys and secret-shaped values in free text or params are REFUSED outright, not redacted. The manifest never becomes a secret carrier.

## Lifecycle truthfulness

Three events on three axes, never collapsed:

1. **Provider completion** — job status: queued / running / succeeded / failed /
   cancelled / unknown; terminal = succeeded, failed, cancelled.
2. **Local ingestion** — pending / ingested / failed / partial. `partial` is a
   derived honest aggregate when sibling outputs had mixed ingestion outcomes;
   once mixed, always mixed. It is never a caller-supplied value.
3. **Review state** — review_state ∈ {submitted, completed, reviewed}.

A job can truthfully be status=succeeded with ingestion_state=partial. That is a
first-class observable state, not an error to smooth over.

## provider_output_index semantics

- Lumina-assigned ordinal for one output within a job's output set: int >= 0.
- Booleans are rejected (a bool is not an ordinal).
- An artifact without an assigned index cannot be manifested.
- Multi-output jobs keep every output: every discovered output is accounted for
  and processed independently; no discovered output is silently discarded or
  collapsed into another.

## Inspection procedure

When reviewing an artifact or manifest:

1. Confirm the manifest validates against the exact field set. Every validation
   refusal is fail-closed — nothing is coerced, defaulted, upgraded, or dropped.
2. Confirm `artifact_path` is a LOCAL filesystem path that exists. A remote URL as
   artifact_path violates the law (remote URLs are transport, never durable identity).
3. If `cost_estimate` or `cost_actual` is present, confirm `cost_unit` is present,
   and confirm `authorization_ref` is present and non-blank for cost-bearing artifacts.
4. Read `review_state` and `job_ingestion_state` independently; report both; never
   summarize them into a single "done".
5. Confirm `params` carries no secret-shaped material and is within the 16 KB cap.
6. Cross-check `lumina_job_id` / `provider_job_id` against the job record when
   tracing provenance end-to-end.

Red flags (report, never fix silently): missing authorization_ref on a cost-bearing
artifact · aggregate "success" claims · remote URL as artifact_path · secret-shaped
material · missing/extra fields · unassigned provider_output_index · tz-naive
created_utc · params over cap.

## Never include (hard exclusions)

Credentials · Authorization headers · signed/presigned URLs · secret-bearing
provider metadata. If provider metadata carries such material, the manifest is
refused — report the refusal; do not strip-and-proceed.

## Boundary

This skill is governance knowledge. It does not execute generation, does not persist
manifests itself (persistence belongs to the generation service's ingestion path),
and does not modify the provider vocabulary. When the law itself needs to change,
that is a deliberate owner-authorized production edit: flag it, propose it, never
apply it from inside a skill.

## Verification

- Any manifest you cite has been validated against the landed law, not assumed.
- Provenance claims trace to lumina_job_id + artifact_id, not to chat memory alone.
- Review-state interpretations preserve all three axes independently; never
  back-date, aggregate, or upgrade a review state.