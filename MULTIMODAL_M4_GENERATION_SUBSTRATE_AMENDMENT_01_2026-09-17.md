# MULTIMODAL-M4-GENERATION-SUBSTRATE-AMENDMENT-01, 2026-09-17

**Mode: bounded production implementation.** Two substrate amendments, and only
these two, implemented per owner authorization following
`MULTIMODAL-M4-HIGGSFIELD-API-VET-01`'s findings. No Higgsfield adapter, no
network/HTTP/SDK code, no credentials, no UI/config/main.py changes.

**Audit trail, preserved on purpose:** the original substrate design
(`MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01`, commit `214ae86`) assumed one
`GenerationJob` produces at most one `GeneratedArtifact`, and shipped no
cancellation seam at all. Both assumptions were reasonable given what was known
at the time — neither Higgsfield's real contract nor any other provider's had
been read yet. `MULTIMODAL-M4-HIGGSFIELD-API-VET-01` (same day) found real,
primary-source evidence that both were wrong. This document records the
correction as an *amendment*, not a rewrite of that history — the original
module docstrings still say what the original design assumed; new paragraphs,
clearly labeled "Amendment," explain what changed and why, immediately
alongside the original text.

Before writing any code, both evidentiary claims from the API-vet report were
independently re-verified directly against the raw cached `openapi.json` still
in the session's scratchpad (not re-trusted from the report's own prose):

```
nano-banana num_images: {'type': 'integer', 'default': 1, 'maximum': 4, 'minimum': 1}
RequestStatus.images: {'type': 'array', 'items': {'$ref': '#/components/schemas/MediaOutput'}}
RequestStatus.audios: {'type': 'array', 'items': {'$ref': '#/components/schemas/MediaOutput'}}

cancel path present: True
{"202": {"description": "The request was canceled."},
 "400": {"description": "The request has already started and can no longer be canceled.", ...},
 "401": {...}, "404": {...}}
```

Neither claim was weaker than the report's summary implied. Implementation
proceeded without needing to stop and report a gap.

---

## 1–3. Evidence, old assumption, corrected invariant

### Amendment A — multi-artifact support

- **Exact external evidence:** `openapi.json`'s `/nano-banana` and
  `/higgsfield-ai/soul/standard` (and other) request schemas declare
  `num_images` with `minimum: 1, maximum: 4, default: 1`. The shared
  `RequestStatus` component schema declares `images` and `audios` as
  **arrays** of `MediaOutput`, not singular objects. A single provider
  `request_id` can therefore complete with 2–4 independent output artifacts in
  one terminal event — confirmed directly against the primary spec, not
  inferred from prose.
- **Old assumption:** `core/generation_artifact.py`'s `ingest_artifact()`
  called `list_artifacts_for_job()` and raised `IngestionError` if any
  artifact already existed for the job — enforcing at most one artifact per
  job.
- **Corrected invariant:**
  ```
  GenerationJob
      1
      |
      L-- 0..N GeneratedArtifact
  ```
  A job may own zero, one, or many artifacts. Every artifact keeps its
  existing `lumina_job_id` back-reference (unchanged — the foreign key was
  never `UNIQUE`, so the database already modeled one-to-many; only the
  application-level refusal was wrong). Each artifact retains its own stable
  Lumina identity (`artifact_id`, a freshly-minted UUID per call, unchanged).
  Ingestion success/failure is evaluated **per artifact** — one sibling's
  ingestion failure never erases or invalidates a previously-ingested
  sibling, because each `ingest_artifact()` call is independent from the
  first byte written to the last row committed.

  **`ingestion_state` became semantically dishonest and was corrected — the
  smallest additive fix, not a redesign:** with the one-artifact guard
  removed, the job-level `ingestion_state` field (which used to unambiguously
  mean "the one artifact this job produces") could otherwise get silently
  overwritten by each new ingestion attempt — an `INGESTED` job could flip to
  `FAILED` the moment a *second* output failed, erasing the true fact that
  the first one succeeded. The fix is one new enum value,
  `INGESTION_PARTIAL`, and a merge rule in
  `core.generation_job.set_ingestion_result()`: same outcome repeated stays
  that outcome; a differing outcome (or a job already `PARTIAL`) becomes/stays
  `PARTIAL`. Once mixed, always mixed, for that job's life. This preserves
  the original design's core law — provider lifecycle ≠ local ingestion
  lifecycle ≠ review lifecycle — and extends it rather than replacing it: a
  job can now be `status=succeeded, ingestion_state=partial`, distinguishable
  from both `ingested` (every attempt so far succeeded) and `failed` (every
  attempt so far failed). Nobody can see `ingestion_state=ingested` and
  assume *every* expected output exists — `list_artifacts_for_job()` remains
  the actual source of truth for which ones do.

  **`provider_output_index` was added, after concluding it was actually
  necessary, not merely convenient:** `created_at` has only second-level
  precision (`core.generation_job`'s existing `_utcnow_iso()` convention,
  unchanged), so several artifacts ingested for one job within the same
  second have no deterministic order from that column alone. Nothing else in
  the schema distinguishes "this was output #0" from "output #2" of the same
  job. `provider_output_index` is a **Lumina-assigned**, optional, zero-based
  integer ordinal — never a provider-native identifier, since Higgsfield's own
  `MediaOutput` schema is exactly `{"url": ...}` with `additionalProperties:
  false` and supplies no id or index of its own. It is not validated for
  uniqueness or contiguity at the substrate level; that discipline belongs to
  whichever caller knows the full output set (the future adapter/service
  layer), not to this primitive. `list_artifacts_for_job()`'s ordering was
  extended to `created_at, provider_output_index, artifact_id` so retrieval
  is fully deterministic regardless of same-second ingestion or a caller that
  never sets the ordinal at all.

  **No dedup-by-identical-bytes guard was added.** The task's requirement was
  "duplicate/repeated ingestion cannot silently corrupt an existing
  artifact," not "must be blocked." Since every `ingest_artifact()` call
  mints its own `artifact_id` and `local_path` regardless of the guard that
  was removed, two calls with identical bytes were already structurally
  incapable of corrupting or overwriting each other — proven by test, not
  merely asserted (see §6). Adding a new sha256-based dedup rule beyond that
  would have been unrequested complexity; per "make the smallest additive
  correction needed," it was left out.

### Amendment B — cancellation seam

- **Exact external evidence:** `POST /requests/{request_id}/cancel` is a
  fully OpenAPI-specified endpoint — `202` ("The request was canceled"), `400`
  ("already started and can no longer be canceled"), `401`, `404` — also
  wrapped by the official Python SDK's `cancel()` method on every one of
  `SyncRequestController`, `AsyncRequestController`, `SyncClient`, and
  `AsyncClient` (`http/client.py`, read directly during the API-vet pass).
- **Old assumption:** `core/generation_adapter.py`'s `GenerationAdapter`
  Protocol had five methods (`submit`, `poll`, `fetch_result`,
  `describe_model`, `estimate_cost`) and no way to request cancellation at
  all, even though `core/generation_job.py`'s `STATUS_CANCELLED` already
  existed as a first-class terminal status with nothing able to reach it
  through the adapter seam.
- **Corrected invariant:** `GenerationAdapter.cancel(self, job: GenerationJob)
  -> GenerationJob` — the preferred shape specified in the authorization,
  confirmed compatible with live source rather than contradicted by it (the
  real endpoint returns no job body on success, so an adapter needing to
  report *something* durable has to consult the job's own fields and write
  through `core.generation_job.update_status()` itself; the other four
  methods' "return raw provider data, let the caller mutate state" pattern
  doesn't fit a method whose whole point is producing a trustworthy,
  already-durable outcome). The contract, encoded in the Protocol's
  docstring and demonstrated by `FakeAdapter.cancel()`:
  - Cancellation is provider work. An implementation must not set
    `STATUS_CANCELLED` as a local shortcut before the provider's response
    actually supports it.
  - A job already in `TERMINAL_STATUSES` (succeeded/failed/cancelled) is
    refused — `core.generation_job.JobStateConflict` — **before** any
    provider contact is attempted, not after. Reused the existing exception
    type rather than inventing a new one.
  - If the provider only acknowledges the request without confirming a
    terminal outcome, the job may legitimately remain in its existing
    non-terminal status; a later `poll()` is what eventually resolves it,
    exactly like any other status change — no new intermediate status was
    added for this (see below).
  - Whatever the provider reports is mapped through the same
    `normalize_provider_status()` fail-closed path `poll()` results already
    use. An unrecognized response becomes `STATUS_UNKNOWN`, never assumed to
    mean cancelled.
  - **`cancel_requested` was deliberately NOT added to the canonical status
    vocabulary.** The real, re-verified Higgsfield cancel endpoint doesn't
    even have an intermediate acknowledged-but-pending state — it's
    synchronous confirm-(`202`)-or-reject-(`400`). The substrate is
    provider-neutral, so the *contract* still accommodates a hypothetical
    provider that behaves asynchronously (via "the job just stays in its
    current non-terminal status," representable with the existing
    `queued`/`running` values), but nothing in any evidence gathered forces
    a new vocabulary entry, so none was added, per the explicit instruction
    not to invent one without the provider contract forcing it.

  `core/generation_job.py` required **zero changes** for this amendment —
  `update_status()` already permitted any non-terminal→any-status transition
  generically, including into `STATUS_CANCELLED` or `STATUS_UNKNOWN`, and
  already refused transitions out of a terminal status. The gap was entirely
  in the adapter seam, not the job state machine.

---

## 4. Exact source files changed

- `core/generation_job.py` — module docstring amendment paragraph;
  `INGESTION_PARTIAL` constant + `_MERGEABLE_INGESTION_STATES`;
  `set_ingestion_result()` rewritten as a merge instead of a blind overwrite.
  No schema/table changes, no new exported function, no change to
  `begin_generation_job`, `mark_submitted`, `update_status`, `bump_retry`,
  `record_cost_actual`, `get_generation_job`, `find_unresolved_by_fingerprint`,
  or `list_generation_jobs`.
- `core/generation_artifact.py` — module docstring amendment paragraph;
  `GeneratedArtifact.provider_output_index` field; `generation_artifacts`
  table gains one nullable `provider_output_index INTEGER` column (additive,
  no migration of existing rows needed since this is a pre-release schema
  with no production data yet); `ingest_artifact()`'s "already has an
  ingested artifact" refusal removed, `provider_output_index` parameter
  added and validated; `list_artifacts_for_job()`'s `ORDER BY` extended for
  determinism.
- `core/generation_adapter.py` — module docstring amendment paragraph; one
  new import (`from core.generation_job import GenerationJob`, no circular
  import — confirmed `generation_job.py` imports nothing from this module);
  one new Protocol method, `cancel()`, with contract documented in its
  docstring. No other method's signature changed.
- `tests/test_multimodal_m4_generation_substrate_01.py` — `FakeAdapter`
  gained `cancel()` and `configure_cancel()`; three new job-state helper
  functions (`_queued_job`, `_running_job`, `_failed_job`);
  `test_ingest_refuses_second_ingestion_for_same_job` removed (the law it
  proved is no longer true — replaced, not silently deleted, by the tests in
  §6); 16 net new tests added.

No other file was touched. `core/capability_router.py` (M1), `ui/`,
`config.py`, `config.example.py`, and `main.py` all show zero diff —
confirmed by `git diff --stat` immediately before writing this document, not
assumed.

---

## 5. Migration / backward-compatibility impact

**Fully backward-compatible; no migration required.**

- The `generation_artifacts` table's new column is nullable and additive —
  `ALTER TABLE ... ADD COLUMN` semantics via `CREATE TABLE IF NOT EXISTS`'s
  existing idempotent pattern would be needed only if a production database
  with the old schema already existed; none does yet (this substrate has
  never shipped to a real adapter or real user data — commit `214ae86` is the
  only prior state, and it was never given a live provider to populate real
  rows).
- Every existing caller that ingests exactly one artifact per job (the common
  case) sees unchanged behavior: one `ingest_artifact()` call, one row,
  `ingestion_state` becomes `INGESTED` or `FAILED` exactly as before — the
  merge logic degenerates to the old blind-overwrite behavior when there is
  only ever one outcome to merge.
- `typing.Protocol` is structural — adding `cancel()` cannot break any
  existing caller that doesn't reference it, and none in this codebase does
  yet (no real adapter exists).
- The one intentionally-breaking change is `test_ingest_refuses_second_ingestion_for_same_job`
  no longer passing if it still existed — it was removed because the law it
  asserted is no longer true, not preserved-but-skipped.

---

## 6. Tests proving the new laws

`tests/test_multimodal_m4_generation_substrate_01.py`, 77 tests total (61
inherited unchanged + 1 removed + 17 new — net +16):

**Multi-artifact (9 new):**
`test_one_job_one_artifact_is_still_the_common_case`,
`test_one_job_many_artifacts_all_succeed`,
`test_one_sibling_fails_another_succeeds_neither_corrupts_the_other`,
`test_ingestion_state_partial_regardless_of_attempt_order`,
`test_ingestion_state_stays_partial_once_mixed`,
`test_set_ingestion_result_rejects_partial_as_caller_input`,
`test_repeated_ingestion_of_identical_bytes_does_not_corrupt_the_original`,
`test_multi_artifact_relationship_survives_a_fresh_connection` (bypasses this
module's own functions entirely and re-queries the on-disk SQLite file via a
brand new raw `sqlite3.connect()`, the closest simulation of a real process
restart available to this test suite), plus the pre-existing
`test_ingest_requires_succeeded_job` (unchanged, still proves a non-succeeded
job cannot write artifact bytes — no new test needed since that law didn't
change).

**Cancellation (9 new):** `test_cancel_queued_job_provider_confirms_terminal`,
`test_cancel_running_job_provider_confirms_terminal`,
`test_cancel_running_job_rejected_because_already_processing`,
`test_cancel_accepted_but_not_yet_terminal`,
`test_cancel_terminal_succeeded_job_refused`,
`test_cancel_terminal_failed_job_refused`,
`test_cancel_already_cancelled_job_refused`,
`test_cancel_unknown_provider_response_fails_closed`,
`test_fake_adapter_cancel_is_fully_offline` (patches `socket.socket` to raise
and runs a full cancel lifecycle through `FakeAdapter`, proving — not just
asserting — zero network calls occur).

Every required test bullet from the authorization is covered by name above,
one-to-one.

---

## 7. No Higgsfield adapter/provider code was added

**Explicit statement:** this amendment added zero HTTP client code, zero
network imports at module scope, zero credential handling, zero
Higgsfield-specific request/response parsing, zero new dependencies, and zero
UI/settings/config changes. `FakeAdapter` in the test file is the only
`GenerationAdapter` implementation that exists anywhere in this codebase —
in-memory, deterministic, proven offline by test. Every mention of
"Higgsfield" in the diff (confirmed by grep before writing this document) is
a docstring or comment citing the evidence that forced a change, never
executable code that talks to a provider. `MULTIMODAL-M4-HIGGSFIELD-API-VET-01`'s
own recommended next step — the real adapter, built against a still-unread raw
HTTP/SDK contract for the parts not covered by this amendment — remains
untouched and unstarted.
