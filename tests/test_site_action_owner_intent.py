"""Raw owner intake, stable admission and real restart/setup process seams."""
from dataclasses import replace
import json
import multiprocessing
import os
from types import SimpleNamespace

import pytest

from chrome_companion.site_actions import claims as c, owner_intent as o
from chrome_companion.site_actions.model import ContractError
from test_site_action_kernel import World, command, spec_for


@pytest.fixture(params=["reddit.reply", "reddit.create_post"])
def world(tmp_path, request):
    world = World(tmp_path, request.param)
    yield world
    world.close()


def test_exact_grammar_preserves_payload_bytes_and_is_not_authority(world):
    parsed = o.parse_exact(command(world.spec))
    assert parsed.canonical_bytes == world.spec.canonical_bytes
    assert parsed.payload.canonical_bytes == world.spec.payload.canonical_bytes
    with pytest.raises(ContractError):
        world.kernel.authorize(parsed)


@pytest.mark.parametrize("change", ["prefix", "quote", "fence", "trailing", "duplicate", "unknown", "bool",
    "float", "nan", "runtime", "destination", "payload", "surrogate", "oversize", "deep", "blank", "version"])
def test_narrow_grammar_rejects_ambiguous_or_lossy_commands(world, change):
    raw = command(world.spec)
    data = json.loads(raw[len(o.PREFIX):])
    if change == "prefix": raw = "Please " + raw
    elif change == "quote": raw = json.dumps(raw)
    elif change == "fence": raw = "```\n" + raw + "\n```"
    elif change == "trailing": raw += " and submit again"
    elif change == "duplicate": raw = raw.replace('"v": 1', '"v": 1, "v": 1')
    elif change == "surrogate": raw = raw.replace('"body":', '"body": "\\ud800", "ignored":')
    elif change == "oversize": raw += " " * (25 * 1024)
    elif change == "deep": raw = o.PREFIX + "[" * 20 + "0" + "]" * 20
    elif change == "blank": raw = ""
    else:
        if change == "unknown": data["approve"] = True
        elif change == "bool": data["v"] = True
        elif change == "float": data["v"] = 1.0
        elif change == "nan": data["v"] = float("nan")
        elif change == "runtime": data["runtime"] = "api"
        elif change == "destination": data["destination"]["id"] = True
        elif change == "payload": data["payload"]["submit"] = True
        elif change == "version": data["v"] = 2
        raw = o.PREFIX + json.dumps(data)
    with pytest.raises((ContractError, ValueError)):
        o.parse_exact(raw)
    assert not list((world.data / "chrome_companion" / c.STORE_DIR).glob("*.admission.json"))


@pytest.mark.parametrize("source", ["TOOL_OUTPUT", "FILE_CONTENT", "EXTERNAL_CHANNEL_INBOUND", "MODEL", None, True])
def test_external_page_model_attachment_sources_cannot_admit(world, source):
    with pytest.raises(ContractError):
        world.intake.capture(world.agent, source=source, raw_text=command(world.spec), event_id="fake-event")
    assert not list((world.data / "chrome_companion" / c.STORE_DIR).glob("*.admission.json"))


@pytest.mark.parametrize("actor", ["nonowner", "otherowner", "lostowner", "dict"])
def test_nonowner_and_other_agent_cannot_borrow_intake_identity(world, actor):
    agent = SimpleNamespace(owner=False) if actor == "nonowner" else SimpleNamespace(owner=True)
    if actor == "lostowner":
        world.agent.owner = False
        agent = world.agent
    if actor == "dict": agent = {"owner": True}
    with pytest.raises(ContractError):
        world.intake.capture(agent, source="OWNER_DIRECT", raw_text=command(world.spec), event_id="parent-owned")
    with pytest.raises(ContractError):
        o.OwnerIntake(SimpleNamespace(owner=False), world.store, session="s", task="t", channel="c", chat="c", authority_domain="d")


@pytest.mark.parametrize("event", [None, "", True, 1])
def test_missing_or_asserted_event_identity_is_not_minted(world, event):
    with pytest.raises(ContractError):
        world.capture(event)


def test_original_deadline_and_event_key_cannot_be_refreshed(world):
    admission = world.capture()
    original = world.intake.resolve(admission)
    world.time += 1
    other = world.capture("distinct-real-event")
    assert world.intake.resolve(other).key != original.key
    for raw in (command(world.spec), "different task", command(spec_for("reddit.reply"))):
        with pytest.raises((ContractError, c.ClaimStoreError)):
            world.intake.capture(world.agent, source="OWNER_DIRECT", raw_text=raw, event_id="event")
    # A conflicting immutable write poisons its handle by the accepted B1.2
    # contract. Inspect through a new explicit reader; no repair or renewal.
    with o.reopen_claim_store(world.data) as reader:
        assert reader.inspect(original.key, c.RecordKind.ADMISSION).metadata == original.metadata
    with pytest.raises(ContractError):
        world.intake.resolve(replace(admission))
    world.time = original.metadata.expires_ns
    with pytest.raises(ContractError):
        world.intake.resolve(admission)


def _restart_probe(data, capability, event, output, barrier=None):
    from chrome_companion.site_actions import kernel as k
    from chrome_companion.site_actions.model import Runtime, StepKind, PrincipalEvidenceRule
    try:
        with o.reopen_claim_store(data) as store:
            if barrier is not None:
                barrier.wait(timeout=20)
            agent = SimpleNamespace(owner=True)
            intake = o.OwnerIntake(agent, store, session="new-session", task="task", channel="desktop", chat="chat",
                                  authority_domain="desktop/owner/chat", clock=lambda: 200)
            spec = spec_for(capability)
            admission = intake.capture(agent, source="OWNER_DIRECT", raw_text=command(spec), event_id=event)
            observation = k.Observation(Runtime.CHROME_COMPANION, "new-instance", "new-connection", "new-worker", 1, 2,
                "https://www.reddit.com", "https://www.reddit.com/synthetic", "document", "commit", StepKind.COMMIT,
                1, 200, spec.destination, spec.payload.digest,
                k.PrincipalObservation("A", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT), True, True, True, False, False, 0)
            kernel = k.AuthorityKernel(intake, lambda spec, kind: observation)
            handle = kernel.authorize(admission)
            claim = kernel.claim(handle, StepKind.COMMIT)
            permit = kernel.consume_commit(handle, claim)
            output.put(("permit", permit.specification.digest, store.identity.epoch))
    except (ContractError, c.ClaimStoreError, OSError):
        output.put(("denied",))


@pytest.mark.parametrize("phase", ["admitted", "bound", "spent"])
def test_real_process_restart_rehydrates_trusted_identity_but_not_live_authority(world, phase):
    admission = world.capture()
    if phase != "admitted":
        handle = world.kernel.authorize(admission)
        if phase == "spent":
            from chrome_companion.site_actions.model import StepKind
            world.send(handle, StepKind.COMMIT)
    world.kernel.retire()
    with pytest.raises(ContractError):
        world.intake.resolve(admission)
    context = multiprocessing.get_context("spawn")
    results = []
    for event in ("event", "fresh-event-after-restart"):
        output = context.Queue()
        process = context.Process(target=_restart_probe, args=(str(world.data), world.spec.manifest.capability_id, event, output))
        process.start()
        results.append(output.get(timeout=30))
        process.join(30)
        assert process.exitcode == 0
        output.close()
    assert results[0] == ("denied",)
    assert results[1] == ("permit", world.spec.digest, world.identity.epoch)


def test_two_fresh_processes_one_admission_binding_and_possible_commit(world):
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_restart_probe, args=(str(world.data), world.spec.manifest.capability_id,
                  "contended-owner-event", output, barrier)) for _ in range(2)]
    try:
        for process in processes: process.start()
        results = [output.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
        assert sorted(result[0] for result in results) == ["denied", "permit"]
        key = c.EventKey.from_ingress("desktop/owner/chat", "contended-owner-event")
        with o.reopen_claim_store(world.data) as reader:
            assert reader.inspect(key, c.RecordKind.SPENT).kind is c.RecordKind.SPENT
    finally:
        for process in processes:
            process.join(20)
            if process.is_alive(): process.kill(); process.join(20)
        output.close()


def _forked_live_claim(world, handle, claim, output, commit):
    try:
        permit = world.kernel.consume_commit(handle, claim) if commit else world.kernel.consume_step(handle, claim)
        output.put(type(permit).__name__)
    except ContractError:
        output.put("denied")


@pytest.mark.parametrize("commit", [False, True])
def test_forked_service_cannot_inherit_live_process_authority(world, commit):
    from chrome_companion.site_actions.model import StepKind
    handle = world.authorize()
    kind = StepKind.COMMIT if commit else StepKind.FILL
    world.target(kind)
    claim = world.kernel.claim(handle, kind)
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    child = context.Process(target=_forked_live_claim, args=(world, handle, claim, output, commit))
    child.start()
    assert output.get(timeout=20) == "denied"
    child.join(20)
    assert child.exitcode == 0
    output.close()
    # The unrelated child denial cannot invalidate the original parent reducer.
    permit = world.kernel.consume_commit(handle, claim) if commit else world.kernel.consume_step(handle, claim)
    assert permit.kind is kind


@pytest.mark.parametrize("damage", ["missing_pin", "corrupt_pin", "mismatch_pin", "pin_symlink", "pin_hardlink",
    "pin_fifo", "pin_mode", "missing_marker", "missing_reference", "replacement_pair"])
def test_restart_never_adopts_or_repairs_missing_corrupt_or_replaced_identity(world, damage):
    pin = world.data / c.REFERENCE_FILE
    marker = world.data / "chrome_companion" / c.STORE_DIR / c.IDENTITY_FILE
    reference = world.data / "chrome_companion" / c.REFERENCE_FILE
    if damage == "missing_pin": pin.unlink()
    elif damage == "corrupt_pin": pin.write_bytes(b"{}")
    elif damage == "mismatch_pin": pin.write_bytes(c.canonical_json(replace(world.identity, epoch="0" * 64).to_data()))
    elif damage == "pin_symlink":
        pin.rename(world.data / "old-pin"); pin.symlink_to("old-pin")
    elif damage == "pin_hardlink": os.link(pin, world.data / "pin-copy")
    elif damage == "pin_fifo": pin.unlink(); os.mkfifo(pin, 0o600)
    elif damage == "pin_mode": pin.chmod(0o644)
    elif damage == "missing_marker": marker.unlink()
    elif damage == "missing_reference": reference.unlink()
    else:
        (world.data / "chrome_companion").rename(world.data / "original-companion")
        replacement = c.initialize_store(world.data)
        assert replacement.epoch != world.identity.epoch
    before = sorted(str(p.relative_to(world.data)) for p in world.data.rglob("*"))
    with pytest.raises((ContractError, c.ClaimStoreError, OSError)):
        o.reopen_claim_store(world.data)
    assert sorted(str(p.relative_to(world.data)) for p in world.data.rglob("*")) == before


def test_pin_is_explicit_one_time_and_restart_does_not_create_it(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    identity = c.initialize_store(data)
    with pytest.raises(FileNotFoundError):
        o.reopen_claim_store(data)
    assert not (data / c.REFERENCE_FILE).exists()
    o.pin_store_identity(data, identity)
    with pytest.raises(c.ClaimStoreError):
        o.pin_store_identity(data, identity)
    with o.reopen_claim_store(data) as store:
        assert store.identity == identity


def _initialization_contender(data, entered, release, output, first):
    original = c._exclusive_write
    if first:
        def interrupted(fd, name, raw):
            if name == c.IDENTITY_FILE:
                entered.set()
                assert release.wait(20)
            return original(fd, name, raw)
        c._exclusive_write = interrupted
    try:
        identity = c.initialize_store(data)
        output.put(("identity", identity))
    except (c.ClaimStoreError, OSError):
        output.put(("denied",))


def test_R2_two_process_initializers_never_adopt_partial_winner_or_create_second_epoch(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    context = multiprocessing.get_context("spawn")
    entered, release, output = context.Event(), context.Event(), context.Queue()
    first = context.Process(target=_initialization_contender, args=(str(data), entered, release, output, True))
    second = context.Process(target=_initialization_contender, args=(str(data), entered, release, output, False))
    try:
        first.start()
        assert entered.wait(20)
        assert not (data / "chrome_companion" / c.REFERENCE_FILE).exists()
        assert not (data / "chrome_companion" / c.STORE_DIR / c.IDENTITY_FILE).exists()
        second.start()
        assert output.get(timeout=20) == ("denied",)
        second.join(20)
        assert second.exitcode == 0
        # A restart path also refuses while setup is partial and unpinned.
        with pytest.raises((OSError, c.ClaimStoreError)):
            o.reopen_claim_store(data)
        release.set()
        result = output.get(timeout=20)
        first.join(20)
        assert first.exitcode == 0 and result[0] == "identity"
        identity = result[1]
        with c.ClaimStore(data, identity) as store:
            assert store.identity == identity
        assert len(list((data / "chrome_companion" / c.STORE_DIR).iterdir())) == 1
        with pytest.raises(c.ClaimStoreError):
            c.initialize_store(data)
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None:
                process.join(20)
                if process.is_alive(): process.kill(); process.join(20)
        output.close()
