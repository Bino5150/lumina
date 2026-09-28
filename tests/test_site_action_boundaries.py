"""B1.1–B1.4 structural gates, including scratch changes that must break them.

This is a closed audited data boundary, not a sandbox for hostile Python.
Future checkpoint modules must explicitly extend these ownership rules.
"""
import ast
from pathlib import Path
import re
import subprocess
import sys

import pytest

from chrome_companion import protocol
from chrome_companion.site_actions.model import CapabilityManifest, ContractError, decode_json
from chrome_companion.site_actions.registry import CAPABILITY_IDS


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "chrome_companion" / "site_actions"
FILES = {"__init__.py", "model.py", "registry.py", "claims.py", "kernel.py", "owner_intent.py", "review.py", "runtime_guard.py",
         "manifests/reddit.reply.json", "manifests/reddit.create_post.json"}
WIRE_OPS = {"ping", "list_tabs", "get_active_tab", "get_tab", "extract_text", "get_links",
            "open_owner_url", "switch_tab"}
WIRE_TAB_RULES = {"ping": "none", "list_tabs": "none", "get_active_tab": "none",
                  "get_tab": "required", "extract_text": "optional", "get_links": "optional",
                  "open_owner_url": "none", "switch_tab": "required"}
TOOLS = {"chrome_status", "chrome_list_tabs", "chrome_get_active_tab", "chrome_get_url_title",
         "chrome_extract_visible_text", "chrome_get_links", "chrome_open_owner_url", "chrome_switch_tab"}

# Allowed dependency edges, not a blacklist of today's browser modules.
IMPORTS = {
    "__init__.py": set(),
    "model.py": {"from __future__ import annotations", "from dataclasses import dataclass",
                 "from enum import Enum", "import hashlib", "import json", "import re"},
    "registry.py": {"from pathlib import Path",
                    "from .model import CapabilityManifest, ContractError, MAX_JSON_BYTES, decode_json"},
    "claims.py": {"from __future__ import annotations", "from dataclasses import dataclass",
                  "from enum import Enum", "import hashlib", "import os", "import re", "import secrets",
                  "import stat", "import threading", "from chrome_companion import state",
                  "from .model import ContractError, Runtime, canonical_json, decode_json"},
    "owner_intent.py": {"from __future__ import annotations", "from dataclasses import dataclass",
        "import hashlib", "import os", "import secrets", "import threading", "import time",
        "from chrome_companion import state", "from . import claims", "from .registry import load_manifest",
        "from .model import ActionSpec, Consequence, ContractError, DestinationIdentity, FrozenPayload, MAX_JSON_BYTES, Runtime, canonical_json, decode_json"},
    "review.py": {"from __future__ import annotations", "from dataclasses import dataclass",
        "import secrets", "import threading", "from .model import ActionSpec, ContractError",
        "from .owner_intent import AdmittedIntent, OwnerAdmission, OwnerIntake, digest"},
    "kernel.py": {"from __future__ import annotations", "from dataclasses import dataclass, replace",
        "from enum import Enum", "import secrets", "import threading", "from urllib.parse import urlsplit",
        "from . import claims", "from .owner_intent import OwnerAdmission, OwnerIntake, digest, text",
        "from .review import ReviewApproval, ReviewController, ReviewSnapshot",
        "from .model import ActionSpec, ContractError, DestinationIdentity, EvidenceKind, PrincipalEvidenceRule, PrincipalRequirement, ReceiptState, Runtime, StepKind, UnavailablePolicy"},
}
# Exact reviewed call edges. Attribute-name-only matching would allow a newly
# acquired actuator with an innocent method name to escape the dependency gate.
CALLS = {
    "__init__.py": set(),
    "model.py": {
        "ContractError", "DestinationRule.from_data", "PayloadField.from_data",
        "PrincipalPolicy.from_data", "StepDeclaration.from_data", "_array", "_check_json",
        "_enum", "_integer", "_name", "_object", "_text", "_tuple", "_typed", "canonical_json",
        "cls", "dataclass", "dict", "field.validate", "hashlib.sha256",
        "hashlib.sha256(self.canonical_bytes).hexdigest", "int", "item.to_data", "json.dumps",
        "json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode",
        "json.loads", "key.startswith", "kinds.index", "len", "list", "names.append",
        "payload.to_data", "raw.decode", "re.fullmatch", "rules[0].validate",
        "self.capability_id.startswith", "self.destination.to_data", "self.manifest.validate_destination",
        "self.manifest.validate_payload", "self.payload.to_data", "self.principal.to_data",
        "self.to_data", "self.validate", "set", "sorted", "tuple", "type", "value.encode",
        "value.items", "value.lstrip",
    },
    "registry.py": {"CapabilityManifest.from_data", "ContractError", "Path", "Path(__file__).with_name",
                    "decode_json", "next", "path.open", "stream.read", "tuple", "type"},
    "claims.py": {
        "AdmissionMetadata", "BindingMetadata", "ClaimRecord", "ClaimRecord.from_data", "ClaimStore",
        "ClaimStoreError", "EventKey", "RecordKind", "Runtime", "StoreIdentity", "StoreIdentity.from_data",
        "_digest", "_directory", "_entry", "_exclusive_write", "_fields", "_file", "_leaf", "_number", "_read", "_stamp",
        "b''.join", "canonical_json", "chunks.append", "cls", "dataclass", "decode_json", "getattr",
        "hashlib.sha256", "hashlib.sha256(encoded).hexdigest", "hashlib.sha256(self.canonical_bytes).hexdigest",
        "identity.to_data", "len", "memoryview", "min", "os.close", "os.fstat", "os.fsync", "os.getuid",
        "os.mkdir", "os.open", "os.read", "os.stat", "os.write", "re.fullmatch", "secrets.token_hex",
        "self._check_live", "self._create", "self._name", "self._read_record", "self.close", "self.metadata.to_data",
        "self.to_data", "set", "setattr", "stat.S_IMODE", "stat.S_ISDIR", "stat.S_ISREG", "state.absolute_path",
        "state.open_companion_dir", "state.open_private_subdir", "threading.RLock", "type",
        "state.open_trusted_dir",
    },
    "owner_intent.py": {
        "ActionSpec", "AdmittedIntent", "ContractError", "DestinationIdentity", "FrozenPayload.from_data",
        "OwnerAdmission", "canonical_json", "claims.AdmissionMetadata", "claims.ClaimStore", "claims.ClaimStoreError",
        "claims.EventKey.from_ingress", "claims.StoreIdentity.from_data", "claims._exclusive_write", "claims._number",
        "claims._read", "dataclass", "decode_json", "digest", "expected.to_data", "getattr", "hashlib.sha256",
        "hashlib.sha256(canonical_json(value)).hexdigest", "len", "load_manifest", "os.close", "os.fstat", "parse_exact",
        "raw.startswith", "raw_text.startswith", "secrets.token_hex", "self._admissions.clear", "self._admissions.get", "os.getpid",
        "self._clock", "self._owner", "self.now", "self.store.admit", "self.resolve", "self._review_only.add", "self._review_only.clear",
        "set", "state.open_trusted_dir", "store.close",
        "text", "threading.RLock", "type", "value.encode",
    },
    "review.py": {"ContractError", "ReviewApproval", "ReviewRequest", "dataclass", "digest", "secrets.token_hex",
        "self._approvals.clear", "self._approvals.get", "self._request", "self._requests.clear", "self._requests.get",
        "self.intake._owner", "self.intake._review_event", "self.intake.now", "self.intake.resolve",
        "self.specification.to_data", "threading.RLock", "type", "list", "self._requests.items", "self._approvals.items", "self.intake._require_review"},
    "kernel.py": {"ActionReceipt", "CommitClaim", "ContractError", "DispatchPermit", "ReviewSnapshot", "StepClaim",
        "WorkflowAuthorization", "_Workflow", "any", "claims.BindingMetadata", "claims._digest", "claims._number",
        "dataclass", "digest", "enumerate", "len", "next", "ord", "parsed.path.startswith", "replace", "secrets.token_hex",
        "self._claimed", "self._control", "self._controls", "self._current", "self._events.add", "self._evidence",
        "self._invalidate", "self._observation", "self._observe", "self._principal", "self._workflow", "self._workflows.get",
        "self._workflows.values", "self.destination.to_data", "self.intake._owner", "self.intake.invalidate", "self.intake.now",
        "self.intake.resolve", "self.intake.resolve_direct", "self.intake.retire", "self.intake.store.bind", "self.intake.store.consume", "self.review.create",
        "self.review.invalidate", "self.review.resolve", "set", "text", "threading.RLock", "type", "url.path.startswith",
        "urlsplit", "workflow.counts.get"},
}


# Audited B1.4 broker edges. No callable actor/provider is acquired here.
IMPORTS["runtime_guard.py"] = {'from .review import ReviewController', 'from .model import ContractError', 'import asyncio', 'import weakref', 'import threading', 'import os', 'from .owner_intent import OwnerIntake, reopen_claim_store', 'from contextvars import ContextVar', 'from core import emergency_stop', 'from dataclasses import dataclass', 'from .kernel import AuthorityKernel', 'import secrets', 'from __future__ import annotations', 'from contextlib import contextmanager'}
CALLS["runtime_guard.py"] = {'ROUTES.get', 'entry[1].review.approve', '_Task', 'self._context.reset', 'emergency_stop.is_latched', 'task.route.deliver', 'weakref.WeakKeyDictionary', 'dataclass', 'raw_text.startswith', 'self._frames.setdefault', 'self._deliveries.get', 'task.store.close', 'weakref.ref', 'ContextVar', 'task.intake.resolve', '_broker.dispatch', 'set', 'self._context.get', 'self._close_delivery', 'any', 'self._registries.get', 'task.intake.capture', 'list', '_broker.turn', '_execution', 'entry[1].review.cancel', 'ReviewDelivery', 'TaskIdentity', 'task.review._request', 'self._deliveries.values', '_broker.bind_agent', 'self._context.set', 'task.review.invalidate', 'reopen_claim_store', 'route.protected.add', 'AgentRoute', 'str', 'entry[1].review.present', 'origin.route.registry', 'self._registries.values', 'self._tasks.get', 'self._frames.get', 'task.route.agent', 'getattr', 'secrets.token_hex', 'self._frames.setdefault(execution, []).append', 'RoutingBroker', 'self._agents.get', 'ContractError', 'task.route.protected.discard', 'os.getpid', 'OwnerIntake', 'kernel._observation', 'asyncio.current_task', 'self._origin', 'threading.RLock', 'route.agent', 'task.intake.retire', 'threading.current_thread', 'type', 'ReviewController', 'self._deliveries.pop', 'self._review_current', 'self._frames[execution].remove'}

def _package_sources():
    return {path.relative_to(PACKAGE).as_posix(): path.read_text()
            for path in PACKAGE.rglob("*") if path.is_file() and "__pycache__" not in path.parts}


def _check_package(sources):
    assert set(sources) == FILES, "checkpoint package expanded beyond B1.4"
    for name, allowed in IMPORTS.items():
        tree = ast.parse(sources[name])
        imports = {ast.unparse(node) for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))}
        assert imports <= allowed, f"forbidden dependency in {name}: {imports - allowed}"
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        assert calls <= CALLS[name], f"forbidden call edge in {name}: {calls - CALLS[name]}"
        for node in ast.walk(tree):
            assert not isinstance(node, (ast.Lambda, ast.AsyncFunctionDef, ast.Await)), "executable hook"
            if isinstance(node, ast.ClassDef):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call):
                        assert ast.unparse(decorator.func) == "dataclass"
                        assert {k.arg: ast.literal_eval(k.value) for k in decorator.keywords} == {
                            "frozen": True, "slots": True}, "mutable schema class"
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "path.open":
                assert [ast.literal_eval(arg) for arg in node.args] == ["rb"]
                assert not node.keywords, "manifest loader acquired write/custom open authority"
        # No import-time expressions except inert declarations/closed ID tuple.
        for node in tree.body:
            if isinstance(node, ast.Expr):
                assert isinstance(node.value, ast.Constant) and type(node.value.value) is str
            else:
                assert isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.ClassDef, ast.FunctionDef))
                if isinstance(node, ast.Assign):
                    startup_calls = [n for n in ast.walk(node.value) if isinstance(n, ast.Call)]
                    assert not startup_calls or (name == "registry.py" and
                        ast.unparse(node) == "CAPABILITY_IDS = tuple((capability for capability, _ in _MANIFEST_FILES))") or (
                        name == "runtime_guard.py" and ast.unparse(node) == "_broker_lock = threading.RLock()")
    for capability in CAPABILITY_IDS:
        manifest = CapabilityManifest.from_data(decode_json(sources[f"manifests/{capability}.json"]))
        assert manifest.capability_id == capability
    # Loader paths themselves are literal reviewed data, not caller-controlled.
    registry = ast.parse(sources["registry.py"])
    mapping = next(node.value for node in registry.body if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_MANIFEST_FILES" for t in node.targets))
    assert ast.literal_eval(mapping) == tuple((c, c + ".json") for c in CAPABILITY_IDS)
    _check_claim_storage(ast.parse(sources["claims.py"]))
    _check_authority_edges(sources)
    _check_routing_broker(sources["runtime_guard.py"])


def _check_authority_edges(sources):
    # No new reducer per site, no constructor/callback actuation elsewhere.
    kernel = ast.parse(sources["kernel.py"])
    assert not re.search(r"reddit|t[135]_|post_id|subreddit", sources["kernel.py"]), "site-specific authority reducer"
    owners = {}
    for name in ("kernel.py", "owner_intent.py", "review.py"):
        tree = ast.parse(sources[name])
        owners[name] = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        for method in owners[name].values():
            calls = {ast.unparse(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)}
            if "DispatchPermit" in calls:
                assert name == "kernel.py" and method.name in {"consume_step", "consume_commit"}
            if "WorkflowAuthorization" in calls:
                assert name == "kernel.py" and method.name == "authorize"
            if "self._observe" in calls:
                assert name == "kernel.py" and method.name == "_observation"
            if "self._evidence" in calls:
                assert name == "kernel.py" and method.name == "reconcile"
            if "self._clock" in calls:
                assert name == "owner_intent.py" and method.name == "now"
            if "claims._exclusive_write" in calls:
                assert name == "owner_intent.py" and method.name == "pin_store_identity"
        # Constructor-only provider acquisition, no model/adaptor callback hooks.
        for method in owners[name].values():
            for node in ast.walk(method):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if ast.unparse(target) in {"self._observe", "self._evidence", "self._clock", "self._presenter"}:
                            assert method.name == "__init__"
    commit = owners["kernel.py"]["consume_commit"]
    calls = [ast.unparse(node.func) for node in ast.walk(commit) if isinstance(node, ast.Call)]
    assert calls.count("self.intake.store.consume") == 1, "durable spend removed/duplicated"
    assert calls.count("self._current") == 1 and calls.count("self._claimed") == 1, "pre/post spend verification removed"
    source = ast.unparse(commit)
    assert source.index("self.intake.store.consume") < source.index("return DispatchPermit"), "permit precedes spend"
    assert "if not self.intake.store.consume" in source, "spent loser could receive permit"
    for name, seal in (("_workflow", "workflow.handle is not handle"), ("_claimed", "workflow.pending is not claim")):
        assert any(isinstance(node, ast.Compare) and ast.unparse(node) == seal
                   for node in ast.walk(owners["kernel.py"][name])), "claim identity seal removed"
    for name in ("resolve",):
        assert "entry[0] is not handle" in ast.unparse(owners["owner_intent.py"][name]), "admission seal removed"
    for name in ("_request", "resolve"):
        assert "entry[0] is not " in ast.unparse(owners["review.py"][name]), "review seal removed"
    for method in owners["kernel.py"].values():
        for node in ast.walk(method):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if ast.unparse(target) in {"workflow.active", "self.active"} and node.value.value is True:
                        assert method.name == "__init__", "authority revived outside construction"
                    if ast.unparse(target) in {"workflow.commit_started", "self.commit_started"} and node.value.value is False:
                        assert method.name == "__init__", "commit claim rearmed"
    restart = owners["owner_intent.py"]["reopen_claim_store"]
    restart_calls = {ast.unparse(node.func) for node in ast.walk(restart) if isinstance(node, ast.Call)}
    assert not restart_calls & {"pin_store_identity", "claims.initialize_store", "claims._exclusive_write"}, "restart initializes/adopts identity"
    assert "claims._read(fd, claims.REFERENCE_FILE)" in ast.unparse(restart), "independent restart pin missing"


def _check_claim_storage(tree):
    assignments = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                   for target in node.targets if isinstance(target, ast.Name)}
    assert ast.unparse(assignments["_WRITE_FLAGS"]) == (
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC"), "exclusive create weakened"
    assert ast.unparse(assignments["_READ_FLAGS"]) == (
        "os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC"), "safe read weakened"
    writer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_exclusive_write")
    creation = [node for node in ast.walk(writer) if isinstance(node, ast.Call)
                and ast.unparse(node.func) == "os.open"]
    assert len(creation) == 1 and ast.unparse(creation[0]) == (
        "os.open(name, _WRITE_FLAGS, 384, dir_fd=dir_fd)"), "writer lost descriptor/permission binding"
    synchronized = next(node for node in writer.body if isinstance(node, ast.Try) and node.finalbody)
    direct = [ast.unparse(node) for node in synchronized.body]
    assert "os.fsync(fd)" in direct and "os.fsync(dir_fd)" in direct, "required sync removed/conditional"
    assert direct.index("os.fsync(fd)") < direct.index("os.fsync(dir_fd)") < direct.index("return True")
    winners = [node for node in ast.walk(writer) if isinstance(node, ast.Return)
               and isinstance(node.value, ast.Constant) and node.value.value is True]
    assert len(winners) == 1, "alternate successful writer exit"
    setup = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "initialize_store")
    setup_try = next(node for node in setup.body if isinstance(node, ast.Try))
    setup_direct = [ast.unparse(node) for node in setup_try.body]
    assert "os.fsync(data_fd)" in setup_direct, "setup parent directory sync removed/conditional"
    assert setup_direct.index("os.fsync(data_fd)") < setup_direct.index("os.mkdir(STORE_DIR, 448, dir_fd=companion_fd)")
    store = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClaimStore")
    public = {node.name for node in store.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")}
    assert public == {"close", "admit", "bind", "consume", "inspect"}, "release/repair or exposure API added"


def _check_exposure(protocol_source, worker_source, tools_source):
    tree = ast.parse(protocol_source)
    ops = next(node.value for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "OPS" for t in node.targets))
    assert isinstance(ops, ast.Call) and ast.unparse(ops.func) == "frozenset"
    assert len(ops.args) == 1 and not ops.keywords
    assert ast.literal_eval(ops.args[0]) == WIRE_OPS, "new wire authority surface"
    table = re.search(r"const OP_TAB_RULE = \{(.*?)\};", worker_source, re.S)
    assert table, "missing worker closed dispatch table"
    worker_rules = {}
    for entry in table.group(1).split(","):
        property_ = re.fullmatch(r'\s*([a-z_]+):\s*"(none|optional|required)"\s*', entry)
        assert property_, "unclassified worker dispatch entry"
        name, rule = property_.groups()
        assert name not in worker_rules, "duplicate worker dispatch entry"
        worker_rules[name] = rule
    assert worker_rules == WIRE_TAB_RULES, "new or widened worker authority surface"
    tools_tree = ast.parse(tools_source)
    registrations = [node for node in ast.walk(tools_tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "register"]
    assert len(registrations) == len(TOOLS), "new/duplicate public registration"
    assert all(node.args and isinstance(node.args[0], ast.Constant)
               and type(node.args[0].value) is str for node in registrations), "dynamic public registration"
    assert {node.args[0].value for node in registrations} == TOOLS, "new public action tool"


def _production_sources():
    sources = {}
    for directory in ("core", "tools", "ui", "chrome_companion"):
        for path in (ROOT / directory).rglob("*"):
            if path.suffix in {".py", ".js"} and PACKAGE not in path.parents:
                sources[path.relative_to(ROOT).as_posix()] = path.read_text()
    return sources


def _methods(source):
    return {n.name: n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}


def _check_routing_broker(source):
    tree = ast.parse(source)
    broker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RoutingBroker")
    methods = {n.name: n for n in ast.walk(broker) if isinstance(n, ast.FunctionDef)}
    routes = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "ROUTES" for t in n.targets))
    assert ast.literal_eval(routes) == {name: "navigation" if name in {
        "chrome_open_owner_url", "chrome_switch_tab"} else "observation" for name in TOOLS}
    dispatch = methods["dispatch"]
    protected = [n for n in ast.walk(dispatch) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "origin.protected"]
    assert len(protected) == 1 and any(isinstance(n, ast.Raise) for n in protected[0].body), "dormant route denial removed"
    assert not any(isinstance(n, (ast.Return, ast.With)) for n in protected[0].body), "protected executable route enabled"
    origin = ast.unparse(methods["_origin"])
    for seal in ("task.handle is not handle", "ambient is not anchored", "not task.alive", "os.getpid() != self._process",
                 "self._frames.get(_execution(), ())", "self._tasks.get"):
        assert seal in origin, "trusted context seal/frame removed"
    for seal in ("origin.route.registry() is not registry", "route.protected",
                 "any((r.protected for r in self._registries.values()))", "os.getpid() != self._process"):
        assert seal in ast.unparse(dispatch), "missing/foreign runtime identity opened"
    turn = ast.unparse(methods["turn"])
    for edge in ("route.owner", "agent.owner is True", "source == 'OWNER_DIRECT'", "type(raw_text) is str",
                 "raw_text.startswith('/companion-action')", "task.intake.capture", "raw_text=raw_text", "event_id=event",
                 "finally:", "task.intake.retire()", "self._context.reset(token)", "del self._tasks[task.handle.identifier]"):
        assert edge in turn, "canonical capture or finally lifetime removed"
    assert "origin.protected" in turn and "site task cannot borrow" in turn
    queue = ast.unparse(methods["queue_review"])
    assert "self._tasks.get(task.handle.identifier) is not task" in queue and "self._origin() is not task" in queue
    for name in ("approve_review", "present_review", "review_snapshot"):
        assert "self._review_current" in ast.unparse(methods[name]), "live review scope validation bypassed"
    live_review = ast.unparse(methods["_review_current"])
    for edge in ("entry[0] is not delivery", "task.review._request(request)", "kernel._observation",
                 "observation.principal.fingerprint != request.snapshot.principal_digest",
                 "observation.principal.identity != request.snapshot.principal",
                 "observation.surface_digest != request.snapshot.surface_digest",
                 "kernel._epoch != request.snapshot.control_epoch", "emergency_stop.is_latched()"):
        assert edge in live_review, "review identity/principal/scope check removed"
    # Constructor acquisition is explicit; no model/adapter route-role,
    # callback or observation provider can be installed by dispatch args.
    for method in methods.values():
        for node in ast.walk(method):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "task.route.deliver":
                assert method.name == "queue_review"
    assert "initialize_store" not in source and "pin_store_identity" not in source


def _check_integration_edges(sources):
    registry = _methods(sources["tools/registry.py"])
    call = registry["call"]
    scopes = [n for n in ast.walk(call) if isinstance(n, ast.With)
              and any(ast.unparse(i.context_expr) == "dispatch_scope(self, name)" for i in n.items)]
    assert len(scopes) == 1 and any(ast.unparse(n) == "return self._call_guarded(name, args)"
                                  for n in scopes[0].body), "registry dispatch guard removed/bypassed"
    assert not any("['fn']" in ast.unparse(n) for n in ast.walk(call)), "alternate unguarded function call"
    guarded = ast.unparse(registry["_call_guarded"])
    assert "self._gate_fn(name)" in guarded and "emergency_stop.begin_tool_dispatch(name)" in guarded
    assert "finally:" in guarded and "emergency_stop.end_tool_dispatch(lease_id)" in guarded
    assert "name in self._disabled" in ast.unparse(call), "disabled-tool gate replaced"
    agent = _methods(sources["core/agent.py"])
    assert "bind_agent(self, self.registry, config.DATA_DIR)" in ast.unparse(agent["__init__"])
    chat = ast.unparse(agent["chat"])
    hook = "site_action_turn(self, raw_text=user_input, source=source, chat=chat_id, event=site_action_event)"
    assert hook in chat, "trusted raw ingress hook removed/widened"
    assert chat.index("source = 'EXTERNAL_CHANNEL_INBOUND'") < chat.index("approval_event_id =") < chat.index(hook)
    assert "site_action_event = approval_event_id" in chat and "if site_action_event is None:" in chat
    assert chat.index(hook) < chat.index("vision_route_ctx =") < chat.index("result = LuminaAgent._chat_impl")
    qt = _methods(sources["ui/site_action_review.py"])
    assert "self.text.setPlainText(self._rendered)" in ast.unparse(qt["__init__"])
    assert "self.text.setReadOnly(True)" in ast.unparse(qt["__init__"])
    assert "self.approve.clicked.connect(self._approve_clicked)" in ast.unparse(qt["__init__"])
    assert "self.cancel.clicked.connect(self.reject)" in ast.unparse(qt["__init__"])
    for edge in ("not self.isVisible()", "not self._current_scope()", "self.text.toPlainText() != self._rendered",
                 "is not self._snapshot"):
        assert edge in ast.unparse(qt["_current"]), "actual UI presentation check removed"
    click = ast.unparse(qt["_approve_clicked"])
    assert "self._current()" in click and "self._broker.approve_review" in click
    assert click.index("self._current()") < click.index("self._broker.approve_review")
    assert "not self._presented" in click and "self._decided = True" in click
    assert ast.unparse(qt["accept"]).endswith("self.reject()")
    window = _methods(sources["ui/main_window.py"])
    assert "route.broker.attach_presenter(agent, self, self.signals.site_action_review.emit)" in sources["ui/main_window.py"]
    slot = ast.unparse(window["_on_site_action_review"])
    assert "agent is not self.agent" in slot and "chat != self._current_chat_id" in slot
    assert "dialog.approved.connect(self.signals.site_action_approved.emit)" in slot
    assert not re.search(r"\.chat\(|add_user\(|_on_user_message\(", slot), "synthetic chat authority"


def _check_no_consumers(sources):
    allowed = {"core/agent.py", "tools/registry.py", "ui/main_window.py", "ui/site_action_review.py"}
    for path, source in sources.items():
        if path not in allowed:
            assert not re.search(r"site_actions|reddit\.reply|reddit\.create_post", source), (
                f"production consumer outside dormant B1.4 boundary: {path}")
        if path.endswith(".py"):
            calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)}
            assert not calls & {"AuthorityKernel", "DispatchPermit", "WorkflowAuthorization", "initialize_store",
                                "pin_store_identity", "kernel.consume_step", "kernel.consume_commit"}, "production actor/grant acquired"
    _check_integration_edges(sources)


def test_closed_package_dependency_and_call_graph():
    _check_package(_package_sources())


@pytest.mark.parametrize("name,addition", [
    ("model.py", "\nimport socket\n"),
    ("model.py", "\nfrom tools.browser import register\n"),
    ("registry.py", "\nimport importlib\n"),
    ("registry.py", "\ndef plugin(name):\n    return __import__(name)\n"),
    ("model.py", "\ndef bypass(chrome):\n    chrome.submit()\n"),
    ("model.py", "\ndef bypass(callback):\n    callback()\n"),
    ("__init__.py", "\nload_manifest('reddit.reply')\n"),
    ("claims.py", "\nfrom core import idempotency\n"),
    ("claims.py", "\ndef alternate(browser):\n    browser.click()\n"),
    ("claims.py", "\ndef release(fd, name):\n    os.unlink(name, dir_fd=fd)\n"),
])
def test_scratch_forbidden_dependencies_and_hooks_break_structural_guard(name, addition):
    sources = _package_sources()
    sources[name] += addition
    with pytest.raises(AssertionError):
        _check_package(sources)


@pytest.mark.parametrize("old,new", [
    ('path.open("rb")', 'path.open("wb")'),
    ('frozen=True, slots=True', 'frozen=False, slots=True'),
    ('"reddit.reply.json"', '"../outside.json"'),
])
def test_scratch_write_mutability_and_path_widening_break_guard(old, new):
    sources = _package_sources()
    for path in ("model.py", "registry.py"):
        sources[path] = sources[path].replace(old, new)
    with pytest.raises(AssertionError):
        _check_package(sources)


def test_scratch_extra_module_and_executable_manifest_break_guard():
    sources = _package_sources()
    sources["site_executor.py"] = "# future checkpoint\n"
    with pytest.raises(AssertionError):
        _check_package(sources)
    sources = _package_sources()
    raw = sources["manifests/reddit.reply.json"]
    sources["manifests/reddit.reply.json"] = raw.replace('"schema_version": 1,', '"script": "submit()", "schema_version": 1,')
    with pytest.raises(ContractError):
        _check_package(sources)


def test_existing_wire_worker_and_public_tools_have_no_site_action_route():
    _check_exposure((ROOT / "chrome_companion/protocol.py").read_text(),
                    (ROOT / "chrome_companion/extension/worker.js").read_text(),
                    (ROOT / "tools/chrome_companion.py").read_text())
    assert protocol.OPS == WIRE_OPS


@pytest.mark.parametrize("surface", ["protocol", "worker", "tools", "quoted_worker",
                                      "duplicate_worker", "widened_tab_rule", "duplicate_tool"])
def test_scratch_new_exposure_breaks_guard(surface):
    proto = (ROOT / "chrome_companion/protocol.py").read_text()
    worker = (ROOT / "chrome_companion/extension/worker.js").read_text()
    tools = (ROOT / "tools/chrome_companion.py").read_text()
    if surface == "protocol":
        proto = proto.replace('OPS = frozenset({"ping",', 'OPS = frozenset({"site_action_commit", "ping",')
    elif surface == "worker":
        worker = worker.replace('const OP_TAB_RULE = {',
                                'const OP_TAB_RULE = {site_action_commit: "none",')
    elif surface == "quoted_worker":
        worker = worker.replace('const OP_TAB_RULE = {',
                                'const OP_TAB_RULE = {"site_action_commit": "none",')
    elif surface == "duplicate_worker":
        worker = worker.replace('const OP_TAB_RULE = {',
                                'const OP_TAB_RULE = {ping: "none",')
    elif surface == "widened_tab_rule":
        worker = worker.replace('switch_tab: "required"', 'switch_tab: "none"')
    elif surface == "duplicate_tool":
        tools += '\nregistry.register("chrome_status", None, "", {})\n'
    else:
        tools += '\nregistry.register("site_action_commit", None, "", {})\n'
    with pytest.raises(AssertionError):
        _check_exposure(proto, worker, tools)


def test_only_dormant_B1_4_consumers_and_no_actuator():
    _check_no_consumers(_production_sources())


@pytest.mark.parametrize("consumer", [
    "from chrome_companion.site_actions.registry import load_manifest",
    "const op = 'reddit.create_post';",
    "request('reddit.reply')",
])
def test_scratch_external_consumer_breaks_guard(consumer):
    sources = _production_sources()
    sources["tools/scratch_bypass.py"] = consumer
    with pytest.raises(AssertionError):
        _check_no_consumers(sources)


@pytest.mark.parametrize("op", ["reddit.reply", "reddit.create_post", "site_action_commit", "site_action_fill"])
def test_current_protocol_rejects_speculative_ops(op):
    request = {"v": 1, "type": "request", "connection_id": "a" * 32,
               "request_id": "b" * 32, "op": op, "tab_id": None,
               "deadline_ms": 1900000000000, "args": {}}
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.validate_request(request)
    assert exc.value.code == "unknown_op"


def test_import_and_explicit_load_have_no_browser_network_storage_initialization():
    script = r'''
import dataclasses, enum, hashlib, json, pathlib, re, sys
sys.path.insert(0, sys.argv[1])
reads = []
def audit(event, args):
    if event.startswith(("socket.", "subprocess.", "sqlite3.")) or event in {
        "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.system", "os.fork", "os.posix_spawn"
    }:
        raise AssertionError((event, args))
    if event == "open":
        path, mode, flags = args
        # Write flags: WRONLY/RDWR/CREAT/TRUNC/APPEND. Module reads are allowed.
        if (mode and any(c in mode for c in "wax+")) or flags & (1 | 2 | 64 | 512 | 1024):
            raise AssertionError((event, args))
        if str(path).endswith(".json"):
            reads.append(str(path))
sys.addaudithook(audit)
import chrome_companion.site_actions
from chrome_companion.site_actions import model, registry, claims, kernel, owner_intent, review, runtime_guard
assert runtime_guard._broker is None
assert reads == [], reads
assert not any(n == "config" or n.startswith(("core.", "tools.", "ui.")) for n in sys.modules)
for capability in registry.CAPABILITY_IDS:
    manifest = registry.load_manifest(capability)
    assert type(manifest) is model.CapabilityManifest
assert len(reads) == 2, reads
assert all("/site_actions/manifests/" in p for p in reads)
print("import: no initialization; explicit load: two read-only bundled JSON files")
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no initialization" in result.stdout


@pytest.mark.parametrize("old,new", [
    ("os.O_EXCL", "os.O_TRUNC"),
    ("os.O_NOFOLLOW", "0"),
    ("os.O_NONBLOCK", "0"),
    ("os.fsync(fd)", "pass"),
    ("os.fsync(dir_fd)", "pass"),
    ("os.fsync(data_fd)", "pass"),
    ("os.fsync(dir_fd)", "if False:\n            os.fsync(dir_fd)"),
    ("dir_fd=dir_fd)", "dir_fd=None)"),
])
def test_scratch_claim_durability_and_descriptor_weakening_break_guard(old, new):
    sources = _package_sources()
    assert old in sources["claims.py"]
    sources["claims.py"] = sources["claims.py"].replace(old, new)
    with pytest.raises(AssertionError):
        _check_package(sources)


@pytest.mark.parametrize("name,addition", [
    ("kernel.py", "\nfrom tools.browser import register\n"),
    ("kernel.py", "\ndef actuator(browser):\n    browser.click()\n"),
    ("kernel.py", "\ndef unguarded(spec):\n    return DispatchPermit('fake', spec, StepKind.COMMIT, None)\n"),
    ("kernel.py", "\ndef rearm(workflow):\n    workflow.active = True\n"),
    ("kernel.py", "\ndef reset(workflow):\n    workflow.commit_started = False\n"),
    ("kernel.py", "\ndef site_authority():\n    post_id = 't3_fake'\n"),
    ("review.py", "\ndef page_approve(callback):\n    callback()\n"),
    ("review.py", "\nfrom ui.main_window import MainWindow\n"),
    ("owner_intent.py", "\nfrom core import idempotency\n"),
    ("owner_intent.py", "\ndef startup(path):\n    claims.initialize_store(path)\n"),
])
def test_scratch_authority_dependency_bypass_and_rearm_break_guards(name, addition):
    sources = _package_sources()
    sources[name] += addition
    with pytest.raises(AssertionError):
        _check_package(sources)


@pytest.mark.parametrize("name,old,new", [
    ("kernel.py", "if not self.intake.store.consume(workflow.admitted.key, workflow.binding):", "if False:"),
    ("kernel.py", "current = self._current(workflow, StepKind.COMMIT)", "current = observation"),
    ("kernel.py", "workflow.handle is not handle", "workflow.handle != handle"),
    ("kernel.py", "workflow.pending is not claim", "workflow.pending != claim"),
    ("owner_intent.py", "entry[0] is not handle", "entry[0] != handle"),
    ("review.py", "entry[0] is not approval", "entry[0] != approval"),
    ("review.py", "entry[0] is not request", "entry[0] != request"),
    ("owner_intent.py", "expected = claims.StoreIdentity.from_data(decode_json(raw))", "expected = claims.initialize_store(data_dir)"),
])
def test_scratch_removed_spend_revalidation_seal_or_restart_pin_breaks_guard(name, old, new):
    sources = _package_sources()
    assert old in sources[name]
    sources[name] = sources[name].replace(old, new)
    with pytest.raises(AssertionError):
        _check_package(sources)


@pytest.mark.parametrize("old,new", [
    ("if origin.protected:", "if False:"),
    ("task.handle is not handle", "task.handle != handle"),
    ("ambient is not anchored", "False"),
    ("os.getpid() != self._process", "False"),
    ("origin.route.registry() is not registry", "False"),
    ("source == \"OWNER_DIRECT\"", "True"),
    ("raw_text=raw_text", "raw_text=str(chat)"),
    ("self._context.reset(token)", "pass"),
    ("observation.principal.fingerprint != request.snapshot.principal_digest", "False"),
    ("observation.surface_digest != request.snapshot.surface_digest", "False"),
    ("observation.principal.identity != request.snapshot.principal", "False"),
    ("self._review_current(delivery, presenter)[3].snapshot", "None"),
    ("reopen_claim_store(task.route.data_dir)", "claims.initialize_store(task.route.data_dir)"),
    ("self._tasks.get(task.handle.identifier) is not task", "False"),
    ("self._origin() is not task", "False"),
])
def test_scratch_context_route_or_live_review_bypass_breaks_guard(old, new):
    sources = _package_sources()
    assert old in sources["runtime_guard.py"]
    sources["runtime_guard.py"] = sources["runtime_guard.py"].replace(old, new)
    with pytest.raises(AssertionError):
        _check_package(sources)


@pytest.mark.parametrize("path,old,new", [
    ("tools/registry.py", "with dispatch_scope(self, name):", "if True:"),
    ("tools/registry.py", "return self._call_guarded(name, args)", "return str(self._tools[name]['fn'](**args))"),
    ("core/agent.py", "raw_text=user_input", "raw_text=str(attachments)"),
    ("core/agent.py", "if site_action_event is None:", "if not site_action_event:"),
    ("core/agent.py", "self._site_action_route = bind_agent(self, self.registry, config.DATA_DIR)", "pass"),
    ("ui/site_action_review.py", "self.text.setPlainText(self._rendered)", "self.text.setHtml(self._rendered)"),
    ("ui/site_action_review.py", "not self.isVisible()", "False"),
    ("ui/site_action_review.py", "self._current()", "pass"),
    ("ui/site_action_review.py", "not self._presented", "False"),
    ("ui/main_window.py", "dialog.approved.connect(self.signals.site_action_approved.emit)", "self.agent.chat('approved')"),
])
def test_scratch_removed_registry_ingress_or_Qt_wiring_guard_is_detected(path, old, new):
    sources = _production_sources()
    assert old in sources[path]
    sources[path] = sources[path].replace(old, new)
    with pytest.raises(AssertionError):
        _check_no_consumers(sources)
