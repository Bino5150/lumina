"""
core/higgsfield_adapter.py -- MULTIMODAL-M4-HIGGSFIELD-ADAPTER-01

The first real core.generation_adapter.GenerationAdapter implementation,
against Higgsfield's documented, server-credential REST API only -- never
the CLI's device-flow surface, never a shelled-out CLI process. See
MULTIMODAL_M4_HIGGSFIELD_API_VET_2026-09-17.md for the full evidence base
and MULTIMODAL_M4_HIGGSFIELD_ADAPTER_01_2026-09-17.md for this module's
own design record. Both evidentiary claims this module depends on most
(submit/status shape, cancel endpoint) were re-verified live -- a fresh
fetch of https://docs.higgsfield.ai/docs/openapi.json byte-diffed as
IDENTICAL against the API-vet campaign's same-day cache -- immediately
before this module was written, not merely re-read from the prior report.

Architecture (the seam this module sits behind):

    Lumina domain types (core.generation_job / core.generation_artifact)
            |
    GenerationAdapter Protocol (core.generation_adapter)
            |
    HiggsfieldAdapter (this module) -- knows Higgsfield's domain shape
            |
    HiggsfieldTransport (core.higgsfield_transport) -- knows HTTP only
            |
    Higgsfield REST API (api.higgsfield.ai)

Provider wire objects never leak into GenerationJob/GeneratedArtifact:
this module accepts and returns only Lumina domain values (provider_job_id
strings, raw status strings, bytes, provider-neutral dicts) at its
GenerationAdapter-Protocol-facing boundary. Higgsfield-specific shapes
(MediaOutput, RequestStatus, ImageUrlInputImageSchema) live and die inside
this module's own request/response parsing.

REST catalog scope (v1, deliberately narrow -- truthful narrow support
over fictional broad support, per the CLI-vs-REST catalog gap the API-vet
report flagged as unreconciled, Sec 14 item 1): only `nano-banana` and
`higgsfield-ai/soul/standard` are implemented, chosen because between them
they exercise the two structurally different request shapes the real
OpenAPI spec actually has for image endpoints -- one with a reference-image
array (`input_images`) and one prompt-only. The CLI's much larger
device-flow-authenticated catalog (23 image models, Marketing Studio,
Virality Predictor, 3D, audio -- none of it proven against this
server-credential surface) is intentionally NOT imported here. See
_CATALOG_SOURCE below and MULTIMODAL_M4_HIGGSFIELD_ADAPTER_01_2026-09-17.md
Sec 3 for the full accounting of what was left out and why.

Credentials: sourced from core.secrets.get_secret() -- the established,
already-existing OS-local credential store this project already uses for
"cloud API keys" (see core/secrets.py's own docstring). This module reads
"higgsfield_key_id" / "higgsfield_key_secret" from it by default,
overridable via constructor arguments for tests. No config.py constant, no
Settings UI, no new credential storage mechanism was added or needed --
config/UI integration is explicitly out of this campaign's scope. Neither
value is ever logged, repr'd, or embedded in any exception/provenance
string -- see HiggsfieldTransport's own __repr__ override and every error
path below, which builds messages only from provider-supplied text run
through core.redaction.redact_secret_shapes(), never from the credential
values themselves (which this module never even holds in a way that could
be accidentally interpolated -- only HiggsfieldTransport holds the
constructed Authorization header value, privately).

Two Protocol-boundary decisions made here, both to avoid touching
core/generation_adapter.py at all (see
MULTIMODAL_M4_HIGGSFIELD_ADAPTER_01_2026-09-17.md Sec 0 for the reasoning
that concluded neither actually required a Protocol change):

  - poll() (Protocol-mandated) returns the bare raw provider status
    string. Higgsfield's real terminal `nsfw` status needs to become
    status=failed, failure_class=content_policy, but a bare string return
    cannot carry a failure_class -- poll_detailed() (Higgsfield-specific,
    NOT Protocol-mandated) does that full translation; poll() is a thin
    wrapper around it for generic-caller compatibility.
  - fetch_result() (Protocol-mandated) returns a single (bytes, mime_type)
    tuple, matching its original single-output design -- but a completed
    Higgsfield job can have 0..N outputs (the exact fact
    MULTIMODAL-M4-GENERATION-SUBSTRATE-AMENDMENT-01 already corrected at
    the GeneratedArtifact layer). Rather than silently discarding outputs
    2..N, fetch_result() raises MultipleOutputsError directing the caller
    to list_outputs()/fetch_output() (Higgsfield-specific, NOT
    Protocol-mandated) -- the real, fully multi-output-aware path, tested
    end to end in this campaign. Promoting this into the shared Protocol
    is deferred until more than one provider's real shape proves it's the
    right generalization, not assumed from a single provider.

Amendment (MULTIMODAL-M4-HIGGSFIELD-PRICING-REPAIR-01, 2026-09-18):
estimate_cost() originally called a remote `POST /estimate/{model}` endpoint.
MULTIMODAL-M4-HIGGSFIELD-LIVE-SMOKE-01 (same day) found that call returns
HTTP 404 `{"detail":"model_not_found"}` live, and that Higgsfield's own
OpenAPI 3.1.0 spec has zero paths under an `/estimate` namespace for any
model -- the endpoint this module depended on does not exist in the current
API surface. estimate_cost() is now a pure, local, zero-network lookup
against a small hand-verified pricing table (below), independently
re-verified against Higgsfield's current live documentation
(console.higgsfield.ai model pages) on 2026-09-18 -- see
MULTIMODAL_M4_HIGGSFIELD_PRICING_REPAIR_01_2026-09-18.md for the full
evidence record. Only `higgsfield-ai/soul/standard` has a verified price;
`nano-banana` has none (no current official source verifies its identity or
price) and estimate_cost() always returns None for it -- never guessed,
never treated as free, consistent with core.generation_spending_policy's
own fail-closed law. That same evidence record also documents a SEPARATE,
NOT-fixed-here defect: this module's own `_MODEL_CATALOG` entry for
`higgsfield-ai/soul/standard` declares stale parameter values (resolution
"2K"/"4K", key name "num_images") that do not match the real, current API
(resolution "720p"/"1080p", key name "batch_size") -- pricing below is
keyed to the real documented surface regardless, independent of
`_MODEL_CATALOG`/`_validate_settings()`.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Mapping, Optional, Sequence, Tuple

from core.capability_router import Capability
from core.redaction import redact_secret_shapes
import core.generation_job as gj
from core.higgsfield_transport import (
    HiggsfieldConnectionError,
    HiggsfieldTimeoutError,
    HiggsfieldTransport,
    RequestsHiggsfieldTransport,
    TransportResponse,
)

__all__ = [
    "HiggsfieldAdapter",
    "HiggsfieldAdapterError",
    "UnsupportedModelError",
    "UnsupportedParameterError",
    "HiggsfieldMalformedResponseError",
    "HiggsfieldProviderError",
    "MultipleOutputsError",
    "PollResult",
    "HiggsfieldOutputRef",
    "CATALOG_SOURCE",
    "SUPPORTED_MODELS",
]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HiggsfieldAdapterError(Exception):
    """Base class for every error this module raises that isn't already a
    core.generation_job exception (JobStateConflict is reused directly,
    never re-wrapped, for the cancel-a-terminal-job case)."""


class UnsupportedModelError(HiggsfieldAdapterError):
    """The requested model is not in this adapter's v1 catalog. Never
    silently substitutes a different model."""


class UnsupportedParameterError(HiggsfieldAdapterError):
    """A settings key isn't a known parameter for the requested model, a
    required parameter is missing, a parameter value fails its declared
    constraint, or a reference/media input was supplied to a model that
    doesn't accept one. Raised client-side, before any HTTP call --
    mirrors the source-vet finding that a well-behaved client validates
    locally rather than relying on the provider to reject it."""


class HiggsfieldMalformedResponseError(HiggsfieldAdapterError):
    """The provider's response didn't parse as JSON, or didn't contain a
    field this module's logic requires to proceed truthfully."""


class MultipleOutputsError(HiggsfieldAdapterError):
    """Raised by fetch_result() when the completed job actually has more
    than one output -- see this module's own docstring for why. Points the
    caller at list_outputs()/fetch_output() instead of silently returning
    only the first output."""


class HiggsfieldProviderError(HiggsfieldAdapterError):
    """A classified HTTP-level failure from the provider. `kind` is one of
    the constants below (never invented ad hoc at a call site), `status_code`
    is the real HTTP status, `retry_after` is the parsed Retry-After header
    value in seconds when present (None otherwise)."""

    def __init__(self, kind: str, status_code: int, message: str,
                 retry_after: Optional[float] = None):
        super().__init__(f"{kind} (HTTP {status_code}): {message}")
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after


# Error kinds -- a closed, small vocabulary; every HTTP status this module
# has real evidence for maps to exactly one of these (MULTIMODAL_M4_
# HIGGSFIELD_API_VET_2026-09-17.md Sec 10's table). Never extended ad hoc
# at a call site.
ERROR_AUTH_FAILURE = "auth_failure"
ERROR_INSUFFICIENT_CREDITS = "insufficient_credits"
ERROR_VALIDATION_FAILURE = "validation_failure"
ERROR_NOT_FOUND = "not_found"
ERROR_MODEL_UNAVAILABLE = "model_unavailable"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_PROVIDER_ERROR = "provider_error"
ERROR_CANCELLATION_ERROR = "cancellation_error"

_STATUS_TO_ERROR_KIND = {
    401: ERROR_AUTH_FAILURE,
    403: ERROR_INSUFFICIENT_CREDITS,
    404: ERROR_NOT_FOUND,
    422: ERROR_VALIDATION_FAILURE,
    423: ERROR_MODEL_UNAVAILABLE,
    429: ERROR_RATE_LIMITED,
    503: ERROR_MODEL_UNAVAILABLE,
}


def _classify_status(status_code: int, detail: str) -> str:
    """400 is ambiguous by Higgsfield's own docs (Sec 10: "Invalid
    parameters, rejected input, or concurrency reached") -- a best-effort
    substring check on the provider's own message distinguishes the
    concurrency case; everything else defaults per the table above, and any
    unmapped status (or a genuine 5xx) becomes ERROR_PROVIDER_ERROR."""
    if status_code == 400:
        if "concurrent" in detail.lower():
            return ERROR_RATE_LIMITED
        return ERROR_VALIDATION_FAILURE
    return _STATUS_TO_ERROR_KIND.get(status_code, ERROR_PROVIDER_ERROR)


def _parse_retry_after(headers: Mapping[str, str]) -> Optional[float]:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# v1 REST model catalog -- adapter-owned, derived directly from the
# official OpenAPI contract, never the CLI's broader dynamic catalog.
# ---------------------------------------------------------------------------

CATALOG_SOURCE = (
    "https://docs.higgsfield.ai/docs/openapi.json -- openapi 3.1.0, "
    "info.title='Higgsfield API', info.version='2.0.0'. Fetched during "
    "MULTIMODAL-M4-HIGGSFIELD-API-VET-01 (2026-09-17) and re-fetched "
    "immediately before this module was written (also 2026-09-17) -- "
    "byte-identical, zero drift between the two fetches."
)

_MODEL_CATALOG: Mapping[str, dict] = {
    "nano-banana": {
        "path": "/nano-banana",
        "capability": Capability.IMAGE_GENERATION.value,
        "required_params": frozenset({"prompt"}),
        "optional_params": {
            "num_images": {"type": "integer", "minimum": 1, "maximum": 4},
            "aspect_ratio": {"type": "enum", "values": (
                "auto", "1:1", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "16:9", "9:16", "21:9",
            )},
            "output_format": {"type": "enum", "values": ("jpeg", "png")},
        },
        "reference_role": "input_images",
        "reference_max": 8,
        "cost_estimate_supported": True,
    },
    "higgsfield-ai/soul/standard": {
        "path": "/higgsfield-ai/soul/standard",
        "capability": Capability.IMAGE_GENERATION.value,
        "required_params": frozenset({"prompt"}),
        "optional_params": {
            "num_images": {"type": "integer", "minimum": 1, "maximum": 4},
            "resolution": {"type": "enum", "values": ("2K", "4K")},
            "aspect_ratio": {"type": "enum", "values": (
                "1:1", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "16:9", "9:16", "21:9",
            )},
        },
        "reference_role": None,
        "reference_max": 0,
        "cost_estimate_supported": True,
    },
}
SUPPORTED_MODELS = tuple(sorted(_MODEL_CATALOG))


# ---------------------------------------------------------------------------
# Local, deterministic pricing -- MULTIMODAL-M4-HIGGSFIELD-PRICING-REPAIR-01.
# Closed, hand-maintained table; no network, no credentials. Every entry was
# independently re-verified against Higgsfield's current live documentation
# on 2026-09-18 -- see MULTIMODAL_M4_HIGGSFIELD_PRICING_REPAIR_01_2026-09-18.md
# for the full evidence record (this module's own docstring amendment above
# has the short version). Deliberately keyed to the REAL documented parameter
# surface (resolution "720p"/"1080p", "batch_size"), not to this file's own
# (stale, unfixed-here) _MODEL_CATALOG validation values -- see that same
# evidence doc Sec 4. A model absent from this table (e.g. "nano-banana") is
# never priced by guesswork; PRICE_TABLE.get(model) returning None is the
# correct, honest outcome, not a bug.
PRICE_TABLE: Mapping[str, dict] = {
    "higgsfield-ai/soul/standard": {
        "price_per_image_usd": {
            "720p": Decimal("0.0938"),
            "1080p": Decimal("0.1875"),
        },
        "default_resolution": "720p",
        "supported_batch_sizes": (1, 4),  # the only values Higgsfield documents
        "default_batch_size": 1,
    },
    # "nano-banana" intentionally absent: no current official Higgsfield
    # source (dedicated docs page, docs-site search, or the console's live
    # model/pricing catalog) verifies its identity or price as of
    # 2026-09-18, even though its raw generation path still appears in the
    # OpenAPI catalog. See the evidence doc for the full absence trail.
}


def _is_real_int(value) -> bool:
    """True only for a genuine int -- bool is an int subclass in Python and
    a batch size of True/False is nonsensical, never silently coerced."""
    return isinstance(value, int) and not isinstance(value, bool)


def _local_price_estimate(model: str, settings: Mapping[str, object]) -> Optional[Decimal]:
    """Pure, local, deterministic USD-per-request lookup. Returns None
    whenever the model isn't in PRICE_TABLE, or any pricing-relevant
    parameter value isn't in the documented, priced set -- never guesses,
    never extrapolates, never treats an unknown combination as free.
    Accepts either the real documented parameter name (`batch_size`) or
    this catalog's currently-declared name (`num_images`, see PRICE_TABLE's
    comment) for the image count; the two disagreeing is genuinely
    ambiguous and also fails closed."""
    entry = PRICE_TABLE.get(model)
    if entry is None:
        return None

    resolution = settings.get("resolution", entry["default_resolution"])
    if not isinstance(resolution, str):
        return None
    price_per_image = entry["price_per_image_usd"].get(resolution)
    if price_per_image is None:
        return None

    has_batch_size = "batch_size" in settings
    has_num_images = "num_images" in settings
    if has_batch_size and has_num_images:
        if settings["batch_size"] != settings["num_images"]:
            return None
        count = settings["batch_size"]
    elif has_batch_size:
        count = settings["batch_size"]
    elif has_num_images:
        count = settings["num_images"]
    else:
        count = entry["default_batch_size"]

    if not _is_real_int(count) or count not in entry["supported_batch_sizes"]:
        return None

    return price_per_image * Decimal(count)


def _validate_param_value(key: str, value, spec: dict, *, model: str) -> None:
    kind = spec["type"]
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise UnsupportedParameterError(f"{model}: {key!r} must be an int, got {value!r}")
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise UnsupportedParameterError(f"{model}: {key!r}={value!r} outside [{lo}, {hi}]")
    elif kind == "enum":
        if value not in spec["values"]:
            raise UnsupportedParameterError(
                f"{model}: {key!r}={value!r} not one of {spec['values']}"
            )


def _validate_settings(model: str, entry: dict, settings: Mapping[str, object]) -> None:
    known = entry["required_params"] | frozenset(entry["optional_params"])
    unknown = set(settings) - known
    if unknown:
        raise UnsupportedParameterError(
            f"{model}: unsupported parameter(s) {sorted(unknown)}; known: {sorted(known)}"
        )
    missing = entry["required_params"] - set(settings)
    if missing:
        raise UnsupportedParameterError(f"{model}: missing required parameter(s) {sorted(missing)}")
    prompt = settings.get("prompt")
    if "prompt" in entry["required_params"] and (not isinstance(prompt, str) or not prompt.strip()):
        raise UnsupportedParameterError(f"{model}: 'prompt' must be a non-empty string")
    for key, value in settings.items():
        spec = entry["optional_params"].get(key)
        if spec is not None:
            _validate_param_value(key, value, spec, model=model)


# ---------------------------------------------------------------------------
# Poll / output value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PollResult:
    """Higgsfield-specific richer poll result -- see this module's
    docstring for why poll() alone (Protocol-mandated, bare string) can't
    carry failure_class."""

    raw_status: str
    canonical_status: str
    failure_class: Optional[str]
    error_detail: Optional[str]


@dataclass(frozen=True)
class HiggsfieldOutputRef:
    """One discovered output, not yet fetched. `index` is exactly the
    value a caller should pass as provider_output_index to
    core.generation_artifact.ingest_artifact() -- provider array order is
    preserved deterministically (images first, then video, then audio,
    then audios, matching RequestStatus's own field order)."""

    index: int
    url: str
    kind: str  # "image" | "video" | "audio"


_NSFW_RAW_STATUS = "nsfw"


def _canonical_status(raw_status: str) -> str:
    if isinstance(raw_status, str) and raw_status.strip().lower() == _NSFW_RAW_STATUS:
        return gj.STATUS_FAILED
    return gj.normalize_provider_status(raw_status)


def _require_str(body: Mapping[str, object], field_name: str, *, context: str) -> str:
    value = body.get(field_name) if isinstance(body, Mapping) else None
    if not isinstance(value, str) or not value:
        raise HiggsfieldMalformedResponseError(
            f"{context}: response missing a non-empty string {field_name!r} field"
        )
    return value


def _parse_output(item, *, kind: str) -> HiggsfieldOutputRef:
    if not isinstance(item, Mapping):
        raise HiggsfieldMalformedResponseError(f"output entry is not an object: {item!r}")
    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise HiggsfieldMalformedResponseError(f"output entry missing a string 'url' field: {item!r}")
    return HiggsfieldOutputRef(index=-1, url=url, kind=kind)  # index assigned by caller


def _extract_outputs(body: Mapping[str, object]) -> List[HiggsfieldOutputRef]:
    """RequestStatus's own field order (images, video, audio, audios) sets
    the deterministic combined ordering across output-type fields; array
    order within `images`/`audios` is preserved as-is. A field that's
    present but not the type OpenAPI declares (array vs. object) is a
    malformed response, not silently skipped."""
    raw: List[HiggsfieldOutputRef] = []

    images = body.get("images")
    if images is not None:
        if not isinstance(images, list):
            raise HiggsfieldMalformedResponseError("'images' field is present but not a list")
        raw.extend(_parse_output(item, kind="image") for item in images)

    video = body.get("video")
    if video is not None:
        raw.append(_parse_output(video, kind="video"))

    audio = body.get("audio")
    if audio is not None:
        raw.append(_parse_output(audio, kind="audio"))

    audios = body.get("audios")
    if audios is not None:
        if not isinstance(audios, list):
            raise HiggsfieldMalformedResponseError("'audios' field is present but not a list")
        raw.extend(_parse_output(item, kind="audio") for item in audios)

    return [HiggsfieldOutputRef(index=i, url=o.url, kind=o.kind) for i, o in enumerate(raw)]


# ---------------------------------------------------------------------------
# Credentials -- the existing, already-established OS-local secret store
# ---------------------------------------------------------------------------

def _default_transport() -> HiggsfieldTransport:
    from core.secrets import get_secret
    key_id = get_secret("higgsfield_key_id", "")
    key_secret = get_secret("higgsfield_key_secret", "")
    if not key_id or not key_secret:
        raise HiggsfieldAdapterError(
            "Higgsfield credentials not configured -- set 'higgsfield_key_id' and "
            "'higgsfield_key_secret' via core.secrets.set_secret() (never source, never "
            "prefs.json). See Higgsfield Console for credential creation."
        )
    return RequestsHiggsfieldTransport(key_id, key_secret)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class HiggsfieldAdapter:
    """core.generation_adapter.GenerationAdapter implementation for
    Higgsfield's server-credential REST API. `transport` is injectable --
    tests pass a fake (see tests/test_multimodal_m4_higgsfield_adapter_01.py);
    production code omits it and gets a real RequestsHiggsfieldTransport
    sourced from core.secrets."""

    def __init__(self, transport: Optional[HiggsfieldTransport] = None):
        self._transport = transport if transport is not None else _default_transport()

    def __repr__(self) -> str:
        return f"HiggsfieldAdapter(transport={self._transport!r})"

    def _catalog_entry(self, model: str) -> dict:
        entry = _MODEL_CATALOG.get(model)
        if entry is None:
            raise UnsupportedModelError(
                f"model {model!r} is not in this adapter's v1 catalog; supported: {SUPPORTED_MODELS}"
            )
        return entry

    def _parse_json(self, response: TransportResponse, *, context: str) -> dict:
        try:
            body = response.json()
        except (_json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HiggsfieldMalformedResponseError(f"{context}: response body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise HiggsfieldMalformedResponseError(f"{context}: response body is not a JSON object")
        return body

    def _translate_error(self, response: TransportResponse, *, context: str,
                          kind_override: Optional[str] = None) -> HiggsfieldProviderError:
        """Never raises itself -- returns the exception for the caller to
        `raise`, so every call site's traceback points at the actual call,
        not this helper. Builds its message only from the provider's own
        `detail` text (FastAPI envelope, per source-vet Sec 10), run
        through redact_secret_shapes() as defense in depth even though
        Higgsfield credentials don't match any of that function's known
        shapes -- this module never embeds the credential value in any
        string in the first place. `kind_override` lets a call site (e.g.
        cancel()) force a specific error kind -- e.g. ERROR_CANCELLATION_ERROR
        -- while still deferring to the general status-code table
        (`_classify_status`) whenever the status is already unambiguous
        (auth/not-found/etc. mean the same thing regardless of which
        operation hit them)."""
        try:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, list):
                detail = "; ".join(str(d) for d in detail)
            detail = str(detail) if detail is not None else response.text[:500]
        except Exception:
            detail = response.text[:500]
        detail = redact_secret_shapes(detail)
        kind = _classify_status(response.status_code, detail)
        if kind_override is not None and kind == ERROR_PROVIDER_ERROR:
            kind = kind_override
        retry_after = _parse_retry_after(response.headers)
        return HiggsfieldProviderError(kind, response.status_code, f"{context}: {detail}", retry_after)

    # -- GenerationAdapter Protocol -----------------------------------

    def submit(self, *, model: str, settings: dict,
               reference_assets: Tuple[Tuple[str, str], ...],
               provider_idempotency_key: Optional[str] = None) -> Tuple[str, str]:
        entry = self._catalog_entry(model)
        _validate_settings(model, entry, settings)
        if provider_idempotency_key is not None:
            # Source-vet Sec 5: Higgsfield's real API has NO client-supplied
            # idempotency mechanism -- refuse rather than silently discard
            # or pretend it was honored.
            raise HiggsfieldAdapterError(
                "provider_idempotency_key was supplied, but Higgsfield's API "
                "does not support one (no such field exists anywhere in the "
                "OpenAPI spec, the official SDK source, or the docs) -- "
                "refusing to silently discard it"
            )
        payload = dict(settings)
        if reference_assets:
            payload.update(self._build_reference_payload(model, entry, reference_assets))

        response = self._transport.post(entry["path"], json=payload)
        if response.status_code >= 400:
            raise self._translate_error(response, context=f"submit {model}")
        body = self._parse_json(response, context="submit")
        request_id = _require_str(body, "request_id", context="submit")
        raw_status = _require_str(body, "status", context="submit")
        return request_id, raw_status

    def poll(self, provider_job_id: str) -> str:
        """Protocol-mandated bare-string poll. Needs only provider_job_id
        -- no in-memory submission state -- so reattachment after a
        restart works with nothing but the persisted GenerationJob record,
        exactly matching the substrate's mandatory restart/reattach
        property."""
        return self.poll_detailed(provider_job_id).raw_status

    def poll_detailed(self, provider_job_id: str) -> PollResult:
        response = self._transport.get(f"/requests/{provider_job_id}/status")
        if response.status_code >= 400:
            raise self._translate_error(response, context="poll")
        body = self._parse_json(response, context="poll")
        raw_status = _require_str(body, "status", context="poll")
        canonical = _canonical_status(raw_status)
        error_detail = body.get("error") if isinstance(body.get("error"), str) else None
        failure_class = None
        if canonical == gj.STATUS_FAILED:
            failure_class = "content_policy" if raw_status.strip().lower() == _NSFW_RAW_STATUS else "provider_error"
        return PollResult(raw_status=raw_status, canonical_status=canonical,
                           failure_class=failure_class, error_detail=error_detail)

    def cancel(self, job) -> "gj.GenerationJob":
        if job.status in gj.TERMINAL_STATUSES:
            raise gj.JobStateConflict(
                f"job {job.lumina_job_id} is already terminal ({job.status!r}); "
                "refusing to treat this as new cancellation work"
            )
        if not job.provider_job_id:
            raise HiggsfieldAdapterError(
                f"job {job.lumina_job_id} has no provider_job_id yet; nothing to cancel"
            )
        response = self._transport.post(f"/requests/{job.provider_job_id}/cancel")
        if response.status_code == 202:
            # Higgsfield's documented cancel is synchronous confirm-or-reject
            # (its own 202 description literally reads "The request was
            # canceled") -- but this adapter still goes through the same
            # update_status() CAS path every other transition uses, rather
            # than assuming success independent of the durable record.
            return gj.update_status(job.lumina_job_id, gj.STATUS_CANCELLED)
        if response.status_code == 400:
            # "The request has already started and can no longer be
            # canceled" -- rejected, job is left exactly as it was; a later
            # poll() reflects its real, ongoing fate.
            return job
        if response.status_code >= 400:
            # Unrecognized or genuinely erroneous status (>=400, including
            # one this adapter's classification table doesn't specifically
            # map) -- fails closed by raising, never by silently mutating
            # the job. Nothing about the durable record changes here; the
            # caller sees a clear, explicit error instead of an assumed
            # outcome.
            raise self._translate_error(response, context="cancel", kind_override=ERROR_CANCELLATION_ERROR)
        # A successful-range status OTHER than the one documented 202 --
        # Higgsfield's real cancel endpoint has no defined body/shape for
        # this case at all (its OpenAPI 202 response declares no content
        # schema), so there is no truthful information to act on beyond
        # "the provider accepted the connection but didn't confirm
        # termination the documented way." Per the required contract --
        # "if the provider only acknowledges the request without
        # confirming a terminal outcome, the job may legitimately remain
        # in its existing non-terminal status" -- the job is left exactly
        # as it was; a later poll() is what actually resolves it, exactly
        # like any other status change.
        return job

    def fetch_result(self, provider_job_id: str) -> Tuple[bytes, str]:
        """Protocol-mandated single-output accessor -- see this module's
        docstring for why a job with more than one output raises
        MultipleOutputsError here instead of silently returning only the
        first."""
        outputs = self.list_outputs(provider_job_id)
        if not outputs:
            raise HiggsfieldAdapterError(f"job {provider_job_id} succeeded with zero outputs")
        if len(outputs) > 1:
            raise MultipleOutputsError(
                f"job {provider_job_id} has {len(outputs)} outputs; fetch_result() only "
                "handles the single-output case -- use list_outputs()/fetch_output() instead"
            )
        return self.fetch_output(provider_job_id, 0)

    def list_outputs(self, provider_job_id: str) -> List[HiggsfieldOutputRef]:
        """Higgsfield-specific, NOT Protocol-mandated: discovers every
        output a succeeded job actually has, in deterministic provider
        order, without fetching any bytes. This module's job is discovery
        only -- core.generation_artifact.ingest_artifact() owns durable
        download/hash/storage; this method never writes anything."""
        response = self._transport.get(f"/requests/{provider_job_id}/status")
        if response.status_code >= 400:
            raise self._translate_error(response, context="list_outputs")
        body = self._parse_json(response, context="list_outputs")
        raw_status = _require_str(body, "status", context="list_outputs")
        canonical = _canonical_status(raw_status)
        if canonical != gj.STATUS_SUCCEEDED:
            raise HiggsfieldAdapterError(
                f"job {provider_job_id} is not succeeded (raw status={raw_status!r}); "
                "outputs are only meaningful once the provider reports success"
            )
        return _extract_outputs(body)

    def fetch_output(self, provider_job_id: str, output_index: int) -> Tuple[bytes, str]:
        """Fetch ONE output's bytes by its index into list_outputs()'s
        returned sequence -- that index is exactly the provider_output_index
        value to pass to core.generation_artifact.ingest_artifact(). MIME
        type comes from the HTTP response's Content-Type header, never a
        same-named JSON field (source-vet Sec 9: webhooks.md's example
        payloads show a `content_type` field on MediaOutput, but the
        formal OpenAPI schema declares MediaOutput as strictly `{"url":
        ...}` with additionalProperties:false -- the two official sources
        disagree, so this module never depends on that field existing).
        The fetched bytes are UNTRUSTED provider data at this point --
        this method returns them as-is; core.generation_artifact.
        ingest_artifact() is what hashes and makes them durable."""
        outputs = self.list_outputs(provider_job_id)
        if not (0 <= output_index < len(outputs)):
            raise HiggsfieldAdapterError(
                f"output_index {output_index} out of range for job {provider_job_id} "
                f"({len(outputs)} output(s))"
            )
        target = outputs[output_index]
        response = self._transport.get_raw(target.url)
        if response.status_code >= 400:
            raise HiggsfieldAdapterError(
                f"fetching output {output_index} for job {provider_job_id} failed "
                f"with HTTP {response.status_code}"
            )
        content_type = None
        for key, value in response.headers.items():
            if key.lower() == "content-type":
                content_type = value.split(";")[0].strip()
                break
        if not content_type:
            content_type = "application/octet-stream"
        return response.body, content_type

    def describe_model(self, model: str) -> Optional[dict]:
        entry = _MODEL_CATALOG.get(model)
        if entry is None:
            return None
        return {
            "provider": "higgsfield",
            "model": model,
            "capability": entry["capability"],
            "reference_role": entry["reference_role"],
            "reference_max": entry["reference_max"],
            "required_params": sorted(entry["required_params"]),
            "optional_params": {k: dict(v) for k, v in entry["optional_params"].items()},
            "cost_estimate_supported": entry["cost_estimate_supported"],
            "source": CATALOG_SOURCE,
        }

    def estimate_cost(self, *, model: str, settings: dict) -> Optional[float]:
        """MULTIMODAL-M4-HIGGSFIELD-PRICING-REPAIR-01: a pure, local,
        zero-network lookup against PRICE_TABLE above -- never calls the
        provider (see this module's docstring amendment for why: the
        remote /estimate/{model} endpoint this method used to call does
        not exist in Higgsfield's current API). Fail-closed by design:
        an unknown model, an unpriced parameter combination, or an
        internally ambiguous settings dict all return None (unavailable),
        matching core.generation_spending_policy's own "unknown cost,
        never treated as free" law. Never raises for a pricing-data
        problem -- None is the only "can't tell you" signal this method
        gives."""
        price = _local_price_estimate(model, dict(settings))
        return float(price) if price is not None else None

    # -- Media upload (adapter-internal; used by submit() only) -------

    def _build_reference_payload(self, model: str, entry: dict,
                                  reference_assets: Sequence[Tuple[str, str]]) -> dict:
        if entry["reference_role"] is None:
            raise UnsupportedParameterError(f"{model} does not accept reference/media inputs")
        if len(reference_assets) > entry["reference_max"]:
            raise UnsupportedParameterError(
                f"{model} accepts at most {entry['reference_max']} reference(s), "
                f"got {len(reference_assets)}"
            )
        urls = [self._resolve_reference(artifact_id) for _role, artifact_id in reference_assets]
        return {entry["reference_role"]: [{"type": "image_url", "image_url": url} for url in urls]}

    def _resolve_reference(self, artifact_id: str) -> str:
        """Every reference_assets artifact_ref is treated as a Lumina
        GeneratedArtifact id -- never a raw URL, never a filesystem path.
        This is the only input shape this module trusts: it reads
        already-ingested, hash-verified bytes Lumina itself owns and
        uploads THOSE, so this adapter never performs an arbitrary,
        caller-directed URL fetch on Lumina's behalf."""
        from core.generation_artifact import get_artifact_bytes, get_generation_artifact
        record = get_generation_artifact(artifact_id)
        data = get_artifact_bytes(artifact_id)
        return self._upload_bytes(data, record.mime_type)

    def _upload_bytes(self, data: bytes, content_type: str) -> str:
        """The documented two-step presigned-upload flow (source-vet Sec
        8). Local bytes only -- never a filesystem path is sent to the
        provider in any request body."""
        resp = self._transport.post("/files/generate-upload-url", json={"content_type": content_type})
        if resp.status_code >= 400:
            raise self._translate_error(resp, context="generate-upload-url")
        body = self._parse_json(resp, context="generate-upload-url")
        public_url = _require_str(body, "public_url", context="generate-upload-url")
        upload_url = _require_str(body, "upload_url", context="generate-upload-url")
        upload_headers = body.get("upload_headers")
        if not isinstance(upload_headers, dict):
            raise HiggsfieldMalformedResponseError(
                "generate-upload-url response missing an 'upload_headers' object"
            )
        put_resp = self._transport.put_bytes(upload_url, data=data, headers=upload_headers)
        if put_resp.status_code >= 400:
            raise HiggsfieldAdapterError(
                f"upload PUT to presigned URL failed with HTTP {put_resp.status_code}"
            )
        return public_url
