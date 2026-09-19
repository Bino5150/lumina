"""
tests/test_media_generation_conversational_runtime_01.py --
MEDIA-GENERATION-CONVERSATIONAL-RUNTIME-01.

Offline/mocked proof of the native conversational path: natural-language
intent -> tools/image_generation.py's two tools -> the already-landed
core.image_generation_service.generate_image() -> a truthful outcome
report. Nothing here re-proves the downstream service's own internals
(tests/test_multimodal_m4_image_generation_service_01.py already does
that exhaustively) -- this file is scoped to the NEW seam this campaign
adds: route/credential/estimate resolution at the tool boundary, the
stage/confirm split (core.image_generation_draft), draft-drift fail-
closed behavior, and outcome-string fidelity back to the model. Fully
offline: no real network, no real credentials, no real Higgsfield
adapter -- svc.resolve_image_generation_target and HiggsfieldAdapter are
both monkeypatched per test. config.DB_PATH is isolated per test the same
way tests/test_multimodal_m4_image_generation_service_01.py already does;
config.DATA_DIR is isolated for the whole session by tests/conftest.py.

Cases 1-3 (natural-language intent recognition, non-generation requests
not spuriously triggering it, ambiguous requests never silently spending)
are properties of the skill doc + tool descriptions steering the model's
own judgment, not something this Python suite can execute -- they're
covered in the source-vet report, not here. This suite starts from "the
tool was called" and proves everything downstream of that is safe no
matter how the model got there.
"""
from __future__ import annotations

import hashlib
import inspect
import os

import pytest

import config
import core.generation_manifest as gm
import core.image_generation_draft as draft_store
import tools.image_generation as tool
from core.capability_router import (
    Capability,
    CapabilityRegistry,
    CapabilityRoute,
    EvidenceClass,
    LANE_SPECIALIST,
    RoutingPolicy,
    SpecialistRecord,
)
from core.higgsfield_adapter import HiggsfieldAdapterError
from core.image_generation_service import ImageGenerationTarget

SPECIALIST = "higgsfield"
MODEL = "higgsfield-ai/soul/standard"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))


@pytest.fixture(autouse=True)
def _clean_draft_store():
    draft_store._drafts.clear()
    yield
    draft_store._drafts.clear()


def _real_target() -> ImageGenerationTarget:
    registry = CapabilityRegistry([
        SpecialistRecord(
            name=SPECIALIST, kind="external", local=False,
            capabilities={Capability.IMAGE_GENERATION: EvidenceClass.EVIDENCED},
        ),
    ])
    policy = RoutingPolicy(
        routes={
            Capability.IMAGE_GENERATION.value: CapabilityRoute(
                mode=LANE_SPECIALIST, specialist=SPECIALIST,
            ),
        },
    )
    return ImageGenerationTarget(registry=registry, policy=policy, specialist=SPECIALIST, model=MODEL)


def _route_it(monkeypatch):
    monkeypatch.setattr(tool.svc, "resolve_image_generation_target", lambda *a, **k: _real_target())


def _not_routed(monkeypatch):
    monkeypatch.setattr(tool.svc, "resolve_image_generation_target", lambda *a, **k: None)


class FakeAdapter:
    """Minimal offline GenerationAdapter -- estimate/submit/poll/fetch all
    in caller control, mirrors tests/test_multimodal_m4_image_generation_
    service_01.py's own FakeAdapter but scoped to what this suite needs."""

    def __init__(self, estimate=0.0938, submit_error=None, statuses=("succeeded",),
                 outputs=((b"fake-bytes", "image/png"),)):
        self._estimate = estimate
        self._submit_error = submit_error
        self._statuses = list(statuses)
        self._outputs = list(outputs)
        self.submit_calls = []

    def estimate_cost(self, *, model, settings):
        return self._estimate

    def submit(self, *, model, settings, reference_assets, provider_idempotency_key=None):
        if self._submit_error is not None:
            raise self._submit_error
        self.submit_calls.append({"model": model, "settings": dict(settings)})
        return "job-1", "queued"

    def poll(self, provider_job_id):
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    def fetch_result(self, provider_job_id):
        return self._outputs[0]

    def cancel(self, job):
        return job


def _adapter_ok(monkeypatch, **kwargs) -> FakeAdapter:
    fake = FakeAdapter(**kwargs)
    monkeypatch.setattr(tool, "HiggsfieldAdapter", lambda: fake)
    return fake


def _adapter_no_credentials(monkeypatch):
    def _raise():
        raise HiggsfieldAdapterError("Higgsfield credentials not configured")
    monkeypatch.setattr(tool, "HiggsfieldAdapter", _raise)


def _extract(report: str, field: str) -> str:
    for line in report.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{field!r} not found in report:\n{report}")


def _stage_via_tool(monkeypatch) -> str:
    preview = tool.estimate_image_generation("a purple neon cassette deck")
    return _extract(preview, "draft_id")


# ---------------------------------------------------------------------------
# 4. configured generation route disabled / unconfigured
# ---------------------------------------------------------------------------

def test_estimate_reports_not_routed_when_route_unconfigured(monkeypatch):
    _not_routed(monkeypatch)

    result = tool.estimate_image_generation("a cassette deck")

    assert "outcome: not_routed" in result
    assert "draft_id" not in result


def test_generate_image_never_reachable_at_all_without_a_prior_estimate():
    result = tool.generate_image("hallucinated-draft-id")
    assert "outcome: draft_not_found" in result


# ---------------------------------------------------------------------------
# 5. required credentials missing
# ---------------------------------------------------------------------------

def test_estimate_reports_credentials_unavailable(monkeypatch):
    _route_it(monkeypatch)
    _adapter_no_credentials(monkeypatch)

    result = tool.estimate_image_generation("a cassette deck")

    assert "outcome: credentials_unavailable" in result
    assert "draft_id" not in result


def test_generate_image_reports_credentials_unavailable_if_lost_since_staging(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch)
    draft_id = _stage_via_tool(monkeypatch)

    _adapter_no_credentials(monkeypatch)
    result = tool.generate_image(draft_id)

    assert "outcome: credentials_unavailable" in result
    assert draft_store.peek_draft(draft_id) is None  # single-use even on this failure


# ---------------------------------------------------------------------------
# estimate unavailable (own outcome, task's required taxonomy)
# ---------------------------------------------------------------------------

def test_estimate_unavailable_reported_and_nothing_staged(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch, estimate=None)

    result = tool.estimate_image_generation("a cassette deck")

    assert "outcome: estimate_unavailable" in result
    assert "draft_id" not in result


# ---------------------------------------------------------------------------
# 6/7 -- spend authorization denied / approved
# ---------------------------------------------------------------------------

def test_full_happy_path_estimate_then_confirm(monkeypatch):
    _route_it(monkeypatch)
    fake = _adapter_ok(monkeypatch)

    preview = tool.estimate_image_generation("a purple neon cassette deck")
    assert "outcome: estimate_ready" in preview
    assert "estimated_cost: 0.0938 usd" in preview
    draft_id = _extract(preview, "draft_id")

    result = tool.generate_image(draft_id)

    assert "outcome: success" in result
    assert len(fake.submit_calls) == 1
    assert "![Generated image](file://" in result


# ---------------------------------------------------------------------------
# 8/9. mocked provider success / failure
# ---------------------------------------------------------------------------

def test_provider_failure_reported_truthfully(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch, statuses=("failed",))
    draft_id = _stage_via_tool(monkeypatch)

    result = tool.generate_image(draft_id)

    assert "outcome: provider_failed" in result


# ---------------------------------------------------------------------------
# 10. durable artifact-ingestion failure
# ---------------------------------------------------------------------------

def test_ingestion_failure_reported_truthfully(monkeypatch):
    _route_it(monkeypatch)

    class RaisingFetchAdapter(FakeAdapter):
        def fetch_result(self, provider_job_id):
            raise RuntimeError("network blew up mid-fetch")

    fake = RaisingFetchAdapter()
    monkeypatch.setattr(tool, "HiggsfieldAdapter", lambda: fake)
    draft_id = _stage_via_tool(monkeypatch)

    result = tool.generate_image(draft_id)

    assert "outcome: ingestion_failed" in result


# ---------------------------------------------------------------------------
# 11. generated-artifact-manifest failure -- bytes durable, manifest is not
# ---------------------------------------------------------------------------

def test_manifest_persistence_failure_reported_without_losing_the_artifact(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch)
    draft_id = _stage_via_tool(monkeypatch)

    def _boom(manifest):
        raise gm.ManifestPersistenceError("disk full")

    monkeypatch.setattr(gm, "persist_manifest", _boom)

    result = tool.generate_image(draft_id)

    assert "outcome: manifest_failed" in result
    assert "artifacts_delivered: 1" in result
    assert "![Generated image](file://" in result  # bytes are still durable


# ---------------------------------------------------------------------------
# 12. multiple provider outputs
# ---------------------------------------------------------------------------

def test_multiple_outputs_all_represented(monkeypatch):
    _route_it(monkeypatch)

    class MultiOutputAdapter(FakeAdapter):
        def list_outputs(self, provider_job_id):
            return [0, 1]

        def fetch_output(self, provider_job_id, index):
            return (f"bytes-{index}".encode(), "image/png")

    fake = MultiOutputAdapter()
    monkeypatch.setattr(tool, "HiggsfieldAdapter", lambda: fake)
    draft_id = _stage_via_tool(monkeypatch)

    result = tool.generate_image(draft_id)

    assert "outcome: success" in result
    assert "artifacts_delivered: 2" in result
    assert result.count("![Generated image](file://") == 2


# ---------------------------------------------------------------------------
# 13/16. exact failure/outcome propagation; no authority promotion
# ---------------------------------------------------------------------------

def test_outcome_string_is_the_services_own_unparaphrased_vocabulary(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch, statuses=("cancelled",))
    draft_id = _stage_via_tool(monkeypatch)

    result = tool.generate_image(draft_id)

    assert "outcome: cancelled" in result
    assert isinstance(result, str)  # a plain report, never anything executed


# ---------------------------------------------------------------------------
# 14. no blind paid retry
# ---------------------------------------------------------------------------

def test_second_confirm_of_the_same_draft_never_resubmits(monkeypatch):
    _route_it(monkeypatch)
    fake = _adapter_ok(monkeypatch)
    draft_id = _stage_via_tool(monkeypatch)

    first = tool.generate_image(draft_id)
    second = tool.generate_image(draft_id)

    assert "outcome: success" in first
    assert "outcome: draft_not_found" in second
    assert len(fake.submit_calls) == 1


def test_draft_drift_on_price_change_refuses_and_never_submits(monkeypatch):
    """Sol's directive: confirmation must fail closed if the staged
    operation materially changed since staging -- it must never silently
    honor the OLD estimate the owner actually approved."""
    _route_it(monkeypatch)
    fake = _adapter_ok(monkeypatch, estimate=0.0938)
    draft_id = _stage_via_tool(monkeypatch)

    fake._estimate = 0.50  # price moved between estimate and confirm

    result = tool.generate_image(draft_id)

    assert "outcome: draft_stale" in result
    assert len(fake.submit_calls) == 0
    assert draft_store.peek_draft(draft_id) is None  # single-use even on drift


def test_route_change_between_estimate_and_confirm_refuses(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch)
    draft_id = _stage_via_tool(monkeypatch)

    _not_routed(monkeypatch)
    result = tool.generate_image(draft_id)

    assert "outcome: draft_stale" in result


# ---------------------------------------------------------------------------
# 15. correct originating-chat delivery (render half in
# tests/test_chat_render_local_image_markdown.py)
# ---------------------------------------------------------------------------

def test_artifact_path_embedded_in_result_is_a_real_local_file(monkeypatch):
    _route_it(monkeypatch)
    _adapter_ok(monkeypatch)
    draft_id = _stage_via_tool(monkeypatch)

    result = tool.generate_image(draft_id)

    line = next(l for l in result.splitlines() if l.startswith("![Generated image]"))
    path = line.split("file://", 1)[1].rstrip(")")
    assert os.path.isfile(path)


# ---------------------------------------------------------------------------
# 17. frozen OFFICIAL skill identity remains unchanged
# ---------------------------------------------------------------------------

def test_official_media_generation_skill_hash_unchanged():
    path = "skill_packages/official/media-generation/media-generation.md"
    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    assert actual == "4eac5c81840715c3bf2a631734733afdf6105557195ac53c20320c7ae12cb9b9"


def test_official_generated_artifact_manifest_skill_hash_unchanged():
    path = "skill_packages/official/generated-artifact-manifest/generated-artifact-manifest.md"
    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    assert actual == "cf35a207d5c75c66f8672d26b37724c42381ea7d76efef13e618e235d4e7cbd2"


# ---------------------------------------------------------------------------
# Tool registration / owner gating
# ---------------------------------------------------------------------------

def test_registration_adds_both_tools_to_the_registry():
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    tool.register_image_generation_tools(registry)

    assert "estimate_image_generation" in registry.list_tools()
    assert "generate_image" in registry.list_tools()


def test_registration_call_sits_at_the_same_indentation_as_the_toolmaker_precedent():
    """core/agent.py must register this module only inside `if owner:` --
    the same hard exclusion as register_toolmaker_tools (for a non-owner
    session, cost-bearing tools must be absent from the registry, not
    merely disabled). Verified structurally: the two calls must be
    indented identically, both deeper than a preceding `if owner:` line,
    with no intervening dedent back out of that block."""
    import core.agent as agent_module

    source = inspect.getsource(agent_module)
    lines = source.splitlines()

    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    toolmaker_idx = next(
        i for i, l in enumerate(lines)
        if "register_toolmaker_tools(self.registry, self)" in l
    )
    new_tool_idx = next(
        i for i, l in enumerate(lines)
        if "register_image_generation_tools(self.registry)" in l
    )
    assert new_tool_idx > toolmaker_idx

    block_indent = _indent(lines[toolmaker_idx])
    assert _indent(lines[new_tool_idx]) == block_indent

    # No line between the two dedents back to (or past) `if owner:`'s own
    # indentation -- i.e. both calls are inside the same unbroken block.
    if_owner_idx = next(
        i for i in range(toolmaker_idx, -1, -1)
        if lines[i].strip() == "if owner:"
    )
    if_indent = _indent(lines[if_owner_idx])
    for i in range(if_owner_idx + 1, new_tool_idx):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert _indent(lines[i]) > if_indent, (
            f"line {i} dedents out of the `if owner:` block before "
            "register_image_generation_tools is reached"
        )
