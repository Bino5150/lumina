# Skill: media-generation

**Description:** Generate images through Lumina's native image_generation lane — persisted Multimodal route, spending-policy gate, provider adapter, durable artifact ingestion, canonical manifest. Image-generation subset only; video/audio/3D/reference workflows unsupported.

## Provenance

- source: Lumina Creative Skills Pack draft (2026-09-19) + live source-vet of landed M4 implementation
- authored_by: Lumina Prime
- ownership_class: OFFICIAL
- pack: Lumina Creative Studio
- scope: image_generation ONLY (v1)
- capability_dependencies: image_generation (landed: M1 capability registry, M4 generation substrate, image_generation_service, spending policy, manifest law)

---

## USE WHEN

- The user asks to make/generate/create an image and no specialist creative workflow owns the task.
- A direct one-shot image request: "generate an image of X", "make a picture of Y".
- A model-specific image request the active image_generation route can satisfy.
- The user asks what image generation can do, what it costs, or why a request failed.

## NOT FOR

- YouTube thumbnails / vertical video covers → `thumbnail-studio` (future skill).
- Product photography campaigns, hero shots, carousels → `product-photoshoot` (future skill).
- Brand identity, logos, palettes, type systems → `brand-system` (future skill).
- Marketplace listing imagery / A+-style modules → `marketplace-creative` (future skill).
- Motion films / video production → `motion-design` (future skill).
- Narrated explainers → `video-explainer` (future skill).
- Identity or character model training → `identity-model` (future skill).
- Video, audio, music, or 3D generation — no execution path exists; refuse truthfully.
- Editing, retouching, or upscaling existing images — no landed capability.
- Understanding/analyzing images the user supplies → that is the `vision_understanding` lane, not generation.

When a specialist skill exists and owns the task, route to it; never absorb its job here.

## The only supported execution path

```
user intent
  -> image_generation capability
  -> persisted Multimodal route (owner's settings — never hardcoded)
  -> core.image_generation_service (the ONLY execution seam)
  -> estimate + spending policy gate (BEFORE any submission)
  -> selected provider adapter (GenerationAdapter Protocol)
  -> generation job (submit -> poll -> terminal status)
  -> durable artifact ingestion (local files)
  -> canonical manifest per output (validated, persisted durably)
  -> truthful result returned to Lumina/user
```

The downstream generation-service path is landed. The conversational runtime
handoff from user intent into this path is not yet wired and is covered by the
runtime-wiring campaign. Every landed downstream seam must be used as-is — never
skipped, reordered, or substituted.

## Hard rules (non-negotiable)

1. Provider and model come from the persisted capability route. Never hardcode a provider or model name in procedure, prompt, or code. The route is the owner's decision, changed only by the owner.
2. Never bypass `core.image_generation_service`. It owns routing admission, spending, submission, polling, output discovery, ingestion, and manifest persistence.
3. Never bypass spending policy. Cost-bearing work requires an owner authorization_ref. A denial or approval requirement is a stop, not an obstacle.
4. Never call any vendor CLI. Native service seam only.
5. Submission success is not delivery. Delivery = truthful artifact + manifest outcome. A provider URL alone is never a deliverable.
6. No automatic paid retry without explicit owner authorization. Retry-budget exhaustion is reported truthfully; never work around it with fresh paid submissions.
7. Ambiguous submission acknowledgement never triggers a blind retry. Report the ambiguity (an unresolved twin exists for this exact request) and ask.
8. Missing/disabled route, missing credentials, unknown estimate, policy denial: all fail truthfully. No substitution, no guessing, no silent fallback.
9. Fetch, ingestion, and manifest-persistence failures remain distinct and truthful. Never launder a partial failure into a success.
10. Never mutate provider-global model state. State requirements truthfully ("needs reference-image support"); if the owner-selected model cannot satisfy them, the workflow fails truthfully instead of rewriting the route.
11. Lumina remains the agent. This skill is workflow knowledge, not an identity and not a second generation engine.

## Unsupported capability branches (inert — mention, never execute)

- **video generation** — lane exists in the frozen M1 vocabulary; no adapter model, no service. Future.
- **audio generation** — no lane implementation. Future.
- **3D generation** — no lane implementation. Future.
- **reference-image workflows** — outside media-generation v1 and inert until a
  reference-capable execution path is deliberately landed and admitted.
- **identity training** — belongs to `identity-model` (future), which requires its own privacy/consent/retention contract first.

If asked for any of these: state truthfully that the capability is not landed. Do not invent execution instructions.

---

## Detailed execution contract (lazy-loaded — recall when executing)

### Resolution

`resolve_image_generation_target()` reads the owner's persisted image_generation route
(prefs `multimodal_routes.image_generation`, surfaced via `config.MULTIMODAL_ROUTES`)
and resolves it through the M1 registry to a concrete, truthful target
(registry, policy, specialist, model) — or returns None. Never a guess, never a
silently substituted provider or model. If the route is absent, disabled, or does not
admit the requested specialist, the outcome is `not_routed` and the request stops
before any spend.

Provider and model are always resolved from the target runtime's persisted
capability route — never from any observation embedded in this document.

### Spending gate (before any submission)

`core.generation_spending_policy.evaluate_spend()` is pure and fail-closed:

- `allowed` — within the owner's declared ceilings; proceed.
- `requires_approval` — estimate exceeds the single-job ceiling, or projected
  session spend would exceed the session ceiling, or an estimate is required but
  unavailable under a non-fail-closed policy. STOP. Present the estimate and reason;
  wait for explicit owner approval.
- `denied` — includes the fail-closed case where policy requires an estimate and
  none is available. STOP.

Always attempt estimation before submission. If an estimate is unavailable, the
spending policy decides whether to deny or require explicit owner approval. Unknown
cost is never treated as free. Estimate ≠ actual charge; both are recorded
separately in the manifest and never summed.

### Submission and ambiguity

- `adapter.submit()` raising → outcome `submission_failed`; the service never
  assumes the request reached the provider.
- An unresolved twin already exists for this exact request (submission fingerprint)
  → outcome `ambiguous_duplicate`, reported with the existing job reference. Never
  resubmit on your own initiative.
- The adapter's `estimate_cost()` fails closed to None when unavailable; the
  spending policy decides what an unavailable estimate means. The skill never
  treats it as free.

### Polling and terminal states

Bounded polling until a real terminal status: succeeded / failed / cancelled.
- Bound exhausted without a terminal status → `timed_out`; report the last observed
  provider status. Unknown status is never guessed into a terminal outcome.
- Provider reports failure → `provider_failed`, carrying the provider's failure
  class (content-policy refusals surface as failures, never as successes).
- Provider-confirmed cancellation → `cancelled`.

### Outputs, ingestion, manifests

- One job may yield 0..N outputs. Multi-output discovery is structural; a provider
  reporting several outputs is never silently reduced to one.
- Each discovered output is handled independently. Successful ingestion yields a
  durable GeneratedArtifact with its `provider_output_index`; failed fetch or
  ingestion remains a distinct truthful failure. Remote URLs are transport only;
  the local artifact is the durable identity.
- The service attempts exactly one canonical manifest for each ingested output.
  A fully successful artifact delivery requires that manifest to validate and
  persist. `manifest_failed` truthfully represents durable artifact bytes whose
  manifest persistence failed — report both truths.
- Job ingestion states: `pending` / `ingested` / `failed` / `partial`. `partial` is
  a derived honest aggregate (some sibling outputs ingested, some failed); once
  mixed, always mixed. Never smoothed over.

### Outcome taxonomy (report the service's own outcome string, never paraphrase)

Routing / authorization / submission outcomes: `not_routed` · `unknown_manifest_provider` ·
`missing_authorization` · `spend_denied` · `spend_requires_approval` ·
`ambiguous_duplicate` · `submission_failed`

Post-submission: `provider_failed` · `cancelled` · `timed_out` · `discovery_failed` ·
`ingestion_failed` · `zero_outputs` · `partial` · `manifest_failed` · `success`

Delivery requires `success` (or an explicitly owner-accepted `partial` with the
failures itemized). Anything else is a truthful report of what happened.

### Runtime wiring note (truthful as of 2026-09-19)

The service seam is landed and fully tested. The runtime tool surface that calls it
from a live conversational turn is the next integration step. If this skill fires
and no wired entry point exists in the current runtime, say exactly that — do not
improvise an execution path around the service.

## Verification

- A delivered image exists as a local durable artifact file, not merely a URL.
- Its manifest validates against the landed manifest law (see
  `generated-artifact-manifest`).
- The reported outcome is the service's own outcome string, unedited.
- Cost estimate and actual charge (when known) are reported separately.
- No retry occurred without explicit owner authorization.