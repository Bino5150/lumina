"""B1.1 exact data contracts; these tests do not mint executable authority."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from chrome_companion.site_actions import model as m
from chrome_companion.site_actions.registry import CAPABILITY_IDS, load_manifest


@pytest.fixture(params=CAPABILITY_IDS)
def manifest(request):
    return load_manifest(request.param)


def _scope(manifest, body="Exact owner text."):
    if manifest.capability_id == "reddit.reply":
        destination = m.DestinationIdentity("reddit", "comment", "t1_abc123")
        payload = {"body": body}
    else:
        destination = m.DestinationIdentity("reddit", "subreddit", "t5_def456")
        payload = {"mode": "text", "title": "Exact title", "body": body}
    return m.ActionSpec(manifest, m.Runtime.CHROME_COMPANION, destination,
                        m.FrozenPayload.from_data(payload), m.Consequence.EXTERNAL_COMMIT)


def test_both_closed_manifests_share_one_inert_contract(manifest):
    assert type(manifest) is m.CapabilityManifest
    scope = _scope(manifest)
    assert type(scope) is m.ActionSpec
    assert manifest.schema_version == m.SCHEMA_VERSION == 1
    assert manifest.runtimes == (m.Runtime.CHROME_COMPANION,)
    assert manifest.origins == ("https://www.reddit.com",)
    assert manifest.principal == m.PrincipalPolicy(m.PrincipalRequirement.REQUIRED,
                                                  m.UnavailablePolicy.DENY,
                                                  m.PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT)
    assert tuple(step.kind for step in manifest.steps) == (
        m.StepKind.PREPARE, m.StepKind.FILL, m.StepKind.COMMIT)
    assert manifest.steps[-1].max_count == 1
    assert set(manifest.evidence) == set(m.EvidenceKind)
    assert m.CapabilityManifest.from_data(m.decode_json(manifest.canonical_bytes)) == manifest
    assert scope.digest == hashlib.sha256(scope.canonical_bytes).hexdigest()


def test_site_neutral_schema_accepts_offline_generalization_without_registration():
    data = load_manifest("reddit.reply").to_data()
    data.update(capability_id="github.create_issue", adapter_id="github", namespace="github",
                origins=["https://github.com"],
                destinations=[{"kind": "repository", "codec": "opaque_ascii", "prefix": ""}])
    fixture = m.CapabilityManifest.from_data(data)
    scope = m.ActionSpec(fixture, m.Runtime.CHROME_COMPANION,
                         m.DestinationIdentity("github", "repository", "R_opaque123"),
                         m.FrozenPayload.from_data({"body": "Offline specimen"}),
                         m.Consequence.EXTERNAL_COMMIT)
    assert type(scope) is m.ActionSpec
    with pytest.raises(m.ContractError):
        load_manifest("github.create_issue")


def test_mutating_inputs_and_returned_maps_cannot_change_snapshot(manifest):
    data = manifest.to_data()
    frozen = m.CapabilityManifest.from_data(data)
    fingerprint = frozen.fingerprint
    data["principal"]["unavailable"] = "allow_declared_consequence"
    data["payload_fields"][0]["required"] = False
    data["destinations"].clear()
    exported = frozen.to_data()
    exported["runtimes"].append("playwright")
    assert frozen == manifest and frozen.fingerprint == fingerprint
    payload_data = _scope(manifest).payload.to_data()
    payload = m.FrozenPayload.from_data(payload_data)
    original = payload.canonical_bytes
    payload_data["body"] = "Changed"
    payload.to_data()["body"] = "Changed again"
    assert payload.canonical_bytes == original
    with pytest.raises(FrozenInstanceError):
        frozen.consequence = m.Consequence.DESTRUCTIVE
    with pytest.raises(FrozenInstanceError):
        frozen.principal.unavailable = m.UnavailablePolicy.ALLOW
    with pytest.raises(TypeError):
        payload.fields[0] = ("body", "Changed")
    assert not hasattr(frozen, "__dict__")


def test_canonical_encoding_preserves_exact_whitespace_unicode_and_newlines(manifest):
    body = " \tCafe\u0301 / Café\r\n🙂\n<script>submit()</script>  "
    scope = _scope(manifest, body)
    assert m.decode_json(scope.canonical_bytes)["payload"]["body"] == body
    assert scope.canonical_bytes == m.canonical_json(dict(reversed(list(scope.to_data().items()))))
    assert _scope(manifest, body + " ").digest != scope.digest
    assert _scope(manifest, body.replace("\r\n", "\n")).digest != scope.digest
    assert _scope(manifest, body.replace("Cafe\u0301", "Café")).digest != scope.digest


@pytest.mark.parametrize("raw", [
    b"\xff{}", b'\xef\xbb\xbf{}', b'{"x":1,"x":2}', b'{"a":{"b":1,"b":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1.0}',
    b'{"x":1e3}', b'{"x":9223372036854775808}', b'{"x":-9223372036854775809}',
    b'{"x":' + b'9' * 5000 + b'}', br'{"x":"\uD800"}', br'{"\uDC00":1}',
    b'{} trailing', b'{}{}', br'"\uDFFF"', b'[' * 1000 + b']' * 1000,
    b'[' * 13 + b'0' + b']' * 13, b'[' + b'0,' * 1024 + b'0]',
    b'"' + b'x' * m.MAX_JSON_BYTES + b'"', "\ud800", bytearray(b"{}"),
])
def test_strict_decoder_rejects_hostile_or_nonexact_data(raw):
    with pytest.raises(m.ContractError):
        m.decode_json(raw)


@pytest.mark.parametrize("value", [float("nan"), 1.0, {"x": b"data"}, {1: "x"},
                                       {"x": ("a",)}, {"x": object()}, {"x": "\ud800"}])
def test_encoder_rejects_non_json_values(value):
    with pytest.raises(m.ContractError):
        m.canonical_json(value)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("version", 0), ("version", 1.0),
    ("capability_id", "reply"), ("adapter_id", "__import__"), ("namespace", "x/y"),
    ("runtimes", ["playwright"]), ("runtimes", ["api"]),
    ("runtimes", ["chrome_companion", "chrome_companion"]),
    ("origins", ["https://www.reddit.com/"]), ("origins", ["https://*.reddit.com"]),
    ("origins", ["https://www.reddit.com@evil.test"]), ("origins", ["http://www.reddit.com"]),
    ("origins", ["https://WWW.reddit.com"]), ("origins", ["https://www.reddit.com:443"]),
    ("path_policy", "arbitrary_url"), ("consequence", "fill"), ("consequence", "destructive"),
    ("remote_evidence_rule", "toast_is_success"), ("evidence", ["new_object"]),
    ("evidence", [item.value for item in m.EvidenceKind] + ["toast"]),
])
def test_manifest_top_level_contract_is_closed(manifest, field, value):
    data = manifest.to_data()
    data[field] = value
    with pytest.raises(m.ContractError):
        m.CapabilityManifest.from_data(data)


@pytest.mark.parametrize("location", [None, "principal", "destinations", "payload_fields", "steps"])
@pytest.mark.parametrize("key", ["callback", "script", "selector", "endpoint", "authority_token", "imports"])
def test_executable_authority_and_unknown_fields_rejected_at_every_schema_level(manifest, location, key):
    data = manifest.to_data()
    target = data if location is None else data[location]
    if type(target) is list:
        target = target[0]
    target[key] = "submit()"
    with pytest.raises(m.ContractError):
        m.CapabilityManifest.from_data(data)


@pytest.mark.parametrize("mutate", [
    lambda d: d["principal"].update(unavailable="allow_declared_consequence"),
    lambda d: d["principal"].pop("unavailable"),
    lambda d: d["principal"].update(evidence_rule="unavailable_v1"),
    lambda d: d["destinations"][0].update(codec="regex"),
    lambda d: d["destinations"][0].update(prefix=".*"),
    lambda d: d["destinations"].append(deepcopy(d["destinations"][0])),
    lambda d: d["payload_fields"][0].update(required=1),
    lambda d: d["payload_fields"][0].update(max_bytes=True),
    lambda d: d["payload_fields"][0].update(min_chars=-1),
    lambda d: d["payload_fields"][0].update(literal="default inserted text"),
    lambda d: d["payload_fields"].append(deepcopy(d["payload_fields"][0])),
    lambda d: d["steps"][-1].update(max_count=2),
    lambda d: d["steps"][1].update(kind="submit"),
    lambda d: d["steps"].reverse(),
    lambda d: d["steps"].pop(),
])
def test_nested_schema_contradictions_fail_closed(manifest, mutate):
    data = manifest.to_data()
    mutate(data)
    with pytest.raises(m.ContractError):
        m.CapabilityManifest.from_data(data)


@pytest.mark.parametrize("requirement,unavailable,rule", [
    (m.PrincipalRequirement.OPTIONAL, m.UnavailablePolicy.DENY, m.PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT),
    (m.PrincipalRequirement.OPTIONAL, m.UnavailablePolicy.ALLOW, m.PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT),
    (m.PrincipalRequirement.UNAVAILABLE, m.UnavailablePolicy.DENY, m.PrincipalEvidenceRule.UNAVAILABLE),
    (m.PrincipalRequirement.UNAVAILABLE, m.UnavailablePolicy.ALLOW, m.PrincipalEvidenceRule.UNAVAILABLE),
])
def test_fixture_principal_policies_are_explicit_data_only(requirement, unavailable, rule):
    policy = m.PrincipalPolicy(requirement, unavailable, rule)
    assert m.PrincipalPolicy.from_data(policy.to_data()) == policy
    assert not hasattr(policy, "authorize")


@pytest.mark.parametrize("kind,key", [("post", "t3_abc123"), ("comment", "t1_abc123")])
def test_reply_declares_exact_post_or_comment_fullnames(kind, key):
    load_manifest("reddit.reply").validate_destination(m.DestinationIdentity("reddit", kind, key))


@pytest.mark.parametrize("namespace,kind,key", [
    ("other", "comment", "t1_abc"), ("reddit", "subreddit", "t5_abc"),
    ("reddit", "comment", "t3_abc"), ("reddit", "post", "t1_abc"),
    ("reddit", "comment", "t1_"), ("reddit", "comment", "t1_ABC"),
    ("reddit", "comment", "t1_a/b"), ("reddit", "comment", "https://www.reddit.com/x"),
])
def test_display_urls_names_and_wrong_identity_kinds_cannot_be_destinations(namespace, kind, key):
    with pytest.raises(m.ContractError):
        load_manifest("reddit.reply").validate_destination(m.DestinationIdentity(namespace, kind, key))


def test_field_byte_and_scalar_limits_at_exact_edges(manifest):
    _scope(manifest, "🙂" * 4096)  # exactly 16 KiB; Python length counts scalar values
    with pytest.raises(m.ContractError):
        _scope(manifest, "🙂" * 4096 + "x")
    with pytest.raises(m.ContractError):
        _scope(manifest, "x" * (m.MAX_TEXT_BYTES + 1))
    if manifest.capability_id == "reddit.create_post":
        scope = _scope(manifest, "")
        values = scope.payload.to_data()
        values["title"] = "🙂" * 300
        replace(scope, payload=m.FrozenPayload.from_data(values))
        values["title"] += "x"
        with pytest.raises(m.ContractError):
            replace(scope, payload=m.FrozenPayload.from_data(values))
    else:
        with pytest.raises(m.ContractError):
            _scope(manifest, "")


@pytest.mark.parametrize("body", [True, 1, None, {"callback": "submit"}, ["text"]])
def test_payload_does_not_coerce_values(manifest, body):
    with pytest.raises(m.ContractError):
        _scope(manifest, body)


def test_no_discretionary_fields_defaults_or_consequence_widening(manifest):
    scope = _scope(manifest)
    for key in ("submit", "flair", "url", "principal", "runtime", "consequence", "owner"):
        values = scope.payload.to_data()
        values[key] = "invented"
        with pytest.raises(m.ContractError):
            replace(scope, payload=m.FrozenPayload.from_data(values))
    values = scope.payload.to_data()
    values.pop("body")
    with pytest.raises(m.ContractError):
        replace(scope, payload=m.FrozenPayload.from_data(values))
    for consequence in (m.Consequence.FILL, m.Consequence.DESTRUCTIVE):
        with pytest.raises(m.ContractError):
            replace(scope, consequence=consequence)
    for runtime in ("chrome_companion", "playwright", "api"):
        with pytest.raises(m.ContractError):
            replace(scope, runtime=runtime)
    if manifest.capability_id == "reddit.create_post":
        values = scope.payload.to_data()
        values["mode"] = "link"
        with pytest.raises(m.ContractError):
            replace(scope, payload=m.FrozenPayload.from_data(values))


def test_spec_fingerprint_covers_every_exact_scope_component(manifest):
    original = _scope(manifest)
    changed_destination = replace(original.destination, canonical_key=original.destination.canonical_key + "a")
    changed_schema = replace(manifest, version=2)
    changed_payload = _scope(manifest, "Different payload")
    assert len({original.digest, replace(original, destination=changed_destination).digest,
                replace(original, manifest=changed_schema).digest, changed_payload.digest}) == 4


@pytest.mark.parametrize("value", [
    [["body", "text"]], (("body", {}),), (("body", "x"), ("body", "y")),
    (("z", "x"), ("body", "y")), {"body": "x"},
])
def test_direct_construction_cannot_smuggle_mutable_payload(value):
    with pytest.raises(m.ContractError):
        m.FrozenPayload(value)


def test_direct_constructors_enforce_frozen_nested_types(manifest):
    with pytest.raises(m.ContractError):
        replace(manifest, destinations=list(manifest.destinations))
    with pytest.raises(m.ContractError):
        replace(manifest, principal=manifest.principal.to_data())
    with pytest.raises(m.ContractError):
        replace(manifest, runtimes=("chrome_companion",))
    with pytest.raises(m.ContractError):
        m.StepDeclaration(m.StepKind.COMMIT, True)


@pytest.mark.parametrize("capability", ["../reddit.reply", "reddit.reply.json", "github.create_issue",
                                         "reddit.reply/../../x", None, True, ["reddit.reply"]])
def test_registry_has_no_caller_path_or_registration_surface(capability):
    with pytest.raises(m.ContractError):
        load_manifest(capability)


def test_literal_json_golden_encoding_is_stable():
    value = {"z": "🙂\r\n  ", "a": {"b": 1, "a": "e\u0301"}}
    expected = '{"a":{"a":"e\u0301","b":1},"z":"🙂\\r\\n  "}'.encode("utf-8")
    assert m.canonical_json(value) == expected
    assert json.loads(expected) == value
