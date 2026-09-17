# MULTIMODAL-M4-HIGGSFIELD-ADAPTER-01, 2026-09-17

**Mode: bounded production implementation. Adapter only.** The first real
`core.generation_adapter.GenerationAdapter` implementation, against
Higgsfield's server-credential REST API only. No CLI shell-out, no
device-flow auth, no live/paid call, no service layer, no UI/Settings, no
M5+.

**Audit trail, preserved on purpose:** source-vet
(`MULTIMODAL-M4-HIGGSFIELD-API-VET-01`) established the real contract →
substrate amendment (`MULTIMODAL-M4-GENERATION-SUBSTRATE-AMENDMENT-01`)
corrected two wrong assumptions the contract disproved → this campaign is
the first thing that actually *talks* to that contract. Nothing below
claims the earlier documents anticipated this adapter's exact shape; two
gaps were found only now, during real implementation, and are recorded as
findings in §0, not folded silently into the earlier history.

---

## 0. Pre-implementation gate — what was verified, and two findings before writing code

**Both prior documents read in full; the four currently-landed substrate
files and `core/capability_router.py` re-read fresh.**

**Independent live re-check, not a re-read of the prior report's prose:**
`https://docs.higgsfield.ai/docs/openapi.json` was fetched fresh and
byte-diffed against the API-vet campaign's same-day cache —
**identical, zero drift.** `authentication.md`, `concepts/errors.md`, and
`concepts/billing-and-retention.md` were independently re-fetched and
diffed the same way — all three **identical, zero drift**. The evidence
base this adapter is built against is confirmed current, not stale; no
STOP condition was triggered.

**Two Protocol-boundary gaps were found during design, before any HTTP
code was written, and resolved without touching
`core/generation_adapter.py`:**

1. **`fetch_result(provider_job_id) -> Tuple[bytes, str]` predates the
   multi-artifact amendment and can only return one output.** A completed
   Higgsfield job can have 0–4 outputs (the exact fact the amendment
   campaign already corrected at the `GeneratedArtifact` layer, but never
   reconciled at the adapter-Protocol layer — its own accounting explicitly
   says "No other method's signature changed"). Silently returning only
   the first output and discarding the rest would be a real, load-bearing
   bug. **Resolution:** `fetch_result()` stays Protocol-compliant and
   degenerates cleanly for the common single-output case; it raises
   `MultipleOutputsError` — pointing at `list_outputs()`/`fetch_output()`
   — when a job actually has more than one output, rather than silently
   dropping data. The richer, fully multi-output-aware path is
   implemented and tested as `HiggsfieldAdapter`-specific methods, not a
   Protocol addition. Promoting this into the shared Protocol is deferred
   until a second provider's real shape proves it's the right
   generalization — not assumed from Higgsfield alone. This was a genuine
   candidate for the "STOP and report" gate the campaign brief specified
   for the cost-estimate seam; a smaller, substrate-preserving design was
   found instead, so implementation continued rather than blocking.
2. **`poll(provider_job_id) -> str` (Protocol-mandated) can't carry a
   `failure_class`,** but the brief explicitly requires `nsfw` to map to
   `status=failed, failure_class=content_policy` — two pieces of
   information a bare string return cannot hold. **Resolution:**
   `poll()` stays Protocol-compliant, returning the bare raw provider
   string exactly as documented. `poll_detailed()` (Higgsfield-specific,
   not Protocol-mandated) returns a small `PollResult` with
   `raw_status`/`canonical_status`/`failure_class`/`error_detail` and is
   what actually performs the `nsfw` → `failed`/`content_policy`
   translation. Same non-Protocol-addition pattern as finding 1.

`estimate_cost(self, *, model: str, settings: dict) -> Optional[float]`
already existed in the Protocol from the original substrate design and
needed no change at all — Higgsfield's `/estimate/{model}` maps onto it
directly. No STOP was needed there.

**No other substrate file needed to change.** `core/generation_job.py`,
`core/generation_artifact.py`, `core/generation_spending_policy.py`, and
`core/capability_router.py` (M1) all show zero diff for this campaign —
confirmed by `git diff --stat` before writing this document, not assumed.

---

## 1. Exact official contract implemented

- **Base URL:** `https://api.higgsfield.ai`.
- **Submit:** `POST {model_path}` (the model path itself is the endpoint,
  e.g. `POST /nano-banana`) → `{status, request_id, status_url, cancel_url}`.
- **Poll:** `GET /requests/{request_id}/status` → `RequestStatus` schema
  (`status`, `request_id`, `error`, `images`/`video`/`audio`/`audios`).
- **Cancel:** `POST /requests/{request_id}/cancel` → `202` (confirmed
  canceled, no body) / `400` (already processing, rejected) / `401` / `404`.
- **Estimate:** `POST /estimate{model_path}` (undocumented in the formal
  OpenAPI spec and not SDK-wrapped, but a real, documented, example-backed
  endpoint) → `{"credits": "<str>", "usd": "<str>"}`.
- **Upload:** `POST /files/generate-upload-url` → `{public_url, upload_url,
  content_type, upload_headers}`, then `PUT` raw bytes to `upload_url`
  with exactly `upload_headers` (no Higgsfield credentials).
- **Auth:** `Authorization: Key {key_id}:{key_secret}` on every
  Higgsfield-hosted call; never sent to the presigned upload URL or to an
  output CDN URL.

## 2. Sources/versions used

- `https://docs.higgsfield.ai/docs/openapi.json` — OpenAPI 3.1.0,
  `info.version=2.0.0`. Fetched during the API-vet campaign (2026-09-17)
  and re-fetched immediately before this module was written (also
  2026-09-17) — byte-identical both times.
- `authentication.md`, `concepts/errors.md`,
  `concepts/billing-and-retention.md` — re-fetched and diffed the same
  way, identical.
- `concepts/requests.md`, `concepts/file-uploads.md`, `how-to/webhooks.md`
  — read during the API-vet campaign, not independently re-fetched this
  pass (their content doesn't bear on new code decisions beyond what §0
  already re-verified from the spec directly).
- Official Python SDK source (`higgsfield-client`, already cloned
  read-only during the API-vet campaign) — corroboration only, not
  depended on directly; this adapter does not use the SDK package.

## 3. Supported REST model scope

**`nano-banana` and `higgsfield-ai/soul/standard` only** —
`core.higgsfield_adapter.SUPPORTED_MODELS`. Chosen because together they
exercise the two structurally different request shapes the real OpenAPI
spec has for image endpoints: one with a reference-image array
(`input_images`, up to 8) and one prompt-only (no media input at all).

**Intentionally NOT implemented** (source-vet Sec 14 item 1: the CLI's
device-flow-authenticated catalog is materially larger than — and not
proven to be the same surface as — the public REST API): the other
~10 image-shaped REST endpoints (`flux-pro/kontext/max/text-to-image`,
`reve/*`, `higgsfield-ai/soul/character`, `higgsfield-ai/soul/reference`,
`higgsfield-ai/dop/*`, `higgsfield-ai/popcorn/auto`), every video/3D/audio
endpoint (out of M4 scope regardless), and the entire CLI-only catalog
(23 image models, Marketing Studio, Virality Predictor, 3D, audio — none
of it proven against server credentials). `describe_model()` returns
`None` for any of these rather than a fabricated description —
`test_describe_model_never_claims_cli_only_catalog` asserts this
explicitly for two named CLI-only models.

`describe_model()`'s catalog is adapter-owned (`_MODEL_CATALOG` +
`CATALOG_SOURCE`), derived directly from the OpenAPI contract, source and
version stamped in every `describe_model()` return value — no live
per-model discovery call exists on the real REST surface (confirmed
absent in the API-vet pass), so a documented, versioned mapping is the v1
answer per the campaign brief.

## 4. Auth handling

Credentials sourced from `core.secrets.get_secret("higgsfield_key_id")` /
`get_secret("higgsfield_key_secret")` — the existing, already-established
OS-local credential store this project already uses for cloud API keys
(`~/.config/lumina/credentials.json`, 0600, deliberately separate from
`prefs.json`). No `config.py` constant, no Settings UI, no new storage
mechanism was added — reusing the existing boundary meant no STOP was
needed for this section either. `HiggsfieldAdapter(transport=...)` accepts
an injected transport for tests/callers that construct their own
credentials differently; production code with no explicit transport gets
a real `RequestsHiggsfieldTransport` built from `core.secrets`.

Never logged, repr'd, or embedded in any exception/provenance string:
`RequestsHiggsfieldTransport.__repr__` is overridden to print `[REDACTED]`
instead of the constructed Authorization header;
`test_credentials_never_in_transport_repr`,
`test_credentials_never_in_adapter_repr`, and
`test_credentials_never_in_error_message` prove this against the *real*
transport class (with real-shaped credential strings, all HTTP calls
monkeypatched), not just the fake test double.

## 5. Request serialization

Settings validated client-side against the adapter-owned catalog before
any HTTP call: unknown parameters, missing required parameters, and
out-of-range/enum-violating values all raise `UnsupportedParameterError`
immediately, with zero calls recorded on the transport
(`test_submit_unsupported_parameter_fails_explicitly` asserts
`transport.calls == []`). `provider_idempotency_key`, if a caller supplies
one, raises rather than being silently discarded or sent — Higgsfield's
real API has no such field anywhere in the spec, SDK source, or docs.
Reference assets are resolved through the presigned-upload flow (§9) and
merged into the payload under the model's own declared parameter name
(`input_images` for `nano-banana`) as
`{"type": "image_url", "image_url": <public_url>}` entries, matching
`ImageUrlInputImageSchema` exactly.

## 6. State translation table

| Higgsfield raw `status` | `poll()` (bare) | `poll_detailed().canonical_status` | `failure_class` |
|---|---|---|---|
| `queued` | `"queued"` | `STATUS_QUEUED` | — |
| `in_progress` | `"in_progress"` | `STATUS_RUNNING` | — |
| `completed` | `"completed"` | `STATUS_SUCCEEDED` | — |
| `failed` | `"failed"` | `STATUS_FAILED` | `"provider_error"` |
| `nsfw` | `"nsfw"` | `STATUS_FAILED` | `"content_policy"` |
| `canceled` | `"canceled"` | `STATUS_CANCELLED` | — |
| anything else | raw string, unchanged | `STATUS_UNKNOWN` | — |

`poll()` never invents a translated string — it always returns exactly
what the provider sent. The `nsfw` special case lives entirely in
`poll_detailed()`/`_canonical_status()`, never in the shared
`core.generation_job.normalize_provider_status()` synonym table (kept
provider-agnostic, unmodified). Every row above is covered by
`test_poll_every_documented_state_mapping` (parametrized) plus
`test_poll_nsfw_maps_to_failed_content_policy` and
`test_poll_unknown_state_never_guessed`.

**Restart/reattach, mandatory per the brief:** both `poll()` and
`poll_detailed()` take only `provider_job_id` and hold no in-memory
submission state — `test_poll_reattach_without_original_submission_call`
constructs a fresh `HiggsfieldAdapter` that never called `submit()` and
polls a job by id alone.

## 7. Cancellation semantics

`cancel(job) -> GenerationJob`:

| Provider response | Adapter behavior |
|---|---|
| `202` (documented: "The request was canceled") | `core.generation_job.update_status(STATUS_CANCELLED)` |
| `400` (documented: already processing) | job returned **unchanged** |
| any other 2xx (undocumented for this endpoint) | job returned **unchanged** — "accepted but not confirmed the documented way"; a later `poll()` resolves it |
| `>=400`, any status not explicitly handled above | raises `HiggsfieldProviderError` (`kind=cancellation_error` when otherwise unclassified) — **fails closed**, job untouched |
| `job.status` already terminal | raises `core.generation_job.JobStateConflict` **before any HTTP call** |

Higgsfield's real cancel endpoint is a synchronous confirm-or-reject with
no documented "accepted, still pending" body shape — the middle row above
is the adapter's honest, provider-neutral answer for a response the real
contract doesn't define, not a fabricated Higgsfield behavior. No
`cancel_requested` status was added to the canonical vocabulary — nothing
forces one. No background cancellation worker exists.

## 8. Cost-estimate handling

`estimate_cost(model, settings)` fails closed to `None` on every failure
mode that isn't a caller-side bug: network error, HTTP error status,
malformed JSON, or a missing `usd` field. A genuinely invalid request
(unknown model/parameter) still raises — that's not "the provider's
estimate is unavailable," it's the same request that would fail at
`submit()` too, and staying silent about it would hide a real mistake.
The adapter **never evaluates `GenerationSpendingPolicy` itself** — it
returns a number or `None`; `test_estimate_never_autonomously_approves_spend`
proves a policy evaluated by the caller is what actually gates the
decision, with the adapter having no opinion of its own.

## 9. Upload/reference handling

Every `reference_assets` entry's `artifact_ref` is treated as a Lumina
`GeneratedArtifact.artifact_id` — **never** a raw URL, **never** a
filesystem path. The adapter reads already-ingested, hash-verified bytes
via `core.generation_artifact.get_artifact_bytes()`, uploads *those*
through the documented two-step presigned flow, and only ever passes the
resulting `public_url` (never a `local_path`) into the provider request —
`test_upload_no_local_path_leaked_into_final_request` serializes every
recorded transport call and asserts the artifact's own `local_path` string
never appears anywhere in them. The presigned `PUT` never carries the
Higgsfield `Authorization` header —
`test_upload_presigned_put_never_receives_higgsfield_auth_header` proves
this against the real transport class. Models with `reference_role=None`
(`higgsfield-ai/soul/standard`) reject any reference asset outright;
reference counts beyond a model's declared maximum are rejected before
any upload attempt. Lumina performs no automatic arbitrary-URL fetching —
the only URLs this adapter ever fetches are ones a completed job's own
provider response returned (§10), never a caller-supplied or
externally-sourced URL.

## 10. Multi-output handling

`list_outputs(provider_job_id) -> List[HiggsfieldOutputRef]` discovers
every output a succeeded job has — parsing `images` (array), `video`
(single), `audio` (single), `audios` (array) in that field order, which
sets the deterministic combined index — **without fetching any bytes**.
`fetch_output(provider_job_id, index)` fetches exactly one output's bytes
by that index; `index` is exactly the value a caller passes as
`provider_output_index` to `core.generation_artifact.ingest_artifact()`.
A malformed individual output entry (missing/non-string `url`) raises
`HiggsfieldMalformedResponseError` rather than being silently skipped —
losing track of billable content the owner paid for was judged worse than
a loud failure. Zero outputs on a succeeded job is valid and returns an
empty list, not an error. MIME type is read from the HTTP response's
`Content-Type` header, never a same-named JSON field (source-vet Sec 9
flagged `webhooks.md`'s example payloads showing a `content_type` field
that the formal `MediaOutput` schema — `additionalProperties: false` —
doesn't actually declare; this adapter never depends on it existing).
Fetched bytes are returned as plain `(bytes, mime_type)` — this module
**never calls `ingest_artifact()` itself**;
`test_remote_url_stays_non_durable_until_ingestion` proves a fetched
output is not yet a `GeneratedArtifact` until a caller deliberately
ingests it.

## 11. Error translation

| HTTP status | `kind` | Notes |
|---|---|---|
| 401 | `auth_failure` | |
| 403 | `insufficient_credits` | |
| 400 | `validation_failure` (or `rate_limited` if the message mentions "concurrent") | Higgsfield's own docs call 400 ambiguous between these two causes |
| 404 | `not_found` | |
| 422 | `validation_failure` | |
| 423 | `model_unavailable` | |
| 429 | `rate_limited` | `Retry-After` header parsed into `.retry_after` (seconds) when present |
| 500 / unmapped 5xx | `provider_error` | |
| 503 | `model_unavailable` | |
| (cancel-specific >=400) | `cancellation_error` when otherwise unclassified | auth/not-found etc. still win when they apply |
| connection failure | `HiggsfieldConnectionError` (transport-level, not an HTTP status) | |
| timeout | `HiggsfieldTimeoutError` (transport-level) | |
| unparseable JSON body | `HiggsfieldMalformedResponseError` | |

Every error message is built only from the provider's own `detail` text
(FastAPI `{"detail": ...}` envelope), run through
`core.redaction.redact_secret_shapes()` as defense in depth. No retry loop
exists anywhere in this module — a caller decides whether and how to
retry; `Retry-After` is preserved as data, never acted on automatically.

## 12. Known API unknowns/gaps (inherited from the API-vet report, not resolved here)

- The CLI-vs-REST catalog gap (source-vet Sec 14 item 1) remains
  unreconciled — this adapter resolves it by scope, not by investigation:
  REST-provable models only.
- Whether a previous `request_id` can be reused as a media reference on
  the real REST API — not established; this adapter never attempts it
  (every reference is a fresh Lumina-owned upload).
- Whether output URLs require any authentication in practice — every
  documented example is an unauthenticated `https://cdn.example.com/...`
  link; this adapter sends no auth header to them (§9) and hasn't been
  proven against a real one.
- Whether `429` actually occurs in practice for the concurrency-limit case
  (docs say 400, no `429`/`Retry-After` currently published; the official
  SDK's retry defaults still include 429 defensively) — this adapter
  handles both a real `400`-with-"concurrent" message and a hypothetical
  `429` correctly, but neither reconciles the discrepancy itself.
- Real, observed behavior of anything above — every test in this campaign
  uses a fake transport; nothing here was confirmed against a live
  response.

## 13. Intentionally unsupported CLI-only behavior

Live model-list discovery (`higgsfield model list`), workflows
(`draw_to_video`, `reframe`, `voice-change`, `dubbing`), Marketing Studio,
Soul ID training, Virality Predictor, 3D/audio/video generation, presets,
voices, games, websites — none of this exists on the documented REST
surface this adapter targets, and none of it is faked or approximated
here.

## 14. Exact files changed

- `core/higgsfield_transport.py` (new) — injectable HTTP transport:
  `TransportResponse`, `HiggsfieldTransport` Protocol,
  `RequestsHiggsfieldTransport` (wraps `requests`, already a project
  dependency — no new third-party dependency added), `post`/`get`/
  `put_bytes`/`get_raw`, three narrow exception types.
- `core/higgsfield_adapter.py` (new) — `HiggsfieldAdapter`
  (`GenerationAdapter` implementation), `_MODEL_CATALOG`/`CATALOG_SOURCE`,
  settings validation, error classification, `PollResult`/
  `HiggsfieldOutputRef`, six exception types, credential resolution via
  `core.secrets`.
- `tests/test_multimodal_m4_higgsfield_adapter_01.py` (new) — 68 tests,
  `FakeHiggsfieldTransport`, full offline coverage.
- `MULTIMODAL_M4_HIGGSFIELD_ADAPTER_01_2026-09-17.md` (new, this file).

**No other file was touched.** `core/generation_job.py`,
`core/generation_artifact.py`, `core/generation_adapter.py`,
`core/generation_spending_policy.py`, `core/capability_router.py`, `ui/`,
`config.py`, `config.example.py`, `main.py` all show zero diff — confirmed
by `git diff --stat`, not assumed.

## 15. Test evidence

- `tests/test_multimodal_m4_higgsfield_adapter_01.py`: **68 passed**, in
  isolation.
- Combined with the M4 substrate suite
  (`tests/test_multimodal_m4_generation_substrate_01.py`, 77 tests):
  **145 passed** together.
- Full release suite: **4373 passed**, run twice, native exit `0` both
  times (~8m12s / ~8m15s) — exactly 4305 (pre-campaign) + 68 (new), no
  unexplained delta either direction.
- Offline law: `test_offline_law_full_adapter_lifecycle_never_touches_a_socket`
  patches `socket.socket` to raise if constructed at all, then drives a
  full submit → poll → list_outputs → fetch_output → estimate_cost →
  cancel lifecycle through `FakeHiggsfieldTransport` alone.

## 16. No live/paid Higgsfield call occurred

**Explicit statement:** no request was made to `api.higgsfield.ai` (or
any Higgsfield-owned endpoint) during this campaign's test runs or
implementation. `core.secrets` (the real, OS-local credential store) was
never read or written by any test — every `HiggsfieldAdapter()`
construction in the test file passes an explicit `transport=`, confirmed
by grep before this document was written; only `_default_transport()`
(never exercised by any test) would have touched it. Bino's own
pre-existing `~/.config/lumina/credentials.json` (unrelated to this
session, from his normal app usage) was independently confirmed
untouched by this campaign's code paths. No key was created or rotated,
no job was submitted against a real account, no media was uploaded to a
real presigned URL, and no estimate was requested against real credits.
