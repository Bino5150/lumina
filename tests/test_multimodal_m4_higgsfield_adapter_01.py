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


def _begin(model="nano-banana", settings=None, refs=(), **kw):
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
    transport.post("/nano-banana", json={"prompt": "x"})
    assert captured["headers"]["Authorization"] == "Key my-key-id:my-key-secret"
    assert captured["url"] == "https://api.higgsfield.ai/nano-banana"
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
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert "id-should-not-leak-2" not in str(excinfo.value)
    assert "secret-should-not-leak-2" not in str(excinfo.value)


# ===========================================================================
# SUBMIT
# ===========================================================================

def test_submit_valid_image_generation_request():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(200, {
        "status": "queued", "request_id": "req-abc",
        "status_url": "https://api.higgsfield.ai/requests/req-abc/status",
        "cancel_url": "https://api.higgsfield.ai/requests/req-abc/cancel",
    }))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, raw_status = adapter.submit(
        model="nano-banana", settings={"prompt": "a lighthouse", "aspect_ratio": "16:9"},
        reference_assets=(),
    )
    assert request_id == "req-abc"
    assert raw_status == "queued"
    assert transport.calls[0]["json"] == {"prompt": "a lighthouse", "aspect_ratio": "16:9"}


def test_submit_captures_provider_job_id_into_generation_job():
    job = _begin(model="nano-banana", settings={"prompt": "a lighthouse"})
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(200, {"status": "queued", "request_id": "req-xyz"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, raw_status = adapter.submit(model="nano-banana", settings={"prompt": "a lighthouse"},
                                             reference_assets=())
    updated = gj.mark_submitted(job.lumina_job_id, request_id, raw_status)
    assert updated.provider_job_id == "req-xyz"
    assert updated.status == gj.STATUS_QUEUED


def test_submit_malformed_response_missing_request_id():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(200, {"status": "queued"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())


def test_submit_unsupported_parameter_fails_explicitly():
    transport = FakeHiggsfieldTransport()
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="nano-banana", settings={"prompt": "x", "totally_bogus_param": 1},
                        reference_assets=())
    assert transport.calls == []  # rejected client-side, no HTTP call made


def test_submit_missing_required_parameter_fails_explicitly():
    transport = FakeHiggsfieldTransport()
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="nano-banana", settings={"aspect_ratio": "16:9"}, reference_assets=())
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
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=(),
                        provider_idempotency_key="some-key")


def test_submit_param_value_out_of_range_fails_explicitly():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="nano-banana", settings={"prompt": "x", "num_images": 99},
                        reference_assets=())


def test_submit_param_enum_violation_fails_explicitly():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="nano-banana", settings={"prompt": "x", "aspect_ratio": "not-a-real-ratio"},
                        reference_assets=())


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
# COST
# ===========================================================================

def test_estimate_valid():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/estimate/nano-banana", _json_response(200, {"credits": "1.500", "usd": "0.094"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    estimate = adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"})
    assert estimate == pytest.approx(0.094)


def test_estimate_missing_usd_field_fails_closed():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/estimate/nano-banana", _json_response(200, {"credits": "1.500"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    assert adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"}) is None


def test_estimate_malformed_json_fails_closed():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/estimate/nano-banana", _raw_response(200, b"not json at all"))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    assert adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"}) is None


def test_estimate_http_error_fails_closed():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/estimate/nano-banana", _json_response(500, {"detail": "boom"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    assert adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"}) is None


def test_estimate_unsupported_parameter_still_raises():
    """A caller bug (unknown parameter) is not the same as 'estimate
    unavailable' -- it raises exactly like submit() would for the same
    settings, rather than silently returning None."""
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.estimate_cost(model="nano-banana", settings={"prompt": "x", "bogus": 1})


def test_estimate_never_autonomously_approves_spend():
    """The adapter returns a number (or None); SpendingPolicy, evaluated
    by the caller, is what actually allows or blocks submission -- the
    adapter has no opinion and no gate of its own."""
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/estimate/nano-banana", _json_response(200, {"credits": "160.0", "usd": "9.99"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    estimate = adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"})
    policy = sp.SpendingPolicy(single_job_ceiling=1.0)
    decision = sp.evaluate_spend(policy, estimate)
    assert decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL  # adapter never decided this itself


# ===========================================================================
# UPLOAD
# ===========================================================================

def _ingest_local_reference_artifact():
    job = _succeeded_job(provider_job_id="req-ref-source")
    return ga.ingest_artifact(job.lumina_job_id, b"\x89PNGfake-reference-bytes", "image/png")


def test_upload_supported_local_media_full_presigned_flow():
    artifact = _ingest_local_reference_artifact()
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/files/generate-upload-url", _json_response(200, {
        "public_url": "https://cdn.example.com/input/uploaded.png",
        "upload_url": "https://storage.example.com/presigned-upload-url",
        "content_type": "image/png",
        "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"},
    }))
    transport.configure("PUT", "https://storage.example.com/presigned-upload-url", _raw_response(200, b""))
    transport.configure("POST", "/nano-banana", _json_response(200, {"status": "queued", "request_id": "req-up-1"}))

    adapter = ha.HiggsfieldAdapter(transport=transport)
    request_id, _ = adapter.submit(
        model="nano-banana", settings={"prompt": "stylize this"},
        reference_assets=(("style", artifact.artifact_id),),
    )
    assert request_id == "req-up-1"

    upload_call = next(c for c in transport.calls if c["method"] == "PUT")
    assert upload_call["url"] == "https://storage.example.com/presigned-upload-url"
    assert upload_call["data"] == b"\x89PNGfake-reference-bytes"
    assert upload_call["headers"] == {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"}

    submit_call = next(c for c in transport.calls if c["method"] == "POST" and c["path"] == "/nano-banana")
    assert submit_call["json"]["input_images"] == [
        {"type": "image_url", "image_url": "https://cdn.example.com/input/uploaded.png"}
    ]


def test_upload_no_local_path_leaked_into_final_request():
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
    transport.configure("POST", "/nano-banana", _json_response(200, {"status": "queued", "request_id": "req-up-2"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    adapter.submit(model="nano-banana", settings={"prompt": "x"},
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


def test_upload_rejected_too_many_references():
    artifacts = [_ingest_local_reference_artifact() for _ in range(9)]  # nano-banana max is 8
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ha.UnsupportedParameterError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"},
                        reference_assets=tuple(("ref", a.artifact_id) for a in artifacts))


def test_upload_rejects_unknown_artifact_id():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    with pytest.raises(ga.ArtifactNotFound):
        adapter.submit(model="nano-banana", settings={"prompt": "x"},
                        reference_assets=(("style", "does-not-exist"),))


def test_upload_presigned_put_never_receives_higgsfield_auth_header():
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
        adapter.submit(model="nano-banana", settings={"prompt": "x"},
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
    transport.configure("POST", "/nano-banana", _json_response(401, {"detail": "Invalid credentials"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_AUTH_FAILURE
    assert excinfo.value.status_code == 401


def test_error_403_insufficient_credits():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(403, {"detail": "Insufficient credits"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_INSUFFICIENT_CREDITS


def test_error_400_validation_failure():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(400, {"detail": "Invalid aspect_ratio"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_VALIDATION_FAILURE


def test_error_400_concurrency_classified_as_rate_limited():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana",
                         _json_response(400, {"detail": "Maximum number of concurrent requests (4) has been reached"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_RATE_LIMITED


def test_error_422_validation_failure():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(422, {"detail": [{"msg": "field required"}]}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_VALIDATION_FAILURE


def test_error_429_rate_limited_with_retry_after():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana",
                         _json_response(429, {"detail": "Too many requests"}, headers={"Retry-After": "30"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_RATE_LIMITED
    assert excinfo.value.retry_after == 30.0


def test_error_5xx_provider_error():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(500, {"detail": "Unexpected server error"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_PROVIDER_ERROR
    assert excinfo.value.status_code == 500


def test_error_503_model_unavailable():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _json_response(503, {"detail": "Model is disabled or not ready"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldProviderError) as excinfo:
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert excinfo.value.kind == ha.ERROR_MODEL_UNAVAILABLE


def test_error_timeout(monkeypatch):
    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        raise ht.requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(ht.requests, "post", fake_post)
    transport = ht.RequestsHiggsfieldTransport("k", "s")
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ht.HiggsfieldTimeoutError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())


def test_error_connection_failure(monkeypatch):
    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        raise ht.requests.exceptions.ConnectionError("simulated connection failure")

    monkeypatch.setattr(ht.requests, "post", fake_post)
    transport = ht.RequestsHiggsfieldTransport("k", "s")
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ht.HiggsfieldConnectionError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())


def test_error_malformed_json_body():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _raw_response(200, b"this is not json"))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())


def test_error_body_not_json_object():
    transport = FakeHiggsfieldTransport()
    transport.configure("POST", "/nano-banana", _raw_response(200, b"[1, 2, 3]"))
    adapter = ha.HiggsfieldAdapter(transport=transport)
    with pytest.raises(ha.HiggsfieldMalformedResponseError):
        adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())


# ===========================================================================
# describe_model()
# ===========================================================================

def test_describe_model_known():
    adapter = ha.HiggsfieldAdapter(transport=FakeHiggsfieldTransport())
    desc = adapter.describe_model("nano-banana")
    assert desc["provider"] == "higgsfield"
    assert desc["capability"] == Capability.IMAGE_GENERATION.value
    assert desc["reference_role"] == "input_images"
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
    assert set(ha.SUPPORTED_MODELS) == {"nano-banana", "higgsfield-ai/soul/standard"}


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
    transport.configure("POST", "/nano-banana", _json_response(200, {"status": "queued", "request_id": "req-offline"}))
    transport.configure("GET", "/requests/req-offline/status", _json_response(200, {
        "status": "completed", "request_id": "req-offline",
        "images": [{"url": "https://cdn.example.com/offline.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/offline.jpg",
                         _raw_response(200, b"offline-bytes", headers={"Content-Type": "image/jpeg"}))
    transport.configure("POST", "/estimate/nano-banana", _json_response(200, {"credits": "1.0", "usd": "0.05"}))
    transport.configure("POST", "/requests/req-offline-2/cancel", _raw_response(202, b""))

    adapter = ha.HiggsfieldAdapter(transport=transport)

    request_id, raw_status = adapter.submit(model="nano-banana", settings={"prompt": "x"}, reference_assets=())
    assert request_id == "req-offline"
    result = adapter.poll_detailed(request_id)
    assert result.canonical_status == gj.STATUS_SUCCEEDED
    data, mime_type = adapter.fetch_output(request_id, 0)
    assert data == b"offline-bytes"
    estimate = adapter.estimate_cost(model="nano-banana", settings={"prompt": "x"})
    assert estimate == pytest.approx(0.05)

    job = _queued_job(provider_job_id="req-offline-2")
    cancelled = adapter.cancel(job)
    assert cancelled.status == gj.STATUS_CANCELLED
