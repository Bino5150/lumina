"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- regression tests for the Opus Pass A
security/concurrency review (HIGH 1, MEDIUM 2-9, and three LOW notes).
Each test reproduces the reviewed failure against the pre-fix code and
pins the repaired behavior.
"""
import threading
import time
import urllib.parse

import pytest
import requests

from core.chatgpt_auth import oauth
from core.chatgpt_auth.session import (
    AuthorizationCancelled, ReauthRequired, SessionState, SessionStorageError, TemporaryAuthError,
    IdentityNotVerified, AccountMismatch,
)
from chatgpt_auth_fakes import FakeClock, FakeOpenAIAuth
from test_chatgpt_auth_01c_concurrency import (
    active, doc_of, make_manager, ready_and_expired, run_bg, sign_in,
)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    f = FakeOpenAIAuth(clock=clock)
    yield f
    f.close()


def jwks_outage_mid_refresh(m, fake):
    fake.jwks_status = 503
    m.provider._jwks.clear()
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert m.get_session_state().state is SessionState.REFRESHING


# --- HIGH 1: disconnect while a promotable rotation exists -----------------

def test_disconnect_works_while_refreshing_and_revokes_the_pending_token(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    jwks_outage_mid_refresh(m, fake)
    pending_rt = active(m).pending_rotation.credentials.refresh_token
    outcome = m.disconnect()
    assert outcome.state is SessionState.DISCONNECTED and outcome.remote_revocation_confirmed is True
    assert fake.revoked[0] == pending_rt
    p = active(m)
    assert p.credentials is None and p.pending_rotation is None


def test_disconnect_racing_identity_verification_wins(tmp_path, fake, clock):
    """Disconnect lands between the rotation checkpoint (step 4) and the
    publish (step 6): generation G+1 must never become active."""
    m = ready_and_expired(tmp_path, fake, clock)
    entered, release = threading.Event(), threading.Event()
    real_verify = m.provider.verify_id_token

    def slow_verify(*a, **kw):
        entered.set()
        assert release.wait(10)
        return real_verify(*a, **kw)
    m.provider.verify_id_token = slow_verify
    t, box = run_bg(m.get_valid_access_token)
    assert entered.wait(10)
    pending_rt = active(m).pending_rotation.credentials.refresh_token
    td, dbox = run_bg(m.disconnect)
    deadline = time.time() + 10
    while active(m).status != "disconnecting" and time.time() < deadline:
        time.sleep(0.02)
    assert active(m).status == "disconnecting"
    assert active(m).pending_rotation.promotable is False
    release.set()
    t.join(20)
    td.join(20)
    assert isinstance(box.get("error"), TemporaryAuthError)
    assert dbox["value"].state is SessionState.DISCONNECTED
    assert pending_rt in fake.revoked
    assert not fake.family_alive_for(pending_rt)
    assert active(m).credentials is None


# --- MEDIUM 2: never return an already-expired token -----------------------

def test_resumed_rotation_that_expired_meanwhile_is_renewed_before_use(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    jwks_outage_mid_refresh(m, fake)
    clock.advance(2 * 3600)                    # the outage outlasted the rotated access token
    fake.jwks_status = 200
    grant = m.get_valid_access_token()
    assert grant.expires_at > clock()
    assert fake.refresh_count() == 2
    assert fake.family_alive_for(active(m).credentials.refresh_token)


# --- MEDIUM 3: environmental verification failures keep the rotation -------

@pytest.mark.parametrize("override", [{"iat_ahead": 600}, {"kid": "not-yet-published"}])
def test_clock_lag_or_key_lag_on_refresh_keeps_the_session(tmp_path, fake, clock, override):
    m = ready_and_expired(tmp_path, fake, clock)
    if "iat_ahead" in override:
        fake.refresh_id_token_overrides = {"iat": int(clock()) + override["iat_ahead"]}
    else:
        fake.refresh_id_token_overrides = {"kid": override["kid"]}
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    p = active(m)
    assert m.get_session_state().state is SessionState.REFRESHING
    assert p.pending_rotation is not None and fake.family_alive_for(p.pending_rotation.credentials.refresh_token)
    fake.refresh_id_token_overrides = {}
    # A later attempt re-verifies the SAME rotation once the clock/keys agree.
    if "iat_ahead" in override:
        clock.advance(override["iat_ahead"])
        p2 = active(m)
        with m.store.locked():
            doc = m.store.read()
            doc.profile(p2.profile_id).pending_rotation.credentials.received_at = clock()
            m.store.write(doc)
    else:
        fake.kid = "not-yet-published"
    grant = m.get_valid_access_token()
    assert fake.refresh_count() == 1 and m.is_grant_current(grant)


@pytest.mark.parametrize("kind", ["bad_signature", "other_subject"])
def test_discarded_rotation_on_real_identity_failure_is_revoked(tmp_path, fake, clock, kind):
    m = ready_and_expired(tmp_path, fake, clock)
    if kind == "bad_signature":
        original = fake.id_token
        fake.id_token = lambda **kw: original(**{**kw, "key": fake.other_key})
        expected = IdentityNotVerified
    else:
        fake.refresh_id_token_overrides = {"subject": "someone-else"}
        expected = AccountMismatch
    with pytest.raises(expected):
        m.get_valid_access_token()
    assert m.get_session_state().state is SessionState.REAUTH_REQUIRED
    newest = fake.revoked[-1]
    assert newest.startswith("CANARY-RT-") and not fake.family_alive_for(newest)


# --- MEDIUM 4: malformed 2xx refresh never keeps the retired token ---------

@pytest.mark.parametrize("override", [{"expires_in": "3600"}, {"expires_in": 90 * 86400}, {"token_type": "mac"}])
def test_malformed_successful_refresh_goes_to_reauth_and_revokes_the_successor(tmp_path, fake, clock, override):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_response_overrides = override
    with pytest.raises(ReauthRequired):
        m.get_valid_access_token()
    p = active(m)
    assert p.credentials is None and p.status == "reauth_required"
    assert fake.revoked and not fake.family_alive_for(fake.revoked[-1])
    fake.refresh_response_overrides = {}
    with pytest.raises(ReauthRequired):
        m.get_valid_access_token()
    assert fake.refresh_count() == 1                  # the retired token is never re-sent


def test_checkpoint_write_failure_revokes_the_unpersisted_successor(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)

    def disk_full(*a, **kw):
        raise OSError(28, "No space left on device")
    m._checkpoint_rotation = disk_full
    with pytest.raises(SessionStorageError):
        m.get_valid_access_token()
    assert fake.revoked and not fake.family_alive_for(fake.revoked[-1])


# --- MEDIUM 5: a lost refresh response forbids "confirmed" -----------------

def test_lost_refresh_response_never_yields_confirmed_disconnect(tmp_path, fake, clock, monkeypatch):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.revoke_cascades = False
    monkeypatch.setattr(oauth, "HTTP_READ_TIMEOUT", 0.2)
    fake.refresh_delay = 0.8                          # the vendor rotates; we never see the answer
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert active(m).refresh_outcome_unknown is True
    time.sleep(1.0)                                   # let the fake finish rotating
    outcome = m.disconnect()
    assert outcome.remote_revocation_confirmed is False
    assert outcome.state is SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED


def test_answered_refresh_failure_clears_the_uncertainty(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_faults.append((400, {"error": "invalid_request"}))     # definite, non-terminal
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert active(m).refresh_outcome_unknown is False
    assert m.disconnect().remote_revocation_confirmed is True


# --- MEDIUM 6: cancel during the code exchange commits nothing --------------

def test_cancel_during_code_exchange_commits_nothing_and_revokes(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    real_exchange = m.provider.exchange_code

    def exchange_then_cancel(**kw):
        result = real_exchange(**kw)
        m.cancel_sign_in()
        return result
    m.provider.exchange_code = exchange_then_cancel
    with pytest.raises(AuthorizationCancelled):
        sign_in(m)
    assert doc_of(m).profiles == []
    assert m.get_session_state().state is SessionState.DISCONNECTED
    assert fake.revoked and not fake.family_alive_for(fake.revoked[-1])


# --- MEDIUM 7: state never in browser argv; forged registrations not kept --

def test_browser_is_launched_with_a_one_shot_local_start_url_only(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=fake.browser(record=seen))
    sign_in(m)
    [launched] = fake.launched_urls
    parts = urllib.parse.urlsplit(launched)
    assert parts.hostname == "127.0.0.1" and parts.path.startswith("/auth/start/") and not parts.query
    for secret in (seen[0]["state"], seen[0]["nonce"], seen[0]["code_challenge"], seen[0]["ext_agent_host_id"]):
        assert secret not in launched


def test_start_url_answers_exactly_once(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=lambda url: fake.launched_urls.append(url))
    m.begin_sign_in()
    [start] = fake.launched_urls
    s = requests.Session()
    s.trust_env = False
    first = s.get(start, allow_redirects=False, timeout=5)
    second = s.get(start, allow_redirects=False, timeout=5)
    assert first.status_code == 302 and first.headers["Location"].startswith(fake.issuer)
    assert second.status_code == 404
    m.cancel_sign_in()


def test_forged_callback_cannot_poison_future_sign_ins(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=fake.browser(
        mutate_callback=lambda cb: {**cb, "code": "junk", "client_id": "oaiapp_ATTACKER"}))
    with pytest.raises(Exception):
        sign_in(m)
    assert doc_of(m).pending_registrations == []
    seen = []
    m._open_browser = fake.browser(record=seen)
    sign_in(m)
    assert seen[0]["client_id"] == "dynamic_agent_client"


def test_pending_registrations_expire(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    real_exchange = m.provider.exchange_code
    m.provider.exchange_code = lambda **kw: real_exchange(**{**kw, "verifier": oauth.new_pkce_verifier()})
    with pytest.raises(AuthorizationCancelled):
        sign_in(m)
    assert len(doc_of(m).pending_registrations) == 1
    m.provider.exchange_code = real_exchange
    clock.advance(16 * 60)
    seen = []
    m._open_browser = fake.browser(record=seen)
    sign_in(m)
    assert seen[0]["client_id"] == "dynamic_agent_client"
    assert doc_of(m).pending_registrations == []


# --- MEDIUM 8: a rotation that lost to a timed-out disconnect is revoked ---

def test_rotation_landing_after_a_timed_out_disconnect_is_revoked(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock, disconnect_refresh_wait=0.3)
    fake.revoke_cascades = False
    fake.refresh_respond_gate = threading.Event()
    t, box = run_bg(m.get_valid_access_token)
    assert fake.refresh_rotated.wait(10)              # vendor rotated; our response is delayed
    outcome = m.disconnect()                          # gives up waiting, revokes only the retired token
    assert outcome.remote_revocation_confirmed is False
    family = next(iter(fake.families.values()))
    assert family["alive"]                            # the successor is still live at this point
    fake.refresh_respond_gate.set()
    t.join(20)
    assert isinstance(box.get("error"), TemporaryAuthError)
    assert family["current"] in fake.revoked and not family["alive"]
    assert active(m).credentials is None and active(m).pending_rotation is None


# --- MEDIUM 9: invalid_client is a registration problem -------------------
# (covered in test_chatgpt_auth_01c_concurrency.py::
#  test_terminal_refresh_failures_require_reauth_and_keep_the_registration[invalid_client])


# --- LOW: replaced session revoked; slow local connection can't block ------

def test_reconnect_never_revokes_the_session_it_replaces(tmp_path, fake, clock):
    """Opus Pass B (HIGH, conditional): under the same issued client and
    account the replaced tokens may share the new session's OAuth grant, and
    RFC 7009 lets a revocation end the whole grant. Never revoke them."""
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    old_rt = active(m).credentials.refresh_token
    sign_in(m, profile_id=active(m).profile_id)
    assert fake.revoked == []
    assert fake.family_alive_for(active(m).credentials.refresh_token)


def test_a_stalled_local_connection_cannot_block_the_callback(tmp_path, fake, clock):
    """A local process that opens the listener's port and never finishes its
    request must not delay the genuine callback (threaded listener)."""
    import socket
    seen, captured = [], {}
    m = make_manager(tmp_path / "chatgpt", fake, clock,
                     browser=fake.browser(deliver=False, record=seen,
                                          mutate_callback=lambda cb: captured.update(cb) or cb))
    pending = m.begin_sign_in()
    redirect = seen[0]["redirect_uri"]
    port = urllib.parse.urlsplit(redirect).port
    stall = socket.create_connection(("127.0.0.1", port))
    stall.sendall(b"GET /auth/callback HTTP/1.1\r\n")       # never finishes its headers
    try:
        s = requests.Session()
        s.trust_env = False
        started = time.monotonic()
        response = s.get(redirect + "?" + urllib.parse.urlencode(captured), timeout=3)
        assert response.status_code == 200 and time.monotonic() - started < 3
        assert m.complete_sign_in(pending, timeout=5).state is SessionState.READY
    finally:
        stall.close()


# --- Opus Pass B ------------------------------------------------------------

def _start_and_location(url):
    s = requests.Session()
    s.trust_env = False
    return s.get(url, allow_redirects=False, timeout=5)


def test_raced_start_url_marks_the_attempt_compromised_and_keeps_nothing(tmp_path, fake, clock):
    """An attacker fetches the one-shot start URL first (learning the state),
    the real browser's fetch is the second hit, then a forged callback with
    the attacker's real issued client and a junk code arrives."""
    fake.clients["oaiapp_ATTACKER"] = "attacker-sub"
    launched = []
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=lambda url: launched.append(url))
    pending = m.begin_sign_in()
    attacker = _start_and_location(launched[0])
    assert attacker.status_code == 302
    assert _start_and_location(launched[0]).status_code == 404      # the real browser, too late
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(attacker.headers["Location"]).query))
    forged = q["redirect_uri"] + "?" + urllib.parse.urlencode(
        {"state": q["state"], "code": "junk", "client_id": "oaiapp_ATTACKER"})
    assert _start_and_location(forged).status_code == 200
    with pytest.raises(IdentityNotVerified) as exc:
        m.complete_sign_in(pending, timeout=5)
    assert exc.value.error_class == "attempt_compromised"
    assert doc_of(m).pending_registrations == [] and doc_of(m).profiles == []
    assert fake.grants == []                                         # never even exchanged


def test_compromise_detected_during_the_exchange_drops_the_saved_registration(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    real_exchange = m.provider.exchange_code

    def exchange_with_late_second_hit(**kw):
        _start_and_location(fake.launched_urls[0])                   # someone replays the start URL now
        raise oauth.ProviderRejected("invalid_grant", 400)
    m.provider.exchange_code = exchange_with_late_second_hit
    with pytest.raises(AuthorizationCancelled):
        sign_in(m)
    assert doc_of(m).pending_registrations == []


def test_gateway_5xx_on_refresh_keeps_the_outcome_unknown(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_faults.append((504, {}))
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert active(m).refresh_outcome_unknown is True
    assert m.disconnect().remote_revocation_confirmed is False


def test_rate_limited_refresh_settles_the_outcome(tmp_path, fake, clock):
    m = ready_and_expired(tmp_path, fake, clock)
    fake.refresh_faults.append((429, {}))
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert active(m).refresh_outcome_unknown is False


@pytest.mark.parametrize("where", ["exchange", "callback"])
def test_rejected_client_during_reconnect_marks_the_registration_invalid(tmp_path, fake, clock, where):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    pid = active(m).profile_id
    m.disconnect()
    if where == "exchange":
        def rejected(**kw):
            raise oauth.ProviderRejected("invalid_client", 400)
        m.provider.exchange_code = rejected
    else:
        m._open_browser = fake.browser(mutate_callback=lambda cb: {"state": cb["state"],
                                                                   "error": "unauthorized_client"})
    with pytest.raises(Exception):
        sign_in(m, profile_id=pid)
    assert active(m).registration_invalid is True
    m.provider.exchange_code = oauth.ProviderClient.exchange_code.__get__(m.provider)
    seen = []
    m._open_browser = fake.browser(record=seen)
    assert sign_in(m, profile_id=pid).state is SessionState.READY
    assert seen[0]["client_id"] == "dynamic_agent_client"
    assert len(doc_of(m).profiles) == 1 and active(m).client_id == "oaiapp_test2"


def test_account_mismatch_revokes_the_other_accounts_new_session_only(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    a_rt = active(m).credentials.refresh_token
    m._open_browser = fake.browser(subject="user-sub-B")
    with pytest.raises(AccountMismatch):
        sign_in(m, profile_id=active(m).profile_id)
    assert len(fake.revoked) == 1 and fake.revoked[0] != a_rt
    assert not fake.family_alive_for(fake.revoked[0])
    assert fake.family_alive_for(a_rt)


def test_failed_reconnect_of_a_live_account_never_revokes_its_grant(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    a_rt = active(m).credentials.refresh_token
    real_exchange = m.provider.exchange_code

    def exchange_then_cancel(**kw):
        result = real_exchange(**kw)
        m.cancel_sign_in()
        return result
    m.provider.exchange_code = exchange_then_cancel
    with pytest.raises(AuthorizationCancelled):
        sign_in(m, profile_id=active(m).profile_id)
    assert fake.revoked == []
    assert fake.family_alive_for(a_rt) and active(m).credentials.refresh_token == a_rt


def test_listener_handler_threads_are_bounded(tmp_path, fake, clock):
    import socket
    seen = []
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=fake.browser(deliver=False, record=seen))
    m.begin_sign_in()
    port = urllib.parse.urlsplit(seen[0]["redirect_uri"]).port
    before = threading.active_count()
    socks = [socket.create_connection(("127.0.0.1", port)) for _ in range(40)]
    try:
        for sk in socks:
            sk.sendall(b"GET /auth/callback HTTP/1.1\r\n")
        time.sleep(0.5)
        assert threading.active_count() - before <= 8
    finally:
        for sk in socks:
            sk.close()
        m.cancel_sign_in()


def test_directory_fsync_failure_after_replace_does_not_revoke_a_saved_rotation(tmp_path, fake, clock, monkeypatch):
    import os
    import stat as stat_mod
    from core.chatgpt_auth import store as store_mod
    m = ready_and_expired(tmp_path, fake, clock)
    real_fsync = os.fsync

    def flaky_fsync(fd):
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "I/O error")
        return real_fsync(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky_fsync)
    grant = m.get_valid_access_token()
    assert fake.revoked == []
    assert m.is_grant_current(grant) and fake.family_alive_for(active(m).credentials.refresh_token)
