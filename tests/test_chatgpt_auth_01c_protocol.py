"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- Sign in with ChatGPT protocol fixtures:
PKCE/state/nonce binding, loopback callback discipline, issued-client
registration, ID-token verification, plan permission vs identity, and
account/registration separation.

Everything runs against tests/chatgpt_auth_fakes.FakeOpenAIAuth on
127.0.0.1 (real HTTP, test-only RSA key) with a scratch session store. No
live OAuth, no production credentials, no inference endpoint exists in the
fake at all.
"""
import json
import os
import re
import socket
import urllib.parse

import pytest
import requests

from core.chatgpt_auth import oauth
from core.chatgpt_auth.callback import LoopbackCallback
from core.chatgpt_auth.session import (
    AccountMismatch, AuthorizationCancelled, BrowserUnavailable, ChatGPTSessionManager,
    IdentityNotVerified, PlanPermission, PlanPermissionNotGranted, SessionState,
    TemporaryAuthError,
)
from core.chatgpt_auth.store import SessionStore
from chatgpt_auth_fakes import (
    DROP, NO_PLAN_SCOPES, FakeClock, FakeOpenAIAuth, provider_session,
)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    f = FakeOpenAIAuth(clock=clock)
    yield f
    f.close()


def make_manager(tmp_path, fake, clock, browser=None, **kw):
    store = SessionStore(str(tmp_path / "chatgpt"), lock_timeout=10)
    provider = oauth.ProviderClient(fake.issuer, session=provider_session(), allow_loopback_http=True)
    events = []
    m = ChatGPTSessionManager(store, provider, clock=clock,
                              open_browser=browser or fake.browser(),
                              recorder=lambda e, f: events.append((e, f)), **kw)
    m.events = events
    return m


def sign_in(m, **kw):
    pending = m.begin_sign_in(**kw)
    return m.complete_sign_in(pending, timeout=10)


def read_doc(m):
    with m.store.locked():
        return m.store.read()


def _get(url, **headers):
    s = requests.Session()
    s.trust_env = False
    return s.get(url, headers=headers, timeout=5)


# ---------------------------------------------------------------------------
# PKCE / randomness
# ---------------------------------------------------------------------------

def test_pkce_challenge_matches_rfc7636_appendix_b_vector():
    assert oauth.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_state_nonce_are_fresh_csprng_values():
    verifiers = {oauth.new_pkce_verifier() for _ in range(50)}
    states = {oauth.new_state() for _ in range(50)}
    nonces = {oauth.new_nonce() for _ in range(50)}
    assert len(verifiers) == len(states) == len(nonces) == 50
    for v in verifiers:
        assert 43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", v)


# ---------------------------------------------------------------------------
# First registration / reconnect
# ---------------------------------------------------------------------------

def test_first_registration_uses_bootstrap_flow_and_persists_issued_client(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(record=seen))
    status = sign_in(m)

    assert status.state is SessionState.READY
    assert status.plan_permission is PlanPermission.GRANTED
    q = seen[0]
    assert q["client_id"] == "dynamic_agent_client"
    assert q["agent_name_hint"] == "Lumina"
    assert re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", q["ext_agent_host_id"])
    assert q["scope"].split() == ["openid", "profile", "email", "offline_access",
                                  "resource.invoke", "chatgpt.tokens.use.direct"]
    assert q["resource"] == "https://api.openai.com/v1"
    assert q["code_challenge_method"] == "S256"
    assert urllib.parse.urlsplit(q["redirect_uri"]).hostname == "127.0.0.1"
    assert urllib.parse.urlsplit(q["redirect_uri"]).path == "/auth/callback"
    assert "id_token_hint" not in q and "login_hint" not in q and "prompt" not in q

    doc = read_doc(m)
    [profile] = doc.profiles
    assert profile.client_id == "oaiapp_test1"           # issued, never the bootstrap ID
    assert profile.subject == "user-sub-A"
    assert profile.issuer == fake.issuer
    assert doc.pending_registrations == []
    assert doc.active_profile_id == profile.profile_id
    # The token endpoint never saw the bootstrap client ID.
    assert [c for _, c in fake.grants] == ["oaiapp_test1"]


def test_reconnect_reuses_issued_client_and_host_with_fresh_state_nonce_pkce(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(record=seen))
    sign_in(m)
    pid = read_doc(m).active_profile_id
    gen_before = read_doc(m).profile(pid).generation
    sign_in(m, profile_id=pid)

    first, second = seen
    assert second["client_id"] == "oaiapp_test1"
    assert "agent_name_hint" not in second
    assert second["ext_agent_host_id"] == first["ext_agent_host_id"]
    for key in ("state", "nonce", "code_challenge"):
        assert first[key] != second[key]
    doc = read_doc(m)
    assert len(doc.profiles) == 1 and doc.profile(pid).generation == gen_before + 1


def test_host_id_is_stable_across_manager_restarts(tmp_path, fake, clock):
    seen = []
    sign_in(make_manager(tmp_path, fake, clock, browser=fake.browser(record=seen)))
    sign_in(make_manager(tmp_path, fake, clock, browser=fake.browser(record=seen, subject="user-sub-B")),
            new_account=True)
    assert seen[0]["ext_agent_host_id"] == seen[1]["ext_agent_host_id"]


def test_consent_denied_stops_without_code_exchange(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(consent=False))
    with pytest.raises(AuthorizationCancelled) as exc:
        sign_in(m)
    assert exc.value.error_class == "access_denied"
    assert fake.grants == []
    assert read_doc(m).profiles == []
    assert m.get_session_state().state is SessionState.DISCONNECTED


def test_missing_plan_scope_is_connected_but_not_ready(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(scopes=NO_PLAN_SCOPES))
    status = sign_in(m)
    assert status.state is SessionState.CONNECTED_NO_PLAN_PERMISSION
    assert status.plan_permission is PlanPermission.NOT_GRANTED
    with pytest.raises(PlanPermissionNotGranted):
        m.get_valid_access_token()


def test_callback_scope_is_not_trusted_token_response_scope_decides(tmp_path, fake, clock):
    """The callback can claim the plan scope; only the token response's
    granted scopes count."""
    fake.code_scope_override = NO_PLAN_SCOPES
    m = make_manager(tmp_path, fake, clock)
    assert sign_in(m).state is SessionState.CONNECTED_NO_PLAN_PERMISSION


def test_reenable_plan_permission_requests_consent_with_saved_client(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(scopes=NO_PLAN_SCOPES, record=seen))
    sign_in(m)
    pid = read_doc(m).active_profile_id
    m._open_browser = fake.browser(record=seen)
    status = sign_in(m, profile_id=pid, request_plan_consent=True)
    assert seen[1]["prompt"] == "consent" and seen[1]["client_id"] == "oaiapp_test1"
    assert status.state is SessionState.READY


# ---------------------------------------------------------------------------
# Callback discipline
# ---------------------------------------------------------------------------

def _two_step_browser(fake, first_mutation):
    """Delivers a bad callback first (which must NOT consume the attempt),
    then the genuine one."""
    seen = []
    real = fake.browser(deliver=False, record=seen, mutate_callback=lambda cb: captured.update(cb) or cb)
    captured = {}
    responses = []

    def open_browser(url):
        real(url)
        redirect = seen[-1]["redirect_uri"]
        bad_url, headers = first_mutation(redirect, dict(captured))
        responses.append(_get(bad_url, **headers).status_code)
        responses.append(_get(redirect + "?" + urllib.parse.urlencode(captured)).status_code)
        return True
    return open_browser, responses


@pytest.mark.parametrize("name,mutation,status", [
    ("wrong_state", lambda r, cb: (r + "?" + urllib.parse.urlencode({**cb, "state": "forged"}), {}), 400),
    ("missing_state", lambda r, cb: (r + "?" + urllib.parse.urlencode(
        {k: v for k, v in cb.items() if k != "state"}), {}), 400),
    ("duplicate_state", lambda r, cb: (r + "?" + urllib.parse.urlencode(cb) + "&state=" + cb["state"], {}), 400),
    ("wrong_path", lambda r, cb: (r.replace("/auth/callback", "/callback") + "?"
                                  + urllib.parse.urlencode(cb), {}), 404),
    ("wrong_host_header", lambda r, cb: (r + "?" + urllib.parse.urlencode(cb),
                                         {"Host": "localhost:" + r.split(":")[2].split("/")[0]}), 404),
])
def test_bad_callbacks_are_rejected_without_consuming_the_attempt(tmp_path, fake, clock, name, mutation, status):
    browser, responses = _two_step_browser(fake, mutation)
    m = make_manager(tmp_path, fake, clock, browser=browser)
    result = sign_in(m)
    assert responses == [status, 200], name
    assert result.state is SessionState.READY


def test_duplicate_callback_after_completion_is_refused(tmp_path, fake, clock):
    replay = {}

    def capture(cb):
        replay.update(cb)
        return cb
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(mutate_callback=capture, record=seen))
    sign_in(m)
    url = seen[0]["redirect_uri"] + "?" + urllib.parse.urlencode(replay)
    with pytest.raises(requests.ConnectionError):
        _get(url)                       # listener closed: the port is released
    assert len(read_doc(m).profiles) == 1


def test_second_callback_on_a_still_open_listener_gets_404():
    listener = LoopbackCallback(state="s1", expected_client_id="oaiapp_x")
    try:
        base = listener.redirect_uri
        assert _get(base + "?state=s1&code=c1").status_code == 200
        assert _get(base + "?state=s1&code=c2").status_code == 404
        assert listener.wait(1).code == "c1"
    finally:
        listener.close()


def test_listener_binds_loopback_only_and_releases_its_port():
    listener = LoopbackCallback(state="s", expected_client_id=None)
    host, port = listener._server.server_address
    assert host == "127.0.0.1"
    assert listener.redirect_uri == f"http://127.0.0.1:{port}/auth/callback"
    listener.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_stale_attempt_callback_cannot_complete_a_new_attempt(tmp_path, fake, clock):
    seen = []
    old = {}
    m = make_manager(tmp_path, fake, clock,
                     browser=fake.browser(deliver=False, record=seen, mutate_callback=lambda cb: old.update(cb) or cb))
    m.begin_sign_in()                       # attempt 1, never completed
    old_redirect = seen[0]["redirect_uri"]
    m._open_browser = fake.browser(record=seen)
    pending = m.begin_sign_in()             # attempt 2 replaces it
    # Attempt 1's listener is gone; its state means nothing to attempt 2.
    with pytest.raises(requests.ConnectionError):
        _get(old_redirect + "?" + urllib.parse.urlencode(old))
    status = m.complete_sign_in(pending, timeout=10)
    assert status.state is SessionState.READY
    assert seen[0]["state"] != seen[1]["state"]


def test_expired_attempt_never_accepts_a_late_callback(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock)
    real_browser = fake.browser()

    def late(url):
        clock.advance(m._attempt_ttl + 1)
        return real_browser(url)
    m._open_browser = late
    with pytest.raises(AuthorizationCancelled) as exc:
        sign_in(m)
    assert exc.value.error_class == "attempt_expired"
    assert [g for g, _ in fake.grants] == []
    assert read_doc(m).profiles == []


def test_cancel_during_authorizing_terminates_listener(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(deliver=False, record=seen))
    pending = m.begin_sign_in()
    assert m.get_session_state().state is SessionState.AUTHORIZING
    m.cancel_sign_in()
    with pytest.raises(AuthorizationCancelled):
        m.complete_sign_in(pending, timeout=5)
    port = int(urllib.parse.urlsplit(seen[0]["redirect_uri"]).port)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert m.get_session_state().state is SessionState.DISCONNECTED


def test_restart_with_pending_authorization_persists_no_transient_material(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(deliver=False, record=seen))
    m.begin_sign_in()
    restarted = make_manager(tmp_path, fake, clock)
    assert restarted.get_session_state().state is SessionState.DISCONNECTED
    blob = b""
    for name in os.listdir(m.store.directory):
        with open(os.path.join(m.store.directory, name), "rb") as fh:
            blob += fh.read()
    q = seen[0]
    for secret in (q["state"], q["nonce"], q["code_challenge"]):
        assert secret.encode() not in blob
    m.cancel_sign_in()


def test_browser_unavailable_releases_the_listener(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=lambda url: False)
    with pytest.raises(BrowserUnavailable):
        m.begin_sign_in()
    assert m._attempt is None


# ---------------------------------------------------------------------------
# Issued client registration
# ---------------------------------------------------------------------------

def test_new_registration_without_issued_client_id_is_incomplete(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(
        mutate_callback=lambda cb: {k: v for k, v in cb.items() if k != "client_id"}))
    with pytest.raises(IdentityNotVerified) as exc:
        sign_in(m)
    assert exc.value.error_class == "registration_incomplete"
    assert fake.grants == []


def test_bootstrap_client_id_echoed_back_is_never_accepted(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(
        mutate_callback=lambda cb: {**cb, "client_id": "dynamic_agent_client"}))
    with pytest.raises(IdentityNotVerified):
        sign_in(m)
    assert fake.grants == []


def test_reconnect_callback_with_a_different_client_id_is_rejected(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock)
    sign_in(m)
    pid = read_doc(m).active_profile_id
    before = read_doc(m).profile(pid)
    m._open_browser = fake.browser(mutate_callback=lambda cb: {**cb, "client_id": "oaiapp_attacker"})
    with pytest.raises(IdentityNotVerified) as exc:
        sign_in(m, profile_id=pid)
    assert exc.value.error_class == "client_id_mismatch"
    after = read_doc(m).profile(pid)
    assert after.client_id == before.client_id
    assert after.credentials.access_token == before.credentials.access_token


def test_pkce_mismatch_fails_and_keeps_issued_client_for_the_retry(tmp_path, fake, clock):
    seen = []
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(record=seen))
    original = oauth.ProviderClient.exchange_code

    def wrong_verifier(self, **kw):
        kw["verifier"] = oauth.new_pkce_verifier()
        return original(self, **kw)
    m.provider.exchange_code = wrong_verifier.__get__(m.provider)
    with pytest.raises(AuthorizationCancelled) as exc:
        sign_in(m)
    assert exc.value.error_class == "authorization_code_rejected"
    doc = read_doc(m)
    assert doc.profiles == []
    assert [r.client_id for r in doc.pending_registrations] == ["oaiapp_test1"]

    # The retry reuses that issued client: no second registration.
    m.provider.exchange_code = original.__get__(m.provider)
    status = sign_in(m)
    assert seen[1]["client_id"] == "oaiapp_test1" and "agent_name_hint" not in seen[1]
    assert status.state is SessionState.READY
    assert read_doc(m).pending_registrations == []


def test_invalid_code_is_rejected(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(
        mutate_callback=lambda cb: {**cb, "code": "CANARY-CODE-forged"}))
    with pytest.raises(AuthorizationCancelled):
        sign_in(m)
    assert read_doc(m).profiles == []


@pytest.mark.parametrize("override", [
    {"refresh_token": DROP}, {"token_type": "mac"}, {"expires_in": 0}, {"expires_in": "3600"},
    {"access_token": ""}, {"scope": DROP}, {"id_token": DROP},
])
def test_invalid_token_response_never_commits(tmp_path, fake, clock, override):
    fake.code_response_overrides = override
    m = make_manager(tmp_path, fake, clock)
    with pytest.raises(IdentityNotVerified):
        sign_in(m)
    assert read_doc(m).profiles == []


# ---------------------------------------------------------------------------
# ID token verification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,setup,error_class", [
    ("bad_signature", lambda f: setattr(f, "code_id_token_key", f.other_key), "id_token_rejected"),
    ("wrong_issuer", lambda f: f.code_id_token_overrides.update(iss="https://evil.example"), "id_token_rejected"),
    ("wrong_audience", lambda f: f.code_id_token_overrides.update(aud="oaiapp_someone_else"), "id_token_rejected"),
    ("expired", lambda f: f.code_id_token_overrides.update(exp=int(f.clock()) - 600,
                                                           iat=int(f.clock()) - 4200), "id_token_expired"),
    ("wrong_nonce", lambda f: f.code_id_token_overrides.update(nonce="not-this-attempt"), "id_token_nonce"),
    ("missing_subject", lambda f: f.code_id_token_overrides.update(sub=DROP), "id_token_rejected"),
    ("empty_subject", lambda f: f.code_id_token_overrides.update(sub=""), "id_token_subject"),
    ("azp_mismatch", lambda f: f.code_id_token_overrides.update(azp="oaiapp_other"), "id_token_azp"),
    ("future_iat", lambda f: f.code_id_token_overrides.update(iat=int(f.clock()) + 600), "id_token_issued_in_future"),
])
def test_id_token_validation_failures_never_commit(tmp_path, fake, clock, label, setup, error_class):
    setup(fake)
    m = make_manager(tmp_path, fake, clock)
    with pytest.raises(IdentityNotVerified) as exc:
        sign_in(m)
    assert exc.value.error_class == error_class, label
    assert read_doc(m).profiles == []
    assert m.get_session_state().state is SessionState.DISCONNECTED


def test_unsigned_and_symmetric_tokens_are_rejected_by_algorithm(fake):
    import jwt as pyjwt
    provider = oauth.ProviderClient(fake.issuer, session=provider_session(), allow_loopback_http=True)
    claims = {"iss": fake.issuer, "aud": "oaiapp_x", "sub": "s", "iat": int(fake.clock()),
              "exp": int(fake.clock()) + 60}
    for token in (pyjwt.encode(claims, None, algorithm="none"),
                  pyjwt.encode(claims, "shared-secret-for-confusion-attempt", algorithm="HS256")):
        with pytest.raises(oauth.IdentityInvalid):
            provider.verify_id_token(token, client_id="oaiapp_x", nonce=None, now=fake.clock())


def test_unknown_kid_refetches_jwks_once_then_is_unavailable_not_invalid(fake):
    """A kid still unknown after one refetch is a key-set propagation issue,
    not proof the token is bad: verification is unavailable (nothing is
    committed on sign-in; a refresh keeps its rotation and retries)."""
    provider = oauth.ProviderClient(fake.issuer, session=provider_session(), allow_loopback_http=True)
    token = fake.id_token(client_id="oaiapp_x", subject="s", kid="rotated-away")
    with pytest.raises(oauth.IdentityVerificationUnavailable) as exc:
        provider.verify_id_token(token, client_id="oaiapp_x", nonce=None, now=fake.clock())
    assert exc.value.error_class == "id_token_unknown_key"
    assert fake.jwks_hits == 1


def test_jwks_outage_is_temporary_and_commits_nothing(tmp_path, fake, clock):
    fake.jwks_status = 503
    m = make_manager(tmp_path, fake, clock)
    with pytest.raises(TemporaryAuthError):
        sign_in(m)
    assert read_doc(m).profiles == []


def test_discovery_must_name_the_expected_issuer_and_origin(fake):
    provider = oauth.ProviderClient(fake.issuer + "/", session=provider_session(), allow_loopback_http=True)
    assert provider.endpoints().issuer == fake.issuer
    with pytest.raises(ValueError):
        oauth.ProviderClient("http://auth.openai.com")
    with pytest.raises(ValueError):
        oauth.ProviderClient(fake.issuer)          # loopback http only when explicitly allowed (tests)


def test_production_defaults_point_at_openai():
    assert oauth.ISSUER == "https://auth.openai.com"
    assert oauth.RESOURCE == "https://api.openai.com/v1"
    assert oauth.ProviderClient().issuer == "https://auth.openai.com"


# ---------------------------------------------------------------------------
# Account / registration separation
# ---------------------------------------------------------------------------

def test_wrong_account_on_reconnect_fails_closed(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock)
    sign_in(m)
    pid = read_doc(m).active_profile_id
    before = read_doc(m).profile(pid)
    m._open_browser = fake.browser(subject="user-sub-B", email="owner@example.test")
    with pytest.raises(AccountMismatch):
        sign_in(m, profile_id=pid)
    after = read_doc(m).profile(pid)
    assert after.subject == "user-sub-A"
    assert after.generation == before.generation
    assert after.credentials.access_token == before.credentials.access_token
    assert len(read_doc(m).profiles) == 1


def test_same_email_registrations_stay_distinct(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock, browser=fake.browser(subject="sub-A", email="same@example.test"))
    sign_in(m)
    m._open_browser = fake.browser(subject="sub-B", email="same@example.test")
    sign_in(m, new_account=True)
    doc = read_doc(m)
    assert len(doc.profiles) == 2
    a, b = doc.profiles
    assert a.email == b.email == "same@example.test"
    assert (a.subject, a.client_id) != (b.subject, b.client_id)
    assert a.label != b.label
    assert a.credentials.refresh_token != b.credentials.refresh_token
    assert doc.active_profile_id == b.profile_id


def test_same_subject_new_registration_is_a_separate_profile(tmp_path, fake, clock):
    """A second bootstrap registration (e.g. another workspace) gets its own
    issued client; the vendor binds client to user+workspace."""
    m = make_manager(tmp_path, fake, clock)
    sign_in(m)
    sign_in(m, new_account=True)
    clients = [p.client_id for p in read_doc(m).profiles]
    assert clients == ["oaiapp_test1", "oaiapp_test2"]


def test_select_profile_switches_active_without_touching_credentials(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock)
    sign_in(m)
    m._open_browser = fake.browser(subject="sub-B")
    sign_in(m, new_account=True)
    doc = read_doc(m)
    a = doc.profiles[0]
    snapshot = json.dumps([p.to_dict() for p in doc.profiles], sort_keys=True)
    m.select_profile(a.profile_id)
    doc2 = read_doc(m)
    assert doc2.active_profile_id == a.profile_id
    assert json.dumps([p.to_dict() for p in doc2.profiles], sort_keys=True) == snapshot


def test_status_dtos_carry_no_credentials(tmp_path, fake, clock):
    m = make_manager(tmp_path, fake, clock)
    sign_in(m)
    grant = m.get_valid_access_token()
    texts = [repr(m.get_session_state()), repr(m.list_profiles()), repr(m.get_active_profile()),
             repr(grant), str(grant)]
    for text in texts:
        assert "CANARY" not in text and "user-sub-A" not in text and "oaiapp_" not in text
        assert "owner@example.test" not in text
