"""
tests/test_multimodal_m4_higgsfield_adapter_01.py -- MULTIMODAL-M4-HIGGSFIELD-ADAPTER-01

Exercises core/higgsfield_adapter.py's real request-building and
response-parsing logic with EVERY I/O path mocked. FakeHiggsfieldTransport
never opens a socket; test_offline_law_full_adapter_lifecycle proves this
for a full lifecycle by patching socket.socket to raise if called at all.

No real Higgsfield network call, no real credentials, no live/paid
generation anywhere in this file -- see
MULTIMODAL_M4_HIGGSFIELD_ADAPTER_01_2026-09-17.md Sec 16.
"""
from __future__ import annotations

import json
import socket

import pytest

import config
import core.generation_job as gj
import core.generation_artifact as ga
import core.generation_spending_policy as sp
import core.higgsfield_adapter as ha
import core.higgsfield_transport as ht
from core.capability_router import Capability, RoutingDecision


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    return tmp_path


# ---------------------------------------------------------------------------
# Fake transport -- fully offline, deterministic, records every call made
# ---------------------------------------------------------------------------

class FakeHiggsfieldTransport:
    """Satisfies core.higgsfield_transport.HiggsfieldTransport structurally.
    Never touches a socket. Configure a response per (METHOD, path-or-url);
    an unconfigured call raises AssertionError immediately rather than
    hanging or guessing."""

    def __init__(self):
        self.calls = []

    def configure(self, method, key, response):
        self.__dict__.setdefault("_responses", {})[(method, key)] = response

    def _resolve(self, method, key):
        responses = self.__dict__.setdefault("_responses", {})
        resp = responses.get((method, key))
        if resp is None:
            raise AssertionError(f"FakeHiggsfieldTransport: no response configured for {method} {key}")
        return resp() if callable(resp) else resp

    def post(self, path, *, json=None, params=None):
        self.calls.append({"method": "POST", "path": path, "json": json, "params": params})
        return self._resolve("POST", path)

    def get(self, path, *, params=None):
        self.calls.append({"method": "GET", "path": path, "params": params})
        return self._resolve("GET", path)

    def put_bytes(self, url, *, data, headers):
        self.calls.append({"method": "PUT", "url": url, "data": data, "headers": dict(headers)})
        return self._resolve("PUT", url)

    def get_raw(self, url, *, headers=None):
        self.calls.append({"method": "GET_RAW", "url": url, "headers": dict(headers) if headers else None})
        return self._resolve("GET_RAW", url)


def _json_response(status_code, body_dict, headers=None):
    return ht.TransportResponse(
        status_code=status_code,
        headers=headers or {},
        body=json.dumps(body_dict).encode("utf-8"),
    )


def _raw_response(status_code, body_bytes, headers=None):
    return ht.TransportResponse(status_code=status_code, headers=headers or {}, body=body_bytes)


class _FakeRequestsResponse:
    """Duck-types requests.Response enough for
    RequestsHiggsfieldTransport._to_transport_response()."""

    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content


# ---------------------------------------------------------------------------
# Generation-substrate helpers (same pattern as
# tests/test_multimodal_m4_generation_substrate_01.py)
# ---------------------------------------------------------------------------

def _routed_decision(specialist="higgsfield", capability=Capability.IMAGE_GENERATION.value):
    return RoutingDecision(capability=capability, outcome="routed", selected=specialist,
                            classification="explicit", reason="test fixture")


def _begin(model="higgsfield-ai/soul/standard", settings=None, refs=(), **kw):
    return gj.begin_generation_job(
        Capability.IMAGE_GENERATION, "higgsfield", model, _routed_decision(),
        settings if settings is not None else {"prompt": "a red bicycle"},
        reference_assets=refs, **kw,
    )


def _queued_job(provider_job_id="req-1", **kw):
    job = _begin(**kw)
    return gj.mark_submitted(job.lumina_job_id, provider_job_id, "queued")


def _running_job(provider_job_id="req-1", **kw):
    return gj.update_status(_queued_job(provider_job_id=provider_job_id, **kw).lumina_job_id, gj.STATUS_RUNNING)


def _succeeded_job(provider_job_id="req-1", **kw):
    job = _queued_job(provider_job_id=provider_job_id, **kw)
    return gj.update_status(job.lumina_job_id, gj.STATUS_SUCCEEDED)


def _failed_job(**kw):
    job = _queued_job(**kw)
    return gj.update_status(job.lumina_job_id, gj.STATUS_FAILED)


# ===========================================================================
# AUTH
# ===========================================================================

def test_transport_auth_header_construction(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _FakeRequestsResponse(200, {}, b'{"status": "queued", "request_id": "r1"}')

    monkeypatch.setattr(ht.requests, "post", fake_post)
    transport = ht.RequestsHiggsfieldTransport("my-key-id", "my-key-secret")
    transport.post("/higgsfield-ai/soul/standard", json={"prompt": "x"})
    assert captured["headers"]["Authorization"] == "Key my-key-id:my-key-secret"
    assert captured["url"] == "https://api.higgsfield.ai/higgsfield-ai/soul/standard"
    assert captured["headers"]["Content-Type"] == "application/json"


def test_credentials_never_in_transport_repr():
    transport = ht.RequestsHiggsfieldTransport("secret-key-id-xyz", "secret-key-secret-abc")
    text = repr(transport)
    assert "secret-key-id-xyz" not in text
    assert "secret-key-secret-abc" not in text
    assert "REDACTED" in text


def test_credentials_never_in_adapter_repr():
    transport = ht.RequestsHiggsfieldTransport("id-should-not-leak", "secret-should-not-leak")
    adapter = ha.HiggsfieldAdapter(transport=transport)
    text = repr(adapter)
    assert "id-should-not-leak" not in text
    assert "secret-should-not-leak" not in text


def test_credentials_never_in_error_message(monkeypatch):
    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        return _FakeRequestsResponse(401, {}, b'{"detail": "Invalid credentials"}')

    monkeypatch.setattr(ht.requests, "post", fake_post)
    transport = ht.RequestsHiggsfieldTransport("id-should-not-leak-2", "secret-should-not-leak-2")
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert "id-should-not-leak-2" not in str(excinfo.value)
    assert "secret-should-not-leak-2" not in str(excinfo.value)


# ===========================================================================
# SUBMIT
# ===========================================================================

def test_submit_valid_image_generation_request():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(200, {
        "status": "queued", "request_id": "req-abc",
        "status_url": "https://api.higgsfield.ai/requests/req-abc/status",
        "cancel_url": "https://api.higgsfield.ai/requests/req-abc/cancel",
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, raw_status = adapter.submit(
        model="higgsfield-ai/soul/standard", settings={"prompt": "a lighthouse", "aspect_ratio": "16:9"},
        reference_assets=(),
    )
    assert request_id == "req-abc"
    assert raw_status == "queued"
    assert transport.calls[0]["json"] == {"prompt": "a lighthouse", "aspect_ratio": "16:9"}


def test_submit_captures_provider_job_id_into_generation_job():
    job = _begin(model="higgsfield-ai/soul/standard", settings={"prompt": "a lighthouse"})
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard",
                         _json_response(200, {"status": "queued", "request_id": "req-xyz"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, raw_status = adapter.submit(model="higgsfield-ai/soul/standard",
                                             settings={"prompt": "a lighthouse"}, reference_assets=())
    updated = gj.mark_submitted(job.lumina_job_id, request_id, raw_status)
    assert updated.provider_job_id == "req-xyz"
    assert updated.status == gj.STATUS_QUEUED


def test_submit_malformed_response_missing_request_id():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(200, {"status": "queued"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())


def test_submit_unsupported_parameter_fails_explicitly():
    transport = FakeHiggsfieldTransport()
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x", "totally_bogus_param": 1},
                        reference_assets=())
    assert transport.calls == []  # rejected client-side, no HTTP call made


def test_submit_missing_required_parameter_fails_explicitly():
    transport = FakeHiggsfieldTransport()
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"aspect_ratio": "16:9"}, reference_assets=())
    assert transport.calls == []


def test_submit_unsupported_model_fails_explicitly():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedModelError):
        adapter.submit(model="some-cli-only-model-not-in-rest-catalog", settings={"prompt": "x"},
                        reference_assets=())


def test_submit_rejects_provider_idempotency_key():
    """Higgsfield's real API has no client-supplied idempotency key
    (source-vet Sec 5) -- refuses rather than silently discarding it."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.HiggsfieldAdapterError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=(),
                        provider_idempotency_key="some-key")


def test_submit_param_enum_violation_fails_explicitly():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="higgsfield-ai/soul/standard",
                        settings={"prompt": "x", "aspect_ratio": "not-a-real-ratio"},
                        reference_assets=())


# ===========================================================================
# SOUL STANDARD SCHEMA -- MULTIMODAL-M4-HIGGSFIELD-CATALOG-SCHEMA-ALIGNMENT-01
#
# _MODEL_CATALOG["higgsfield-ai/soul/standard"] was re-aligned to the real,
# live-re-vetted official schema: resolution enum 720p/1080p, batch_size
# (not num_images) enum 1/4, aspect_ratio 7 documented values. The tests
# below are this repair's required proof (task's required cases 1-9): every
# documented combination validates and reaches the transport with the exact
# provider request shape, and every stale/undocumented value is rejected
# client-side rather than silently translated or forwarded.
# ===========================================================================

_SOUL_STANDARD = "higgsfield-ai/soul/standard"
_SOUL_STANDARD_PATH = "/higgsfield-ai/soul/standard"


@pytest.mark.parametrize("resolution,batch_size", [
    ("720p", 1), ("720p", 4), ("1080p", 1), ("1080p", 4),
])
def test_submit_soul_standard_documented_resolution_batch_combinations_validate(resolution, batch_size):
    """Required cases 1-4: every documented (resolution, batch_size) pair
    validates client-side and reaches the transport."""
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", _SOUL_STANDARD_PATH, _json_response(200, {
        "status": "queued", "request_id": "req-schema-1",
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, raw_status = adapter.submit(
        model=_SOUL_STANDARD,
        settings={"prompt": "x", "resolution": resolution, "batch_size": batch_size},
        reference_assets=(),
    )
    assert request_id == "req-schema-1"
    assert raw_status == "queued"
    # Required case 9: the exact provider request shape -- real documented
    # parameter names only, nothing translated or invented.
    assert transport.calls[0]["json"] == {
        "prompt": "x", "resolution": resolution, "batch_size": batch_size,
    }


@pytest.mark.parametrize("stale_resolution", ["2K", "4K"])
def test_submit_soul_standard_stale_resolution_rejected(stale_resolution):
    """Required cases 5-6: this catalog's OLD resolution values ('2K'/'4K')
    are no longer valid -- the real API has never documented them."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model=_SOUL_STANDARD, settings={"prompt": "x", "resolution": stale_resolution},
                        reference_assets=())


def test_submit_soul_standard_stale_num_images_key_rejected():
    """Required case 7: the real, current schema has no 'num_images' field
    for this model (it's 'batch_size') -- never silently translated,
    rejected as an unknown parameter, not forwarded to the provider."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model=_SOUL_STANDARD, settings={"prompt": "x", "num_images": 4},
                        reference_assets=())


@pytest.mark.parametrize("bogus_batch_size", [0, 2, 3, 5, True, False, 1.0, "1", None])
def test_submit_soul_standard_unsupported_batch_size_fails_closed(bogus_batch_size):
    """Required case 8: only 1 and 4 are real documented batch sizes --
    every other value, including bool-as-int aliasing (True == 1 in
    Python), fails closed rather than being silently accepted."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model=_SOUL_STANDARD, settings={"prompt": "x", "batch_size": bogus_batch_size},
                        reference_assets=())


def test_submit_soul_standard_documented_aspect_ratio_values_validate():
    """The re-vetted aspect_ratio enum is the real 7-value set -- not the
    prior catalog's stale 10-value set."""
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", _SOUL_STANDARD_PATH, _json_response(200, {
        "status": "queued", "request_id": "req-schema-2",
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    for ratio in ("9:16", "16:9", "4:3", "3:4", "1:1", "2:3", "3:2"):
        adapter.submit(model=_SOUL_STANDARD, settings={"prompt": "x", "aspect_ratio": ratio},
                        reference_assets=())


@pytest.mark.parametrize("stale_ratio", ["5:4", "4:5", "21:9", "auto"])
def test_submit_soul_standard_stale_aspect_ratio_values_rejected(stale_ratio):
    """The prior catalog's 10-value aspect_ratio set (and nano-banana's own
    'auto' value) are not part of soul/standard's real, current schema."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model=_SOUL_STANDARD, settings={"prompt": "x", "aspect_ratio": stale_ratio},
                        reference_assets=())


# ===========================================================================
# CATALOG TRUTH -- nano-banana decision (required cases 12-14)
# ===========================================================================

def test_supported_models_advertises_only_soul_standard():
    """Required case 12: nano-banana removed -- the production catalog
    tells the truth about what this adapter can actually submit."""
    assert ha.SUPPORTED_MODELS == ("higgsfield-ai/soul/standard",)


def test_nano_banana_no_longer_supported():
    """Required case 13: nano-banana's OpenAPI route is still technically
    declared, but a live smoke preflight returned 404 model_not_found and
    no current official source (dedicated docs page, docs-site search, or
    the console's live model/pricing catalog) documents it as a supported
    production model -- removed rather than kept as a known-dead entry.
    See MULTIMODAL_M4_HIGGSFIELD_CATALOG_SCHEMA_ALIGNMENT_01_2026-09-18.md."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    assert "nano-banana" not in ha.SUPPORTED_MODELS
    assert adapter.describe_model("nano-banana") is None
    with pytest.raises(ha.UnsupportedModelError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())


# ===========================================================================
# POLL
# ===========================================================================

@pytest.mark.parametrize("raw,expected_canonical", [
    ("queued", gj.STATUS_QUEUED),
    ("in_progress", gj.STATUS_RUNNING),
    ("completed", gj.STATUS_SUCCEEDED),
    ("failed", gj.STATUS_FAILED),
    ("canceled", gj.STATUS_CANCELLED),
])
def test_poll_every_documented_state_mapping(raw, expected_canonical):
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-1/status", _json_response(200, {"status": raw, "request_id": "req-1"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    result = adapter.poll_detailed("req-1")
    assert result.canonical_status == expected_canonical
    assert result.raw_status == raw
    assert adapter.poll("req-1") == raw  # Protocol-mandated bare poll returns the raw string


def test_poll_nsfw_maps_to_failed_content_policy():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-1/status", _json_response(200, {"status": "nsfw", "request_id": "req-1"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    result = adapter.poll_detailed("req-1")
    assert result.canonical_status == gj.STATUS_FAILED
    assert result.failure_class == "content_policy"
    assert adapter.poll("req-1") == "nsfw"  # bare poll never invents a translated string either


def test_poll_unknown_state_never_guessed():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-1/status",
                         _json_response(200, {"status": "some_brand_new_status", "request_id": "req-1"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    result = adapter.poll_detailed("req-1")
    assert result.canonical_status == gj.STATUS_UNKNOWN
    assert result.canonical_status not in (gj.STATUS_SUCCEEDED, gj.STATUS_FAILED)


def test_poll_reattach_without_original_submission_call():
    """Mandatory restart/reattach property: needs nothing but
    provider_job_id -- a fresh adapter instance that never called submit()
    for this job must still be able to poll it."""
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/orphan-job-from-before-restart/status",
                         _json_response(200, {"status": "completed", "request_id": "orphan-job-from-before-restart"}))
    fresh_adapter = ha.HiggsfieldAdapter(transport=transport)
    result = fresh_adapter.poll_detailed("orphan-job-from-before-restart")
    assert result.canonical_status == gj.STATUS_SUCCEEDED


def test_poll_malformed_response_missing_status():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-1/status", _json_response(200, {"request_id": "req-1"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.poll_detailed("req-1")


# ===========================================================================
# CANCEL
# ===========================================================================

def test_cancel_queued_job_terminal_cancelled():
    job = _queued_job(provider_job_id="req-c1")
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/requests/req-c1/cancel", _raw_response(202, b""))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    updated = adapter.cancel(job)
    assert updated.status == gj.STATUS_CANCELLED
    assert gj.get_generation_job(job.lumina_job_id).status == gj.STATUS_CANCELLED


def test_cancel_running_job_terminal_cancelled():
    job = _running_job(provider_job_id="req-c2")
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/requests/req-c2/cancel", _raw_response(202, b""))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    updated = adapter.cancel(job)
    assert updated.status == gj.STATUS_CANCELLED


def test_cancel_rejected_already_processing():
    job = _running_job(provider_job_id="req-c3")
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/requests/req-c3/cancel",
                         _json_response(400, {"detail": "The request has already started and can no longer be canceled."}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    updated = adapter.cancel(job)
    assert updated.status == gj.STATUS_RUNNING  # unchanged
    assert gj.get_generation_job(job.lumina_job_id).status == gj.STATUS_RUNNING


def test_cancel_accepted_but_not_yet_terminal():
    """Higgsfield's real cancel endpoint has no such state (it's a
    synchronous 202-confirm-or-400-reject), but the adapter's contract
    stays honest for any provider whose 'success' status isn't the
    documented 202: the job is left non-terminal, unchanged, for a later
    poll() to resolve."""
    job = _queued_job(provider_job_id="req-c4")
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/requests/req-c4/cancel", _json_response(200, {"note": "accepted, processing"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    updated = adapter.cancel(job)
    assert updated.status == gj.STATUS_QUEUED  # unchanged, not promoted to cancelled


def test_cancel_terminal_succeeded_refused():
    job = _succeeded_job()
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(gj.JobStateConflict):
        adapter.cancel(job)


def test_cancel_terminal_failed_refused():
    job = _failed_job()
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(gj.JobStateConflict):
        adapter.cancel(job)


def test_cancel_unknown_response_fails_closed():
    job = _queued_job(provider_job_id="req-c5")
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/requests/req-c5/cancel", _json_response(418, {"detail": "??"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError):
        adapter.cancel(job)
    # job untouched by the failed attempt
    assert gj.get_generation_job(job.lumina_job_id).status == gj.STATUS_QUEUED


# ===========================================================================
# COST -- MULTIMODAL-M4-HIGGSFIELD-PRICING-REPAIR-01
#
# estimate_cost() is now a pure, local, zero-network lookup (the old remote
# /estimate/{model} endpoint returned live HTTP 404 -- see
# MULTIMODAL_M4_HIGGSFIELD_PRICING_REPAIR_01_2026-09-18.md). A transport
# that raises on ANY call is used throughout this section, proving the
# no-network-call law directly rather than merely by absence of configured
# responses.
# ===========================================================================

class _NoNetworkTransport:
    """Every method raises immediately -- proves estimate_cost() for a
    locally-priced model never reaches the transport at all (required
    case 10)."""

    def post(self, *a, **kw):
        raise AssertionError("estimate_cost() must not make an HTTP call for a locally priced model")

    def get(self, *a, **kw):
        raise AssertionError("estimate_cost() must not make an HTTP call for a locally priced model")

    def put_bytes(self, *a, **kw):
        raise AssertionError("estimate_cost() must not make an HTTP call for a locally priced model")

    def get_raw(self, *a, **kw):
        raise AssertionError("estimate_cost() must not make an HTTP call for a locally priced model")


def test_estimate_soul_standard_cheapest_configuration_exact_cost():
    """Required case 1: cheapest supported Soul Standard configuration
    (720p, default batch_size=1) returns the exact documented price."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(model="higgsfield-ai/soul/standard", settings={"prompt": "x"})
    assert estimate == pytest.approx(0.0938)


def test_estimate_soul_standard_other_documented_resolution_exact_cost():
    """Required case 2: the other documented resolution tier (1080p)."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(
        model="higgsfield-ai/soul/standard", settings={"prompt": "x", "resolution": "1080p"},
    )
    assert estimate == pytest.approx(0.1875)


@pytest.mark.parametrize("resolution,expected", [("720p", 0.3752), ("1080p", 0.75)])
def test_estimate_soul_standard_batch_size_scales_linearly(resolution, expected):
    """Required case 3: batch_size (the real documented parameter name)
    scales the per-image price exactly, at both documented resolutions."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(
        model="higgsfield-ai/soul/standard",
        settings={"prompt": "x", "resolution": resolution, "batch_size": 4},
    )
    assert estimate == pytest.approx(expected)


def test_estimate_soul_standard_num_images_alias_scales_too():
    """This adapter's own (stale) _MODEL_CATALOG key is 'num_images', not
    'batch_size' -- pricing accepts either name for the count so a caller
    constrained by today's submit() validation still gets priced."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(
        model="higgsfield-ai/soul/standard", settings={"prompt": "x", "num_images": 4},
    )
    assert estimate == pytest.approx(0.3752)


def test_estimate_soul_standard_conflicting_count_aliases_fails_closed():
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(
        model="higgsfield-ai/soul/standard",
        settings={"prompt": "x", "batch_size": 1, "num_images": 4},
    )
    assert estimate is None


def test_estimate_unsupported_resolution_fails_closed():
    """Required case 4: this catalog's own currently-valid (but real-API-
    stale) '2K'/'4K' values have no documented price -- refused rather
    than guessed."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    for bogus_resolution in ("2K", "4K", "8K", "some-future-tier"):
        assert adapter.estimate_cost(
            model="higgsfield-ai/soul/standard",
            settings={"prompt": "x", "resolution": bogus_resolution},
        ) is None


def test_estimate_unknown_model_returns_none():
    """Required case 5."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    assert adapter.estimate_cost(model="totally-bogus-model-not-in-any-catalog", settings={"prompt": "x"}) is None


@pytest.mark.parametrize("batch_size", [0, 2, 3, 5, True, False, 1.0, "1", None])
def test_estimate_pricing_sensitive_unknown_parameter_combination_returns_none(batch_size):
    """Required case 6: batch sizes Higgsfield doesn't document (only 1
    and 4 are real), plus bool-as-int and other type ambiguity, all fail
    closed rather than being guessed at."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    assert adapter.estimate_cost(
        model="higgsfield-ai/soul/standard", settings={"prompt": "x", "batch_size": batch_size},
    ) is None


def test_estimate_nano_banana_always_none():
    """nano-banana's current identity/price could not be verified against
    any current official Higgsfield source (see the evidence doc) --
    estimate_cost() never guesses a price for it."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    assert adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"}) is None


def test_estimate_never_makes_an_http_request_for_a_locally_priced_model():
    """Required case 10, proven directly: _NoNetworkTransport raises on
    ANY call, so simply not raising here proves zero network calls."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    adapter.estimate_cost(model="higgsfield-ai/soul/standard", settings={"prompt": "x", "resolution": "1080p"})
    adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"})
    adapter.estimate_cost(model="unknown-model", settings={"prompt": "x"})
    # no AssertionError raised above == no transport method was ever called


def test_estimate_never_autonomously_approves_spend():
    """The adapter returns a number (or None); SpendingPolicy, evaluated
    by the caller, is what actually allows or blocks submission -- the
    adapter has no opinion and no gate of its own."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(model="higgsfield-ai/soul/standard", settings={"prompt": "x", "resolution": "1080p"})
    policy = sp.SpendingPolicy(single_job_ceiling=0.10)
    decision = sp.evaluate_spend(policy, estimate)
    assert decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL  # adapter never decided this itself


def test_spending_policy_permits_estimate_below_ceiling():
    """Required case 7."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(model="higgsfield-ai/soul/standard", settings={"prompt": "x"})  # 0.0938
    policy = sp.SpendingPolicy(single_job_ceiling=1.0)
    decision = sp.evaluate_spend(policy, estimate)
    assert decision.outcome == sp.OUTCOME_ALLOWED


def test_spending_policy_rejects_estimate_above_ceiling():
    """Required case 8."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(
        model="higgsfield-ai/soul/standard", settings={"prompt": "x", "resolution": "1080p", "batch_size": 4},
    )  # 0.75
    policy = sp.SpendingPolicy(single_job_ceiling=0.50)
    decision = sp.evaluate_spend(policy, estimate)
    assert decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL


def test_no_estimate_fail_closed_behavior_remains_intact():
    """Required case 9: core.generation_spending_policy itself is
    untouched by this repair -- an unavailable estimate (nano-banana,
    or any unknown model) still fails closed exactly as before, both in
    its default (deny) and opt-out (require approval) configurations."""
    adapter = ha.HiggsfieldAdapter(transport=_NoNetworkTransport())
    estimate = adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"})
    assert estimate is None

    deny_policy = sp.SpendingPolicy(require_estimate=True, fail_closed_on_unknown_cost=True)
    assert sp.evaluate_spend(deny_policy, estimate).outcome == sp.OUTCOME_DENIED

    approval_policy = sp.SpendingPolicy(require_estimate=True, fail_closed_on_unknown_cost=False)
    assert sp.evaluate_spend(approval_policy, estimate).outcome == sp.OUTCOME_REQUIRES_APPROVAL


# ===========================================================================
# UPLOAD -- exercised via a test-only synthetic catalog entry.
#
# The adapter's real v1 catalog has exactly one model
# (higgsfield-ai/soul/standard), which does not accept reference images
# (reference_role=None). nano-banana was the only catalog entry that ever
# did, and it was removed (see the CATALOG TRUTH section above). The
# presigned-upload/reference-payload mechanism itself is still real,
# general adapter code -- not nano-banana-specific -- so it stays covered
# here via a synthetic, clearly-test-only catalog entry injected through
# monkeypatch. Never a real Higgsfield model identity; never implies any
# production model currently accepts a reference image through this
# adapter.
# ===========================================================================

_TEST_REFERENCE_MODEL = "test-only/reference-model"


@pytest.fixture
def reference_model(monkeypatch):
    monkeypatch.setitem(ha._MODEL_CATALOG, _TEST_REFERENCE_MODEL, {
        "path": "/test-only/reference-model",
        "capability": Capability.IMAGE_GENERATION.value,
        "required_params": frozenset({"prompt"}),
        "optional_params": {},
        "reference_role": "input_images",
        "reference_max": 8,
        "cost_estimate_supported": False,
    })
    return _TEST_REFERENCE_MODEL


def _ingest_local_reference_artifact():
    job = _succeeded_job(provider_job_id="req-ref-source")
    return ga.ingest_artifact(job.lumina_job_id, b"\x89PNGfake-reference-bytes", "image/png")


def test_upload_supported_local_media_full_presigned_flow(reference_model):
    artifact = _ingest_local_reference_artifact()
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/files/generate-upload-url", _json_response(200, {
        "public_url": "https://cdn.example.com/input/uploaded.png",
        "upload_url": "https://storage.example.com/presigned-upload-url",
        "content_type": "image/png",
        "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"},
    }))
    transport.configure("PUT", "https://storage.example.com/presigned-upload-url", _raw_response(200, b""))
    transport.configure("POST", "/test-only/reference-model",
                         _json_response(200, {"status": "queued", "request_id": "req-up-1"}))

    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, _ = adapter.submit(
        model=reference_model, settings={"prompt": "stylize this"},
        reference_assets=(("style", artifact.artifact_id),),
    )
    assert request_id == "req-up-1"

    upload_call = next(c for c in transport.calls if c["method"] == "PUT")
    assert upload_call["url"] == "https://storage.example.com/presigned-upload-url"
    assert upload_call["data"] == b"\x89PNGfake-reference-bytes"
    assert upload_call["headers"] == {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"}

    submit_call = next(c for c in transport.calls if c["method"] == "POST" and c["path"] == "/test-only/reference-model")
    assert submit_call["json"]["input_images"] == [
        {"type": "image_url", "image_url": "https://cdn.example.com/input/uploaded.png"}
    ]


def test_upload_no_local_path_leaked_into_final_request(reference_model):
    artifact = _ingest_local_reference_artifact()
    assert artifact.local_path  # sanity: the artifact really has a local filesystem path
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/files/generate-upload-url", _json_response(200, {
        "public_url": "https://cdn.example.com/input/uploaded.png",
        "upload_url": "https://storage.example.com/presigned-upload-url",
        "content_type": "image/png",
        "upload_headers": {"Content-Type": "image/png"},
    }))
    transport.configure("PUT", "https://storage.example.com/presigned-upload-url", _raw_response(200, b""))
    transport.configure("POST", "/test-only/reference-model",
                         _json_response(200, {"status": "queued", "request_id": "req-up-2"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    adapter.submit(model=reference_model, settings={"prompt": "x"},
                    reference_assets=(("style", artifact.artifact_id),))
    for call in transport.calls:
        serialized = json.dumps({k: v for k, v in call.items() if k != "data"}, default=str)
        assert artifact.local_path not in serialized


def test_upload_rejected_model_without_reference_support():
    artifact = _ingest_local_reference_artifact()
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"},
                        reference_assets=(("identity", artifact.artifact_id),))


def test_upload_rejected_too_many_references(reference_model):
    artifacts = [_ingest_local_reference_artifact() for _ in range(9)]  # test model max is 8
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model=reference_model, settings={"prompt": "x"},
                        reference_assets=tuple(("ref", a.artifact_id) for a in artifacts))


def test_upload_rejects_unknown_artifact_id(reference_model):
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ga.ArtifactNotFound):
        adapter.submit(model=reference_model, settings={"prompt": "x"},
                        reference_assets=(("style", "does-not-exist"),))


def test_upload_presigned_put_never_receives_higgsfield_auth_header(reference_model):
    """file-uploads.md: 'Do not send Higgsfield API credentials to the
    presigned storage URL.' Proven against the REAL transport (with real
    credential strings) and real `requests` calls, all monkeypatched."""
    posted, put = {}, {}

    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        posted.update(url=url, headers=headers)
        if "generate-upload-url" in url:
            body = {
                "public_url": "https://cdn.example.com/x.png",
                "upload_url": "https://storage.example.com/presigned",
                "content_type": "image/png",
                "upload_headers": {"Content-Type": "image/png"},
            }
        else:
            body = {"status": "queued", "request_id": "req-real-1"}
        return _FakeRequestsResponse(200, {}, json_module.dumps(body).encode())

    def fake_put(url, data=None, headers=None, timeout=None):
        put.update(url=url, headers=headers)
        return _FakeRequestsResponse(200, {}, b"")

    import json as json_module
    import core.higgsfield_transport as ht_module
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(ht_module.requests, "post", fake_post)
        mp.setattr(ht_module.requests, "put", fake_put)
        transport = ht_module.RequestsHiggsfieldTransport("real-key-id-not-a-fixture", "real-key-secret-not-a-fixture")
        adapter = ha.HiggsfieldAdapter(transport=transport)
        artifact = _ingest_local_reference_artifact()
        adapter.submit(model=reference_model, settings={"prompt": "x"},
                        reference_assets=(("style", artifact.artifact_id),))
    finally:
        mp.undo()

    assert put["url"] == "https://storage.example.com/presigned"
    assert "Authorization" not in put["headers"]
    assert "real-key-id-not-a-fixture" not in json_module.dumps(put["headers"])


# ===========================================================================
# OUTPUT
# ===========================================================================

def test_list_outputs_single_output():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o1/status", _json_response(200, {
        "status": "completed", "request_id": "req-o1",
        "images": [{"url": "https://cdn.example.com/img-0.jpg"}],
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    outputs = adapter.list_outputs("req-o1")
    assert len(outputs) == 1
    assert outputs[0].index == 0
    assert outputs[0].url == "https://cdn.example.com/img-0.jpg"
    assert outputs[0].kind == "image"


def test_list_outputs_multiple_outputs_stable_order():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o2/status", _json_response(200, {
        "status": "completed", "request_id": "req-o2",
        "images": [
            {"url": "https://cdn.example.com/img-0.jpg"},
            {"url": "https://cdn.example.com/img-1.jpg"},
            {"url": "https://cdn.example.com/img-2.jpg"},
            {"url": "https://cdn.example.com/img-3.jpg"},
        ],
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    outputs = adapter.list_outputs("req-o2")
    assert [o.index for o in outputs] == [0, 1, 2, 3]
    assert [o.url for o in outputs] == [
        "https://cdn.example.com/img-0.jpg", "https://cdn.example.com/img-1.jpg",
        "https://cdn.example.com/img-2.jpg", "https://cdn.example.com/img-3.jpg",
    ]
    # order is stable across repeated calls, not just present once
    assert [o.index for o in adapter.list_outputs("req-o2")] == [0, 1, 2, 3]


def test_list_outputs_zero_outputs_on_succeeded():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o3/status",
                         _json_response(200, {"status": "completed", "request_id": "req-o3"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    assert adapter.list_outputs("req-o3") == []


def test_list_outputs_malformed_entry_raises_not_silently_skipped():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o4/status", _json_response(200, {
        "status": "completed", "request_id": "req-o4",
        "images": [{"url": "https://cdn.example.com/ok.jpg"}, {"not_url": "missing"}],
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.list_outputs("req-o4")


def test_list_outputs_requires_succeeded_status():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o5/status",
                         _json_response(200, {"status": "queued", "request_id": "req-o5"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldAdapterError):
        adapter.list_outputs("req-o5")


def test_fetch_result_single_output():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o6/status", _json_response(200, {
        "status": "completed", "request_id": "req-o6",
        "images": [{"url": "https://cdn.example.com/only.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/only.jpg",
                         _raw_response(200, b"fake-jpeg-bytes", headers={"Content-Type": "image/jpeg"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    data, mime_type = adapter.fetch_result("req-o6")
    assert data == b"fake-jpeg-bytes"
    assert mime_type == "image/jpeg"


def test_fetch_result_raises_on_multiple_outputs():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o7/status", _json_response(200, {
        "status": "completed", "request_id": "req-o7",
        "images": [{"url": "https://cdn.example.com/a.jpg"}, {"url": "https://cdn.example.com/b.jpg"}],
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.MultipleOutputsError):
        adapter.fetch_result("req-o7")


def test_fetch_output_by_index_and_mime_from_header_not_json():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o8/status", _json_response(200, {
        "status": "completed", "request_id": "req-o8",
        "images": [{"url": "https://cdn.example.com/a.jpg"}, {"url": "https://cdn.example.com/b.png"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/a.jpg",
                         _raw_response(200, b"bytes-a", headers={"Content-Type": "image/jpeg; charset=binary"}))
    transport.configure("GET_RAW", "https://cdn.example.com/b.png",
                         _raw_response(200, b"bytes-b", headers={"content-type": "image/png"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    data_a, mime_a = adapter.fetch_output("req-o8", 0)
    data_b, mime_b = adapter.fetch_output("req-o8", 1)
    assert (data_a, mime_a) == (b"bytes-a", "image/jpeg")
    assert (data_b, mime_b) == (b"bytes-b", "image/png")


def test_fetch_output_index_out_of_range():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o9/status", _json_response(200, {
        "status": "completed", "request_id": "req-o9", "images": [{"url": "https://cdn.example.com/only.jpg"}],
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldAdapterError):
        adapter.fetch_output("req-o9", 5)


def test_remote_url_stays_non_durable_until_ingestion():
    """fetch_output() only returns bytes -- it never calls
    core.generation_artifact.ingest_artifact() itself. Proven by ingesting
    the returned bytes afterward through the real substrate and confirming
    that's a separate, deliberate step."""
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o10/status", _json_response(200, {
        "status": "completed", "request_id": "req-o10", "images": [{"url": "https://cdn.example.com/only.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/only.jpg",
                         _raw_response(200, b"remote-bytes", headers={"Content-Type": "image/jpeg"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    job = _succeeded_job(provider_job_id="req-o10")

    data, mime_type = adapter.fetch_output("req-o10", 0)
    assert ga.list_artifacts_for_job(job.lumina_job_id) == []  # not durable yet -- adapter didn't ingest

    artifact = ga.ingest_artifact(job.lumina_job_id, data, mime_type, provider_output_index=0)
    assert ga.get_artifact_bytes(artifact.artifact_id) == b"remote-bytes"


def test_fetch_output_http_error_from_cdn():
    transport = FakeHiggsfieldTransport()
    transport.configure("GET", "/requests/req-o11/status", _json_response(200, {
        "status": "completed", "request_id": "req-o11", "images": [{"url": "https://cdn.example.com/gone.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/gone.jpg", _raw_response(404, b"not found"))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldAdapterError):
        adapter.fetch_output("req-o11", 0)


# ===========================================================================
# ERRORS
# ===========================================================================

def test_error_401_auth_failure():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(401, {"detail": "Invalid credentials"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_AUTH_FAILURE
    assert excinfo.value.status_code == 401


def test_error_403_insufficient_credits():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(403, {"detail": "Insufficient credits"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_INSUFFICIENT_CREDITS


def test_error_400_validation_failure():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(400, {"detail": "Invalid aspect_ratio"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_VALIDATION_FAILURE


def test_error_400_concurrency_classified_as_rate_limited():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard",
                         _json_response(400, {"detail": "Maximum number of concurrent requests (4) has been reached"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_RATE_LIMITED


def test_error_422_validation_failure():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(422, {"detail": [{"msg": "field required"}]}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_VALIDATION_FAILURE


def test_error_429_rate_limited_with_retry_after():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard",
                         _json_response(429, {"detail": "Too many requests"}, headers={"Retry-After": "30"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_RATE_LIMITED
    assert excinfo.value.retry_after == 30.0


def test_error_5xx_provider_error():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(500, {"detail": "Unexpected server error"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_PROVIDER_ERROR
    assert excinfo.value.status_code == 500


def test_error_503_model_unavailable():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(503, {"detail": "Model is disabled or not ready"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_MODEL_UNAVAILABLE


def test_error_timeout(monkeypatch):
    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        raise ht.requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(ht.requests, "post", fake_post)
    transport = ht.RequestsHiggsfieldTransport("k", "s")
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ht.HiggsfieldTimeoutError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())


def test_error_connection_failure(monkeypatch):
    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        raise ht.requests.exceptions.ConnectionError("simulated connection failure")

    monkeypatch.setattr(ht.requests, "post", fake_post)
    transport = ht.RequestsHiggsfieldTransport("k", "s")
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ht.HiggsfieldConnectionError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())


def test_error_malformed_json_body():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _raw_response(200, b"this is not json"))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())


def test_error_body_not_json_object():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _raw_response(200, b"[1, 2, 3]"))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"}, reference_assets=())


# ===========================================================================
# describe_model()
# ===========================================================================

def test_describe_model_known():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    desc = adapter.describe_model("higgsfield-ai/soul/standard")
    assert desc["provider"] == "higgsfield"
    assert desc["capability"] == Capability.IMAGE_GENERATION.value
    assert desc["reference_role"] is None
    assert "source" in desc and "openapi.json" in desc["source"]


def test_describe_model_unknown_returns_none():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    assert adapter.describe_model("some-cli-only-model") is None


def test_describe_model_never_claims_cli_only_catalog():
    """The CLI's much larger catalog (23 image models, Marketing Studio,
    Virality Predictor, 3D, audio) is never advertised through this
    REST-only adapter."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    assert adapter.describe_model("gpt_image_2_5") is None
    assert adapter.describe_model("brain_activity") is None
    assert set(ha.SUPPORTED_MODELS) == {"higgsfield-ai/soul/standard"}


# ===========================================================================
# OFFLINE LAW
# ===========================================================================

def test_offline_law_full_adapter_lifecycle_never_touches_a_socket(monkeypatch):
    """Patches socket.socket to raise if constructed at all, then drives a
    full submit -> poll -> list_outputs -> fetch_output -> cancel(on a
    fresh job) -> estimate_cost lifecycle through FakeHiggsfieldTransport.
    Proves the entire adapter test suite's I/O path is genuinely mockable,
    not merely "usually doesn't happen to call requests."""

    def _no_sockets(*args, **kwargs):
        raise AssertionError("adapter test attempted to open a real network socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)

    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard",
                         _json_response(200, {"status": "queued", "request_id": "req-offline"}))
    transport.configure("GET", "/requests/req-offline/status", _json_response(200, {
        "status": "completed", "request_id": "req-offline",
        "images": [{"url": "https://cdn.example.com/offline.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/offline.jpg",
                         _raw_response(200, b"offline-bytes", headers={"Content-Type": "image/jpeg"}))
    transport.configure("POST", "/requests/req-offline-2/cancel", _raw_response(202, b""))

    adapter = ha.HiggsfieldAdapter(transport=transport)

    request_id, raw_status = adapter.submit(model="higgsfield-ai/soul/standard", settings={"prompt": "x"},
                                             reference_assets=())
    assert request_id == "req-offline"
    result = adapter.poll_detailed(request_id)
    assert result.canonical_status == gj.STATUS_SUCCEEDED
    data, mime_type = adapter.fetch_output(request_id, 0)
    assert data == b"offline-bytes"
    # estimate_cost() is now local/zero-network (MULTIMODAL-M4-HIGGSFIELD-
    # PRICING-REPAIR-01) -- no transport response to configure for it.
    estimate = adapter.estimate_cost(model="higgsfield-ai/soul/standard", settings={"prompt": "x"})
    assert estimate == pytest.approx(0.0938)

    job = _queued_job(provider_job_id="req-offline-2")
    cancelled = adapter.cancel(job)
    assert cancelled.status == gj.STATUS_CANCELLED
