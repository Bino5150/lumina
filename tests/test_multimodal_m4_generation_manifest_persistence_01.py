"""
tests/test_multimodal_m4_generation_manifest_persistence_01.py --
MULTIMODAL-M4-GENERATION-MANIFEST-PERSISTENCE-01.

Proves core.generation_manifest.persist_manifest() (and its companion
core.generation_artifact.artifact_manifest_path()) durably materialize an
already-validated manifest to disk, without weakening any of the ten
frozen manifest laws proven by tests/test_generation_manifest_law_01.py.

Fully offline: no network, no Qt. config.DATA_DIR is session-isolated to a
throwaway tmp dir by tests/conftest.py already; these tests additionally
scope config.DATA_DIR to a fresh tmp_path per test so path assertions are
exact and tests never share on-disk state.
"""
from __future__ import annotations

import json
import os

import pytest

import config
import core.generation_manifest as gm
from core.generation_artifact import GeneratedArtifact, artifact_manifest_path
from core.generation_job import GenerationJob
from core.generation_manifest import (
    ManifestPersistenceError,
    ManifestValidationError,
    persist_manifest,
)

FAKE_PROVIDERS = frozenset({"fake-flickr"})


@pytest.fixture(autouse=True)
def _fake_provider_vocabulary(request, monkeypatch):
    if "real_provider_vocabulary" in request.fixturenames:
        return
    monkeypatch.setattr(gm, "CANONICAL_PROVIDERS", FAKE_PROVIDERS)


@pytest.fixture
def real_provider_vocabulary():
    """Opt-out: test needs the real production provider vocabulary."""


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    return tmp_path


def _manifest(**over) -> dict:
    base = {
        "capability": "image_generation",
        "provider": "fake-flickr",
        "model": "fake-model-1",
        "lumina_job_id": "job-1",
        "provider_job_id": "prov-req-1",
        "artifact_id": "art-persist-1",
        "artifact_path": "/data/artifacts/ar/art-persist-1",
        "provider_output_index": 0,
        "job_ingestion_state": "ingested",
        "review_state": "completed",
        "cost_estimate": 0.25,
        "cost_actual": 0.25,
        "cost_unit": "usd",
        "authorization_ref": "owner-envelope-77",
        "created_utc": "2026-09-17T10:02:05+00:00",
        "params": {"size": "512x512"},
        "failure_class": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# PERSISTENCE
# ---------------------------------------------------------------------------

def test_persisted_manifest_lands_under_data_dir(isolated_data_dir):
    manifest = _manifest()
    path = persist_manifest(manifest)
    assert os.path.isfile(path)
    assert path.startswith(str(isolated_data_dir))


def test_persisted_json_round_trips_exactly_to_validated_manifest(isolated_data_dir):
    manifest = _manifest()
    validated = gm.validate_manifest(manifest)
    path = persist_manifest(manifest)
    with open(path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == validated


def test_canonical_path_is_deterministic_from_artifact_id_alone(isolated_data_dir):
    manifest = _manifest(artifact_id="art-deterministic")
    written_path = persist_manifest(manifest)
    recomputed_path = artifact_manifest_path("art-deterministic")
    assert written_path == recomputed_path


def test_manifest_path_is_sibling_of_artifact_bytes_path(isolated_data_dir):
    from core.generation_artifact import _artifact_path  # test-internal peek, same module

    manifest = _manifest(artifact_id="art-sibling")
    manifest_file = persist_manifest(manifest)
    artifact_file = _artifact_path("art-sibling")
    assert manifest_file == artifact_file + ".manifest.json"
    assert os.path.dirname(manifest_file) == os.path.dirname(artifact_file)


def test_parent_directories_are_created_safely(isolated_data_dir):
    manifest = _manifest(artifact_id="brand-new-shard-id")
    path = persist_manifest(manifest)
    assert os.path.isdir(os.path.dirname(path))
    assert os.path.isfile(path)


def test_write_is_atomic_no_tmp_file_left_behind(isolated_data_dir):
    manifest = _manifest(artifact_id="art-atomic")
    path = persist_manifest(manifest)
    assert not os.path.exists(path + ".tmp")


def test_existing_manifest_not_corrupted_on_simulated_write_failure(isolated_data_dir, monkeypatch):
    manifest = _manifest(artifact_id="art-crash-safety")
    good_path = persist_manifest(manifest)
    with open(good_path, "r", encoding="utf-8") as f:
        original_bytes = f.read()

    def _boom(*a, **kw):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(ManifestPersistenceError):
        persist_manifest(_manifest(artifact_id="art-crash-safety", review_state="reviewed"))

    with open(good_path, "r", encoding="utf-8") as f:
        assert f.read() == original_bytes

    # No leaked temp file from the failed attempt.
    assert not os.path.exists(good_path + ".tmp")


def test_write_failure_raises_manifest_persistence_error_not_bare_oserror(isolated_data_dir, monkeypatch):
    def _boom(*a, **kw):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "makedirs", _boom)
    with pytest.raises(ManifestPersistenceError):
        persist_manifest(_manifest(artifact_id="art-permission-denied"))


# ---------------------------------------------------------------------------
# VALIDATION (persist_manifest must never bypass validate_manifest)
# ---------------------------------------------------------------------------

def test_invalid_manifest_cannot_be_persisted_missing_field(isolated_data_dir):
    manifest = _manifest()
    del manifest["cost_unit"]
    with pytest.raises(ManifestValidationError):
        persist_manifest(manifest)
    # Nothing was written for this artifact_id.
    assert not os.path.exists(artifact_manifest_path(manifest["artifact_id"]))


def test_unknown_provider_still_fails_closed(isolated_data_dir):
    manifest = _manifest(provider="not-a-real-provider")
    with pytest.raises(gm.UnknownManifestProvider):
        persist_manifest(manifest)
    assert not os.path.exists(artifact_manifest_path(manifest["artifact_id"]))


def test_unknown_capability_still_fails_closed(isolated_data_dir):
    manifest = _manifest(capability="not_a_real_capability")
    with pytest.raises(gm.UnknownManifestCapability):
        persist_manifest(manifest)
    assert not os.path.exists(artifact_manifest_path(manifest["artifact_id"]))


def test_missing_authorization_ref_still_fails_for_cost_bearing(isolated_data_dir):
    manifest = _manifest(authorization_ref=None)
    with pytest.raises(gm.MissingAuthorizationRef):
        persist_manifest(manifest)
    assert not os.path.exists(artifact_manifest_path(manifest["artifact_id"]))


def test_secret_bearing_manifest_refused_before_durable_write(isolated_data_dir):
    manifest = _manifest(params={"api_key": "sk-should-never-be-here"})
    with pytest.raises(gm.SecretMaterialInManifest):
        persist_manifest(manifest)
    assert not os.path.exists(artifact_manifest_path(manifest["artifact_id"]))


def test_real_production_provider_vocabulary_persists_higgsfield(
    isolated_data_dir, real_provider_vocabulary,
):
    manifest = _manifest(provider="higgsfield", artifact_id="art-real-higgsfield")
    path = persist_manifest(manifest)
    assert os.path.isfile(path)
    with open(path) as f:
        assert json.load(f)["provider"] == "higgsfield"


# ---------------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------------

def test_no_credentials_appear_in_persisted_manifest(isolated_data_dir):
    manifest = _manifest(params={"note": "ordinary caption text"})
    path = persist_manifest(manifest)
    with open(path) as f:
        text = f.read()
    for marker in ("higgsfield_key_id", "higgsfield_key_secret", "Authorization", "Key "):
        assert marker not in text


def test_no_signed_provider_url_appears_in_persisted_manifest(isolated_data_dir):
    manifest = _manifest()
    path = persist_manifest(manifest)
    with open(path) as f:
        text = f.read()
    assert "http://" not in text
    assert "https://" not in text


def test_filesystem_error_message_is_redacted(isolated_data_dir, monkeypatch):
    # A known secret-value SHAPE (core.redaction.SECRET_VALUE_RE) embedded in
    # a simulated OS error message -- proves _atomic_write_manifest_text()
    # actually runs the error text through redact_secret_shapes() before
    # raising, the same defense-in-depth every other diagnostic path in
    # this codebase already applies.
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"

    def _boom(*a, **kw):
        raise OSError(f"could not write: leaked token {secret}")

    monkeypatch.setattr(os, "makedirs", _boom)
    with pytest.raises(ManifestPersistenceError) as exc_info:
        persist_manifest(_manifest(artifact_id="art-error-redaction"))
    message = str(exc_info.value)
    assert secret not in message
    assert "[REDACTED]" in message
