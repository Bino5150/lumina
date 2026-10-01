"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- rotating refresh tokens, interprocess
serialization, generation compare-and-swap, disconnect-vs-refresh races,
revocation uncertainty, terminal vs temporary failures, and process
lifecycle (crash/restart/second instance).

The fake provider implements real rotation with reuse detection: a refresh
token presented twice kills the whole family (`refresh_token_reused`). So an
unserialized double refresh is not just "two requests" -- it destroys the
session, which is exactly what these tests would observe if the refresh
lock or the double-check were removed.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from core.chatgpt_auth import oauth
from core.chatgpt_auth import store as store_mod
from core.chatgpt_auth.session import (
    AccountMismatch, ChatGPTSessionManager, IdentityNotVerified, PlanPermissionNotGranted,
    ReauthRequired, SessionState, SignInRequired, TemporaryAuthError,
)
from core.chatgpt_auth.store import SessionStore
from chatgpt_auth_fakes import NO_PLAN_SCOPES, FakeClock, FakeOpenAIAuth, provider_session

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatgpt_auth_worker.py")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    f = FakeOpenAIAuth(clock=clock)
    yield f
    f.close()


def make_manager(directory, fake, clock, **kw):
    store = SessionStore(str(directory), lock_timeout=kw.pop("lock_timeout", 30))
    provider = oauth.ProviderClient(fake.issuer, session=provider_session(), allow_loopback_http=True,
                                    sleep=kw.pop("sleep", lambda s: None))
    events = []
    m = ChatGPTSessionManager(store, provider, clock=clock,
                              open_browser=kw.pop("browser", fake.browser()),
                              recorder=lambda e, f: events.append((e, f)), **kw)
    m.events = events
    return m


def sign_in(m, **kw):
    return m.complete_sign_in(m.begin_sign_in(**kw), timeout=10)


def doc_of(m):
    with m.store.locked():
        return m.store.read()


def active(m):
    d = doc_of(m)
    return d.profile(d.active_profile_id)


def ready_and_expired(tmp_path, fake, clock, **kw):
    m = make_manager(tmp_path / "chatgpt", fake, clock, **kw)
    sign_in(m)
    clock.advance(3600)
    return m


def run_bg(fn):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:      # noqa: BLE001 -- surfaced to the test
            box["error"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


# ---------------------------------------------------------------------------
# Single refresh / rotation
# ---------------------------------------------------------------------------

def test_single_refresh_rotates_and_publishes_next_generation(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    before = active(m)
    g1 = m.get_valid_access_token()
    assert fake.refresh_count() == 0 and g1.generation == before.generation
    clock.advance(3600)
    g2 = m.get_valid_access_token()
    after = active(m)
    assert fake.refresh_count() == 1
    assert g2.generation == before.generation + 1 == after.generation
    assert g2.access_token != g1.access_token
    assert after.credentials.refresh_token != before.credentials.refresh_token
    assert fake.family_alive_for(after.credentials.refresh_token)
    assert after.pending_rotation is None
    assert m.is_grant_current(g2) and not m.is_grant_current(g1)


def test_refresh_respects_skew_window(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    clock.advance(3600 - 121)
    m.get_valid_access_token()
    assert fake.refresh_count() == 0
    clock.advance(2)
    m.get_valid_access_token()
    assert fake.refresh_count() == 1


@pytest.mark.parametrize("form", ["epoch", "iso"])
def test_earliest_refresh_at_is_honored(tmp_path, fake, clock, form):
    earliest = int(clock()) + 3600 - 30
    fake.code_response_overrides = {"earliest_refresh_at": earliest if form == "epoch" else
                                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(earliest))}
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    clock.advance(3600 - 100)             # inside skew, before earliest: keep the current token
    g = m.get_valid_access_token()
    assert fake.refresh_count() == 0 and g.expires_at > clock()
    clock.advance(80)                     # past earliest, still before expiry: refresh
    m.get_valid_access_token()
    assert fake.refresh_count() == 1


def test_earliest_refresh_at_beyond_expiry_cannot_wedge_the_session(tmp_path, fake, clock):
    fake.code_response_overrides = {"earliest_refresh_at": clock() + 10 * 3600}
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    clock.advance(3600 + 1)
    grant = m.get_valid_access_token()
    assert fake.refresh_count() == 1 and grant.expires_at > clock()


@pytest.mark.parametrize("value", ["9999-12-31T23:59:59+14:00", "0001-01-01T00:00:00-14:00", "not a date", True])
def test_hostile_earliest_refresh_values_are_ignored(value):
    assert oauth._parse_earliest_refresh_at(value) is None or isinstance(
        oauth._parse_earliest_refresh_at(value), float)


def test_refresh_without_id_token_keeps_previous_identity_token(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    previous = active(m).credentials.id_token
    fake.include_id_token_on_refresh = False
    m.get_valid_access_token()
    assert active(m).credentials.id_token == previous


def test_refresh_omitting_scope_keeps_grant_and_narrowed_scope_is_honored(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    m.get_valid_access_token()                  # fake omits scope on refresh
    assert oauth.PLAN_SCOPE in active(m).credentials.scopes
    clock.advance(3600)
    original = oauth.parse_token_response

    def narrowed(data, **kw):
        return original(dict(data, scope=NO_PLAN_SCOPES), **kw)
    oauth.parse_token_response = narrowed
    try:
        with pytest.raises(PlanPermissionNotGranted):
            m.get_valid_access_token()
    finally:
        oauth.parse_token_response = original
    assert m.get_session_state().state is SessionState.CONNECTED_NO_PLAN_PERMISSION


# ---------------------------------------------------------------------------
# Serialization: threads and processes
# ---------------------------------------------------------------------------

def test_two_threads_refresh_once(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_delay = 0.4
    t1, b1 = run_bg(m.get_valid_access_token)
    t2, b2 = run_bg(m.get_valid_access_token)
    t1.join(20)
    t2.join(20)
    assert "error" not in b1 and "error" not in b2, (b1, b2)
    assert fake.refresh_count() == 1
    assert b1["value"].access_token == b2["value"].access_token
    assert fake.family_alive_for(active(m).credentials.refresh_token)


def test_two_managers_in_one_process_refresh_once(tmp_path, fake, clock):
    m1 = ready_and_expired(tmp_path, fake, clock)
    m2 = make_manager(tmp_path / "chatgpt", fake, clock)
    fake.refresh_delay = 0.4
    t1, b1 = run_bg(m1.get_valid_access_token)
    t2, b2 = run_bg(m2.get_valid_access_token)
    t1.join(20)
    t2.join(20)
    assert fake.refresh_count() == 1
    assert b1["value"].access_token == b2["value"].access_token


def _worker_env(tmp_path):
    env = dict(os.environ)
    env.update({
        "LUMINA_TESTING": "1",
        "LUMINA_DATA_DIR": str(tmp_path / "worker_data"),
        "LUMINA_SECRETS_PATH": str(tmp_path / "worker_data" / "credentials.json"),
        "LUMINA_CHATGPT_AUTH_DIR": str(tmp_path / "chatgpt"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def spawn(tmp_path, fake, clock, mode):
    return subprocess.Popen([sys.executable, WORKER, fake.issuer, str(tmp_path / "chatgpt"), mode,
                             repr(clock())], env=_worker_env(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def finish(proc, timeout=60):
    out, err = proc.communicate(timeout=timeout)
    assert "CANARY" not in out and "CANARY" not in err
    return proc.returncode, (json.loads(out.strip().splitlines()[-1]) if out.strip() else None), err


def test_two_processes_refresh_once(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_delay = 0.6
    procs = [spawn(tmp_path, fake, clock, "token") for _ in range(2)]
    results = [finish(p) for p in procs]
    for code, out, err in results:
        assert code == 0 and out and out["ok"], err
    assert fake.refresh_count() == 1
    assert results[0][1]["token_digest"] == results[1][1]["token_digest"]
    assert results[0][1]["generation"] == results[1][1]["generation"] == active(m).generation
    assert fake.family_alive_for(active(m).credentials.refresh_token)


def test_crash_before_request_leaves_session_usable(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    code, _out, _err = finish(spawn(tmp_path, fake, clock, "crash_before_request"))
    assert code == 9
    assert fake.refresh_count() == 0
    grant = m.get_valid_access_token()             # lock was released by the kernel
    assert fake.refresh_count() == 1 and m.is_grant_current(grant)


def test_crash_after_vendor_rotation_before_publish_is_reported_truthfully(tmp_path, fake, clock):
    """The vendor retired the old refresh token and the only copy of the new
    one died with the process. The next refresh presents the old token, the
    vendor reports reuse, and the session lands in REAUTH_REQUIRED with its
    tokens cleared -- never READY on stale credentials, never a silent
    fallback."""
    m = ready_and_expired(tmp_path, fake, clock)
    code, _out, _err = finish(spawn(tmp_path, fake, clock, "crash_after_response"))
    assert code == 9 and fake.refresh_count() == 1
    assert active(m).pending_rotation is None
    with pytest.raises(ReauthRequired) as exc:
        m.get_valid_access_token()
    assert exc.value.error_class in oauth.TERMINAL_REFRESH_CODES
    p = active(m)
    assert p.credentials is None and p.status == store_mod.STATUS_REAUTH_REQUIRED
    assert p.client_id == "oaiapp_test1" and p.subject == "user-sub-A"   # registration kept


def test_crash_after_checkpoint_resumes_without_spending_a_second_refresh(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    code, _out, _err = finish(spawn(tmp_path, fake, clock, "crash_after_checkpoint"))
    assert code == 9 and fake.refresh_count() == 1
    assert m.get_session_state().state is SessionState.REFRESHING
    base = active(m).generation
    grant = m.get_valid_access_token()
    assert fake.refresh_count() == 1                       # resumed, not re-sent
    assert grant.generation == base + 1
    assert active(m).pending_rotation is None
    assert fake.family_alive_for(active(m).credentials.refresh_token)


def test_jwks_outage_during_refresh_keeps_rotation_pending_and_resumable(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.jwks_status = 503
    m.provider._jwks.clear()
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert m.get_session_state().state is SessionState.REFRESHING
    assert fake.refresh_count() == 1
    fake.jwks_status = 200
    restarted = make_manager(tmp_path / "chatgpt", fake, clock)
    grant = restarted.get_valid_access_token()
    assert fake.refresh_count() == 1 and restarted.is_grant_current(grant)


def test_refreshed_identity_for_another_subject_fails_closed(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_id_token_overrides = {"sub": "someone-else"}
    with pytest.raises(AccountMismatch):
        m.get_valid_access_token()
    assert m.get_session_state().state is SessionState.REAUTH_REQUIRED
    assert active(m).credentials is None


def test_refreshed_identity_with_bad_signature_fails_closed(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    original = fake.id_token
    fake.id_token = lambda **kw: original(**{**kw, "key": fake.other_key})
    with pytest.raises(IdentityNotVerified):
        m.get_valid_access_token()
    assert m.get_session_state().state is SessionState.REAUTH_REQUIRED


# ---------------------------------------------------------------------------
# Generation CAS / stale writers
# ---------------------------------------------------------------------------

def test_stale_refresher_cannot_publish_over_a_newer_generation(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    stale_gen = active(m).generation
    m.get_valid_access_token()                         # generation advances
    current = active(m)
    orphan = store_mod.Credentials("CANARY-AT-stale", "CANARY-RT-stale", None, "Bearer",
                                   clock() + 3600, None, current.credentials.scopes, clock())
    assert m._checkpoint_rotation(current.profile_id, stale_gen, orphan) == "stale"
    after = active(m)
    assert after.generation == current.generation
    assert after.credentials.refresh_token == current.credentials.refresh_token
    assert after.pending_rotation is None
    assert "CANARY-RT-stale" in fake.revoked          # the orphan is cleaned up remotely


def test_terminal_decision_about_an_old_generation_never_clears_a_newer_one(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    stale_gen = active(m).generation
    m.get_valid_access_token()
    m._invalidate(active(m).profile_id, stale_gen, "invalid_grant")
    assert m.get_session_state().state is SessionState.READY


def test_old_grant_is_not_current_after_refresh_or_disconnect(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    g = m.get_valid_access_token()
    m.disconnect()
    assert not m.is_grant_current(g)
    with pytest.raises(SignInRequired):
        m.get_valid_access_token()


# ---------------------------------------------------------------------------
# Refresh vs reconnect / account switch / disconnect
# ---------------------------------------------------------------------------

def test_refresh_racing_reconnect_resolves_to_the_reconnect(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    pid = active(m).profile_id
    fake.refresh_gate = threading.Event()
    t, box = run_bg(m.get_valid_access_token)
    assert fake.refresh_entered.wait(10)
    t2, box2 = run_bg(lambda: sign_in(m, profile_id=pid))
    time.sleep(0.3)
    assert "value" not in box2                         # reconnect commit waits for the refresh
    fake.refresh_gate.set()
    t.join(20)
    t2.join(20)
    assert "error" not in box2, box2
    final = active(m)
    refreshed = box["value"]
    assert final.generation == refreshed.generation + 1        # reconnect published after the refresh
    assert not m.is_grant_current(refreshed)
    assert fake.family_alive_for(final.credentials.refresh_token)
    assert fake.refresh_count() == 1


def test_refresh_racing_account_switch_never_returns_the_other_account(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    first = active(m).profile_id
    m._open_browser = fake.browser(subject="sub-B")
    sign_in(m, new_account=True)
    second = active(m).profile_id
    m.select_profile(first)
    clock.advance(3600)
    fake.refresh_gate = threading.Event()
    t, box = run_bg(m.get_valid_access_token)
    assert fake.refresh_entered.wait(10)
    before_b = doc_of(m).profile(second).to_dict()
    m.select_profile(second)
    fake.refresh_gate.set()
    t.join(20)
    assert isinstance(box.get("error"), TemporaryAuthError)
    assert box["error"].error_class == "active_profile_changed"
    assert doc_of(m).profile(second).to_dict() == before_b
    assert doc_of(m).profile(first).pending_rotation is None


def test_disconnect_started_during_refresh_wins_and_revokes_the_newest_token(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    old_rt = active(m).credentials.refresh_token
    fake.refresh_gate = threading.Event()
    t, box = run_bg(m.get_valid_access_token)
    assert fake.refresh_entered.wait(10)
    td, dbox = run_bg(m.disconnect)
    deadline = time.time() + 10
    while active(m).status != store_mod.STATUS_DISCONNECTING and time.time() < deadline:
        time.sleep(0.02)
    assert active(m).status == store_mod.STATUS_DISCONNECTING
    fake.refresh_gate.set()
    t.join(20)
    td.join(20)
    assert isinstance(box.get("error"), TemporaryAuthError)      # generation 9 never became active
    outcome = dbox["value"]
    assert outcome.state is SessionState.DISCONNECTED and outcome.remote_revocation_confirmed is True
    newest = [tok for tok in fake.revoked if tok != old_rt]
    assert fake.revoked[0] == newest[0]                          # newest revoked first
    assert fake.revoked[0] not in (old_rt,)
    assert not fake.family_alive_for(fake.revoked[0])
    p = active(m)
    assert p.credentials is None and p.pending_rotation is None
    raw = open(m.store.sessions_path, "rb").read()
    assert b"CANARY-AT" not in raw and b"CANARY-RT" not in raw


def test_disconnect_after_refresh_commit_revokes_the_committed_generation(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    m.get_valid_access_token()
    committed = active(m).credentials.refresh_token
    outcome = m.disconnect()
    assert fake.revoked[0] == committed
    assert outcome.remote_revocation_confirmed is True


def test_disconnect_that_cannot_wait_out_a_refresh_never_claims_confirmation(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock, disconnect_refresh_wait=0.3)
    sign_in(m)
    held = threading.Event()
    release = threading.Event()

    def hold_refresh_lock():
        with m.store.refresh_locked():
            held.set()
            release.wait(10)
    t, _ = run_bg(hold_refresh_lock)
    assert held.wait(5)
    outcome = m.disconnect()
    release.set()
    t.join(5)
    assert outcome.remote_revocation_confirmed is False
    assert outcome.state is SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED
    assert active(m).credentials is None


def test_second_process_disconnect_and_stale_refresher(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    code, out, err = finish(spawn(tmp_path, fake, clock, "disconnect"))
    assert code == 0 and out["ok"] and out["state"] == "disconnected", err
    # This process still "thinks" it can refresh -- it cannot.
    with pytest.raises(SignInRequired):
        m.get_valid_access_token()
    assert fake.refresh_count() == 0


# ---------------------------------------------------------------------------
# Terminal vs temporary refresh failures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(oauth.TERMINAL_REFRESH_CODES) + ["invalid_client"])
def test_terminal_refresh_failures_require_reauth_and_keep_the_registration(tmp_path, fake, clock, code):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_faults.append((400, {"error": code}))
    with pytest.raises(ReauthRequired):
        m.get_valid_access_token()
    p = active(m)
    assert p.status == store_mod.STATUS_REAUTH_REQUIRED and p.credentials is None
    assert p.client_id == "oaiapp_test1"
    assert m.get_session_state().state is SessionState.REAUTH_REQUIRED
    seen = []
    m._open_browser = fake.browser(record=seen)
    assert sign_in(m, profile_id=p.profile_id).state is SessionState.READY
    if code == "invalid_client":
        # OpenAI rejected the client itself: reconnect registers a fresh one
        # for the SAME account and rebinds this profile (no duplicate).
        assert seen[0]["client_id"] == "dynamic_agent_client"
        assert [q.client_id for q in doc_of(m).profiles] == ["oaiapp_test2"]
        assert active(m).subject == "user-sub-A" and not active(m).registration_invalid
    else:
        # Reauthorization uses the saved issued client ID.
        assert seen[0]["client_id"] == "oaiapp_test1"
        assert len(doc_of(m).profiles) == 1


@pytest.mark.parametrize("fault", [(500, {"error": "server_error"}), (503, {}), (429, {}),
                                   (400, {"error": "something_new"}), (401, {"detail": "x"})])
def test_temporary_or_unknown_refresh_failures_never_destroy_credentials(tmp_path, fake, clock, fault):
    m = ready_and_expired(tmp_path, fake, clock)
    before = active(m)
    fake.refresh_faults.append(fault)
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    after = active(m)
    assert after.credentials.refresh_token == before.credentials.refresh_token
    assert after.generation == before.generation
    assert m.get_session_state().state is SessionState.READY
    m.get_valid_access_token()                  # and it recovers
    assert fake.refresh_count() == 2


def test_network_failure_never_destroys_credentials(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    m.provider.endpoints()
    fake.close()                                 # provider unreachable
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert m.get_session_state().state is SessionState.READY
    assert active(m).credentials is not None


def test_read_timeout_is_temporary(tmp_path, fake, clock, monkeypatch):
    m = ready_and_expired(tmp_path, fake, clock)
    monkeypatch.setattr(oauth, "HTTP_READ_TIMEOUT", 0.2)
    fake.refresh_delay = 1.0
    with pytest.raises(TemporaryAuthError) as exc:
        m.get_valid_access_token()
    assert exc.value.error_class == "network_timeout"
    assert active(m).credentials is not None


# ---------------------------------------------------------------------------
# Disconnect / revocation
# ---------------------------------------------------------------------------

def test_disconnect_success_clears_tokens_keeps_registration(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    rt = active(m).credentials.refresh_token
    outcome = m.disconnect()
    assert outcome.state is SessionState.DISCONNECTED and outcome.remote_revocation_confirmed is True
    assert fake.revoked == [rt]
    p = active(m)
    assert p.credentials is None and p.client_id == "oaiapp_test1" and p.subject == "user-sub-A"
    host = json.loads(open(m.store.host_path).read())["ext_agent_host_id"]
    assert host.startswith("urn:uuid:")
    assert ("chatgpt.disconnect.completed" in [e for e, _ in m.events])


def test_already_invalid_token_revocation_is_vendor_success(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    rt = active(m).credentials.refresh_token
    fake.families[fake.refresh_index[rt]]["alive"] = False      # e.g. disconnected in ChatGPT settings
    assert m.disconnect().remote_revocation_confirmed is True


def test_revocation_retries_5xx_with_bounded_backoff(tmp_path, fake, clock):
    sleeps = []
    m = make_manager(tmp_path / "chatgpt", fake, clock, sleep=sleeps.append)
    sign_in(m)
    fake.revoke_faults = [503, 502]
    outcome = m.disconnect()
    assert outcome.remote_revocation_confirmed is True
    assert len(fake.revoked) == 3 and sleeps == [0.5, 1.0]


@pytest.mark.parametrize("faults,expected_calls", [([503, 503, 503], 3), ([400], 1)])
def test_unconfirmed_revocation_is_reported_and_tokens_still_cleared(tmp_path, fake, clock, faults, expected_calls):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    fake.revoke_faults = list(faults)
    outcome = m.disconnect()
    assert outcome.remote_revocation_confirmed is False
    assert outcome.state is SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED
    assert len(fake.revoked) == expected_calls
    assert active(m).credentials is None
    status = m.get_session_state()
    assert status.error_category.value == "remote_disconnect_unconfirmed"
    assert "chatgpt.disconnect.remote_unconfirmed" in [e for e, _ in m.events]


def test_network_failure_during_disconnect_is_local_only(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    fake.close()
    outcome = m.disconnect()
    assert outcome.state is SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED
    assert outcome.remote_revocation_confirmed is False


def test_local_only_disconnect_survives_restart_without_resurrection(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    fake.revoke_faults = [400]
    m.disconnect()
    restarted = make_manager(tmp_path / "chatgpt", fake, clock)
    assert restarted.get_session_state().state is SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED
    with pytest.raises(SignInRequired):
        restarted.get_valid_access_token()
    raw = open(restarted.store.sessions_path, "rb").read()
    assert b"CANARY-AT" not in raw and b"CANARY-RT" not in raw and b"CANARY-IDT" not in raw


def test_interrupted_disconnect_is_unusable_and_resumable(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    with m.store.locked():
        doc = m.store.read()
        p = doc.profile(doc.active_profile_id)
        p.status = store_mod.STATUS_DISCONNECTING           # crash right after step 1
        p.generation += 1
        m.store.write(doc)
    restarted = make_manager(tmp_path / "chatgpt", fake, clock)
    assert restarted.get_session_state().state is SessionState.DISCONNECTING
    with pytest.raises(SignInRequired):
        restarted.get_valid_access_token()
    outcome = restarted.disconnect()
    assert outcome.state is SessionState.DISCONNECTED and outcome.remote_revocation_confirmed is True


def test_reconnect_after_disconnect_reuses_the_registration(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=fake.browser(record=seen))
    sign_in(m)
    pid = active(m).profile_id
    m.disconnect()
    assert sign_in(m, profile_id=pid).state is SessionState.READY
    assert seen[1]["client_id"] == "oaiapp_test1"
    assert len(doc_of(m).profiles) == 1


def test_sign_in_refused_while_disconnect_in_progress(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    pid = active(m).profile_id
    with m.store.locked():
        doc = m.store.read()
        doc.profile(pid).status = store_mod.STATUS_DISCONNECTING
        m.store.write(doc)
    from core.chatgpt_auth.session import SessionBusy
    with pytest.raises(SessionBusy):
        m.begin_sign_in(profile_id=pid)
