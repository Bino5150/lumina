"""Inert immutable schemas for the shared site-action boundary (BC-01C-B1.1).

No owner admission, claims, storage, evidence acceptance or browser mechanics
live here. Validating data does not authenticate its source or grant authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re


SCHEMA_VERSION = 1
MAX_JSON_BYTES = 24 * 1024
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 1024
MAX_TEXT_BYTES = 16 * 1024


class ContractError(ValueError):
    """Data is outside the closed, exact site-action contract."""


class Runtime(str, Enum):
    CHROME_COMPANION = "chrome_companion"


class PrincipalRequirement(str, Enum):
    REQUIRED = "required_machine_verifiable"
    OPTIONAL = "optional_recorded_when_available"
    UNAVAILABLE = "unavailable"


class UnavailablePolicy(str, Enum):
    DENY = "deny"
    ALLOW = "allow_declared_consequence"


class PrincipalEvidenceRule(str, Enum):
    AUTHENTICATED_ACCOUNT = "authenticated_account_v1"
    UNAVAILABLE = "unavailable_v1"


class Consequence(str, Enum):
    OBSERVE = "observe"
    PREPARE = "prepare"
    FILL = "fill"
    EXTERNAL_COMMIT = "external_commit"
    DESTRUCTIVE = "destructive"


class StepKind(str, Enum):
    PREPARE = "prepare"
    FILL = "fill"
    COMMIT = "external_commit"


class ReceiptState(str, Enum):
    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED = "dispatched"
    BROWSER_LOCAL_EFFECT_OBSERVED = "browser_local_effect_observed"
    AMBIGUOUS_AFTER_DISPATCH = "ambiguous_after_dispatch"
    REMOTE_CONFIRMED = "remote_confirmed"
    DEFINITE_NONCOMMIT = "definite_noncommit"


class IdentityCodec(str, Enum):
    PREFIXED_BASE36 = "prefixed_base36"
    OPAQUE_ASCII = "opaque_ascii"


class FieldKind(str, Enum):
    TEXT = "text"
    LITERAL = "literal"


class PathPolicy(str, Enum):
    VERIFIED_TARGET = "same_origin_verified_target"


class RemoteEvidenceRule(str, Enum):
    CREATED_OBJECT = "created_object_v1"


class EvidenceKind(str, Enum):
    NEW_OBJECT = "new_object"
    OBJECT_IDENTITY = "created_object_identity"
    RESULT_URL = "canonical_result_url"
    DESTINATION_RELATION = "destination_relation"
    PRINCIPAL_MATCH = "principal_match"
    PAYLOAD_MATCH = "payload_match"


def _text(value: object, label: str, max_bytes: int = MAX_JSON_BYTES) -> str:
    if type(value) is not str or len(value) > max_bytes:
        raise ContractError(f"invalid {label}")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ContractError(f"invalid Unicode in {label}") from exc
    if size > max_bytes:
        raise ContractError(f"oversize {label}")
    return value


def _name(value: object, label: str) -> str:
    value = _text(value, label, 64)
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", value):
        raise ContractError(f"invalid {label}")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"invalid {label}")
    return value


def _typed(value: object, cls: type, label: str) -> None:
    if type(value) is not cls:
        raise ContractError(f"invalid {label}")


def _enum(cls: type[Enum], value: object) -> Enum:
    _text(value, cls.__name__, 64)
    try:
        return cls(value)
    except ValueError as exc:
        raise ContractError(f"unknown {cls.__name__}") from exc


def _object(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ContractError("missing or unknown object fields")
    return value


def _array(value: object) -> list:
    if type(value) is not list or not 1 <= len(value) <= 16:
        raise ContractError("invalid schema array")
    return value


def _tuple(value: object, cls: type, label: str) -> tuple:
    if type(value) is not tuple or not 1 <= len(value) <= 16:
        raise ContractError(f"invalid {label}")
    for item in value:
        _typed(item, cls, label)
    if len(set(value)) != len(value):
        raise ContractError(f"duplicate {label}")
    return value


def _check_json(value: object, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [MAX_JSON_ITEMS]
    budget[0] -= 1
    if depth > MAX_JSON_DEPTH or budget[0] < 0:
        raise ContractError("JSON structure exceeds bounds")
    if type(value) is dict:
        for key, item in value.items():
            _text(key, "JSON key", 128)
            _check_json(item, depth + 1, budget)
    elif type(value) is list:
        for item in value:
            _check_json(item, depth + 1, budget)
    elif type(value) is str:
        _text(value, "JSON string")
    elif type(value) is int:
        _integer(value, "JSON integer", -(2**63), 2**63 - 1)
    elif value is not None and type(value) is not bool:
        raise ContractError("non-JSON or floating-point value")


def canonical_json(value: object) -> bytes:
    """Stable UTF-8, sorted object keys; preserves every payload code point."""
    _check_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ContractError("oversize JSON")
    return encoded


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def _reject_number(value: str) -> None:
    raise ContractError("floating-point and nonfinite JSON values are forbidden")


def _parse_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        raise ContractError("oversize JSON integer")
    return _integer(int(value), "JSON integer", -(2**63), 2**63 - 1)


def decode_json(raw: bytes | str) -> object:
    """Strict bounded decoding; unlike observation framing, never scrubs text."""
    if type(raw) is bytes:
        if len(raw) > MAX_JSON_BYTES:
            raise ContractError("oversize JSON")
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContractError("invalid UTF-8") from exc
    else:
        _text(raw, "JSON input")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_int=_parse_int,
                           parse_float=_reject_number, parse_constant=_reject_number)
    except (ValueError, RecursionError) as exc:
        raise ContractError("invalid JSON") from exc
    # Encoding also bounds escaped expansion, nesting and aggregate size.
    canonical_json(value)
    return value


@dataclass(frozen=True, slots=True)
class PrincipalPolicy:
    requirement: PrincipalRequirement
    unavailable: UnavailablePolicy
    evidence_rule: PrincipalEvidenceRule

    def __post_init__(self) -> None:
        _typed(self.requirement, PrincipalRequirement, "principal requirement")
        _typed(self.unavailable, UnavailablePolicy, "unavailable policy")
        _typed(self.evidence_rule, PrincipalEvidenceRule, "principal evidence rule")
        if self.requirement is PrincipalRequirement.REQUIRED and self.unavailable is not UnavailablePolicy.DENY:
            raise ContractError("required principal cannot allow unavailable execution")
        unavailable = self.requirement is PrincipalRequirement.UNAVAILABLE
        if unavailable != (self.evidence_rule is PrincipalEvidenceRule.UNAVAILABLE):
            raise ContractError("principal evidence rule contradicts requirement")

    @classmethod
    def from_data(cls, value: object) -> PrincipalPolicy:
        data = _object(value, {"requirement", "unavailable", "evidence_rule"})
        return cls(_enum(PrincipalRequirement, data["requirement"]),
                   _enum(UnavailablePolicy, data["unavailable"]),
                   _enum(PrincipalEvidenceRule, data["evidence_rule"]))

    def to_data(self) -> dict:
        return {"requirement": self.requirement.value, "unavailable": self.unavailable.value,
                "evidence_rule": self.evidence_rule.value}


@dataclass(frozen=True, slots=True)
class DestinationIdentity:
    namespace: str
    kind: str
    canonical_key: str

    def __post_init__(self) -> None:
        _name(self.namespace, "namespace")
        _name(self.kind, "destination kind")
        key = _text(self.canonical_key, "destination identity", 128)
        if not re.fullmatch(r"[A-Za-z0-9_:-]+", key):
            raise ContractError("invalid opaque destination identity")

    def to_data(self) -> dict:
        return {"namespace": self.namespace, "kind": self.kind, "canonical_key": self.canonical_key}


@dataclass(frozen=True, slots=True)
class DestinationRule:
    kind: str
    codec: IdentityCodec
    prefix: str

    def __post_init__(self) -> None:
        _name(self.kind, "destination kind")
        _typed(self.codec, IdentityCodec, "identity codec")
        _text(self.prefix, "identity prefix", 16)
        if self.codec is IdentityCodec.PREFIXED_BASE36:
            if not re.fullmatch(r"[a-z][0-9]_", self.prefix):
                raise ContractError("invalid fullname prefix")
        elif self.prefix != "":
            raise ContractError("opaque identity codec cannot declare a prefix")

    def validate(self, key: str) -> None:
        _text(key, "destination identity", 128)
        if self.codec is IdentityCodec.PREFIXED_BASE36:
            if not key.startswith(self.prefix) or not re.fullmatch(r"[a-z0-9]{1,64}", key[len(self.prefix):]):
                raise ContractError("destination does not match declared identity codec")
        elif not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", key):
            raise ContractError("invalid opaque identity")

    @classmethod
    def from_data(cls, value: object) -> DestinationRule:
        data = _object(value, {"kind", "codec", "prefix"})
        return cls(data["kind"], _enum(IdentityCodec, data["codec"]), data["prefix"])

    def to_data(self) -> dict:
        return {"kind": self.kind, "codec": self.codec.value, "prefix": self.prefix}


@dataclass(frozen=True, slots=True)
class PayloadField:
    name: str
    kind: FieldKind
    required: bool
    min_chars: int
    max_chars: int
    max_bytes: int
    literal: str

    def __post_init__(self) -> None:
        _name(self.name, "payload field")
        _typed(self.kind, FieldKind, "payload field kind")
        _typed(self.required, bool, "required field")
        _integer(self.min_chars, "minimum characters", 0, MAX_TEXT_BYTES)
        _integer(self.max_chars, "maximum characters", self.min_chars, MAX_TEXT_BYTES)
        _integer(self.max_bytes, "maximum bytes", 1, MAX_TEXT_BYTES)
        _text(self.literal, "literal", 128)
        if self.kind is FieldKind.TEXT and self.literal != "":
            raise ContractError("text field cannot supply default content")
        if self.kind is FieldKind.LITERAL:
            if not self.literal or not self.required:
                raise ContractError("literal fields must be explicit and required")
            self.validate(self.literal)

    def validate(self, value: str) -> None:
        _text(value, "payload field value", self.max_bytes)
        if not self.min_chars <= len(value) <= self.max_chars:
            raise ContractError("payload character limit")
        if self.kind is FieldKind.LITERAL and value != self.literal:
            raise ContractError("payload literal mismatch")

    @classmethod
    def from_data(cls, value: object) -> PayloadField:
        data = _object(value, {"name", "kind", "required", "min_chars", "max_chars", "max_bytes", "literal"})
        return cls(data["name"], _enum(FieldKind, data["kind"]), data["required"],
                   data["min_chars"], data["max_chars"], data["max_bytes"], data["literal"])

    def to_data(self) -> dict:
        return {"name": self.name, "kind": self.kind.value, "required": self.required,
                "min_chars": self.min_chars, "max_chars": self.max_chars,
                "max_bytes": self.max_bytes, "literal": self.literal}


@dataclass(frozen=True, slots=True)
class FrozenPayload:
    """Owned sorted tuple of exact strings; contains no executable value."""
    fields: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.fields) is not tuple or not 1 <= len(self.fields) <= 16:
            raise ContractError("invalid payload fields")
        names = []
        for pair in self.fields:
            if type(pair) is not tuple or len(pair) != 2:
                raise ContractError("invalid payload pair")
            _name(pair[0], "payload field")
            _text(pair[1], "payload value", MAX_TEXT_BYTES)
            names.append(pair[0])
        if names != sorted(set(names)):
            raise ContractError("payload fields must be unique and sorted")
        canonical_json(self.to_data())

    @classmethod
    def from_data(cls, value: object) -> FrozenPayload:
        if type(value) is not dict:
            raise ContractError("invalid payload object")
        canonical_json(value)
        return cls(tuple(sorted(value.items())))

    def to_data(self) -> dict:
        return dict(self.fields)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_data())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class StepDeclaration:
    kind: StepKind
    max_count: int

    def __post_init__(self) -> None:
        _typed(self.kind, StepKind, "step kind")
        _integer(self.max_count, "step bound", 1, 1 if self.kind is StepKind.COMMIT else 4)

    @classmethod
    def from_data(cls, value: object) -> StepDeclaration:
        data = _object(value, {"kind", "max_count"})
        return cls(_enum(StepKind, data["kind"]), data["max_count"])

    def to_data(self) -> dict:
        return {"kind": self.kind.value, "max_count": self.max_count}


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    schema_version: int
    version: int
    capability_id: str
    adapter_id: str
    namespace: str
    runtimes: tuple[Runtime, ...]
    origins: tuple[str, ...]
    path_policy: PathPolicy
    destinations: tuple[DestinationRule, ...]
    payload_fields: tuple[PayloadField, ...]
    principal: PrincipalPolicy
    consequence: Consequence
    steps: tuple[StepDeclaration, ...]
    remote_evidence_rule: RemoteEvidenceRule
    evidence: tuple[EvidenceKind, ...]

    def __post_init__(self) -> None:
        _integer(self.schema_version, "schema version", SCHEMA_VERSION, SCHEMA_VERSION)
        _integer(self.version, "manifest version", 1, 2**31 - 1)
        _name(self.capability_id, "capability ID")
        _name(self.adapter_id, "adapter ID")
        _name(self.namespace, "namespace")
        if not self.capability_id.startswith(self.adapter_id + "."):
            raise ContractError("capability/adapter mismatch")
        _tuple(self.runtimes, Runtime, "runtimes")
        _tuple(self.origins, str, "origins")
        for origin in self.origins:
            _text(origin, "origin", 256)
            if not re.fullmatch(r"https://(?:[a-z0-9]+(?:-[a-z0-9]+)*\.)+[a-z]{2,63}", origin):
                raise ContractError("noncanonical or unsupported origin")
        _typed(self.path_policy, PathPolicy, "path policy")
        _tuple(self.destinations, DestinationRule, "destination schemas")
        if len({rule.kind for rule in self.destinations}) != len(self.destinations):
            raise ContractError("duplicate destination kind")
        _tuple(self.payload_fields, PayloadField, "payload schemas")
        if len({field.name for field in self.payload_fields}) != len(self.payload_fields):
            raise ContractError("duplicate payload field")
        _typed(self.principal, PrincipalPolicy, "principal policy")
        _typed(self.consequence, Consequence, "consequence")
        if self.consequence is not Consequence.EXTERNAL_COMMIT:
            raise ContractError("only external-commit manifests are supported in this schema")
        _tuple(self.steps, StepDeclaration, "steps")
        kinds = tuple(step.kind for step in self.steps)
        if len(set(kinds)) != len(kinds) or kinds[-1] is not StepKind.COMMIT:
            raise ContractError("workflow must end in exactly one declared commit")
        if StepKind.PREPARE in kinds and StepKind.FILL in kinds and kinds.index(StepKind.PREPARE) > kinds.index(StepKind.FILL):
            raise ContractError("prepare must precede fill")
        _typed(self.remote_evidence_rule, RemoteEvidenceRule, "remote evidence rule")
        _tuple(self.evidence, EvidenceKind, "evidence vocabulary")
        if set(self.evidence) != set(EvidenceKind):
            raise ContractError("created-object evidence threshold cannot be weakened")
        canonical_json(self.to_data())

    @classmethod
    def from_data(cls, value: object) -> CapabilityManifest:
        data = _object(value, {"schema_version", "version", "capability_id", "adapter_id", "namespace",
                              "runtimes", "origins", "path_policy", "destinations", "payload_fields",
                              "principal", "consequence", "steps", "remote_evidence_rule", "evidence"})
        return cls(data["schema_version"], data["version"], data["capability_id"], data["adapter_id"],
                   data["namespace"], tuple(_enum(Runtime, item) for item in _array(data["runtimes"])),
                   tuple(_array(data["origins"])), _enum(PathPolicy, data["path_policy"]),
                   tuple(DestinationRule.from_data(item) for item in _array(data["destinations"])),
                   tuple(PayloadField.from_data(item) for item in _array(data["payload_fields"])),
                   PrincipalPolicy.from_data(data["principal"]), _enum(Consequence, data["consequence"]),
                   tuple(StepDeclaration.from_data(item) for item in _array(data["steps"])),
                   _enum(RemoteEvidenceRule, data["remote_evidence_rule"]),
                   tuple(_enum(EvidenceKind, item) for item in _array(data["evidence"])))

    def to_data(self) -> dict:
        return {"schema_version": self.schema_version, "version": self.version,
                "capability_id": self.capability_id, "adapter_id": self.adapter_id, "namespace": self.namespace,
                "runtimes": [item.value for item in self.runtimes], "origins": list(self.origins),
                "path_policy": self.path_policy.value, "destinations": [item.to_data() for item in self.destinations],
                "payload_fields": [item.to_data() for item in self.payload_fields], "principal": self.principal.to_data(),
                "consequence": self.consequence.value, "steps": [item.to_data() for item in self.steps],
                "remote_evidence_rule": self.remote_evidence_rule.value, "evidence": [item.value for item in self.evidence]}

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_data())

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def validate_destination(self, destination: DestinationIdentity) -> None:
        _typed(destination, DestinationIdentity, "destination")
        if destination.namespace != self.namespace:
            raise ContractError("destination namespace mismatch")
        rules = [rule for rule in self.destinations if rule.kind == destination.kind]
        if len(rules) != 1:
            raise ContractError("undeclared destination kind")
        rules[0].validate(destination.canonical_key)

    def validate_payload(self, payload: FrozenPayload) -> None:
        _typed(payload, FrozenPayload, "payload")
        values = payload.to_data()
        declared = {field.name for field in self.payload_fields}
        required = {field.name for field in self.payload_fields if field.required}
        if not required <= set(values) <= declared:
            raise ContractError("missing or unknown payload fields")
        for field in self.payload_fields:
            if field.name in values:
                field.validate(values[field.name])


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Validated exact scope, NOT an authorization, claim or dispatch permit."""
    manifest: CapabilityManifest
    runtime: Runtime
    destination: DestinationIdentity
    payload: FrozenPayload
    consequence: Consequence

    def __post_init__(self) -> None:
        _typed(self.manifest, CapabilityManifest, "manifest")
        _typed(self.runtime, Runtime, "runtime")
        if self.runtime not in self.manifest.runtimes:
            raise ContractError("undeclared runtime")
        _typed(self.consequence, Consequence, "consequence")
        if self.consequence is not self.manifest.consequence:
            raise ContractError("consequence mismatch")
        self.manifest.validate_destination(self.destination)
        self.manifest.validate_payload(self.payload)
        canonical_json(self.to_data())

    def to_data(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "manifest_fingerprint": self.manifest.fingerprint,
                "capability_id": self.manifest.capability_id, "runtime": self.runtime.value,
                "destination": self.destination.to_data(), "payload": self.payload.to_data(),
                "consequence": self.consequence.value}

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_data())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()
