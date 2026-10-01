"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- boundaries around the ChatGPT session:

  * backup: neither Agent Backup nor Memory Backup can carry a session or
    host-identity document -- not normally, not via symlink, hard link,
    rename/move or copy into a collected position;
  * fuel: authentication failure never authorizes a different fuel. With
    paid API credentials deliberately populated, every auth/refresh/revoke/
    scope/corruption failure produces zero requests anywhere but the auth
    provider, zero non-loopback DNS, and zero reads of the API-key store;
  * lane: a READY session does not make `openai_chatgpt_plan` constructible
    or selectable (01B's reservation stands until 01D/01E change it);
  * no inference: the auth package has no Responses/models surface and no
    API-key/config/secrets/backends dependency (static + runtime).
"""
import ast
import json
import os
import shutil
import socket
import zipfile

import pytest
import requests

import config
import core.agent_backup as ab
import core.backend_identity as bi
from core.backend_identity import ReservedBackendLaneError
from core.backends import loader
from core.chatgpt_auth import oauth
from core.chatgpt_auth.session import (
    ChatGPTAuthError, ChatGPTSessionManager, PlanPermissionNotGranted,
    ReauthRequired, SessionState, SessionStorageError, SignInRequired, TemporaryAuthError,
)
from core.chatgpt_auth.store import SessionStore
from chatgpt_auth_fakes import DROP, NO_PLAN_SCOPES, FakeClock, FakeOpenAIAuth, provider_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "core", "chatgpt_auth")
PLAN = bi.OPENAI_CHATGPT_PLAN_LANE


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    f = FakeOpenAIAuth(clock=clock)
    yield f
    f.close()


def make_manager(directory, fake, clock, **kw):
    store = SessionStore(str(directory), lock_timeout=10)
    provider = oauth.ProviderClient(fake.issuer, session=provider_session(), allow_loopback_http=True,
                                    sleep=lambda s: None)
    return ChatGPTSessionManager(store, provider, clock=clock,
                                 open_browser=kw.pop("browser", fake.browser()),
                                 recorder=lambda e, f: None, **kw)


def sign_in(m, **kw):
    pending = m.begin_sign_in(**kw)
    status = m.complete_sign_in(pending, timeout=10)
    return m.confirm_sign_in(pending) if status.state is SessionState.AUTHORIZING else status


def ready_store(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    return m


# ---------------------------------------------------------------------------
# Agent Backup
# ---------------------------------------------------------------------------

def _backup_env(tmp_path):
    """Minimal real Agent Backup environment (mirrors tests/test_agent_backup.py)."""
    from test_agent_backup import _make_env
    return _make_env(tmp_path / "env")


def _agent_backup(data_dir, base_dir, dest):
    return ab.build_agent_backup(data_dir, base_dir, dest,
                                 identity_trailers_path="/nonexistent/identity_trailers.json",
                                 credentials_path="/nonexistent/credentials.json")


def _zip_bytes(path):
    blob = b""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            blob += name.encode() + zf.read(name)
    return blob


def test_agent_backup_never_contains_session_material(tmp_path, fake, clock):
    m = ready_store(tmp_path, fake, clock)
    m.get_valid_access_token()
    data_dir, base_dir = _backup_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _agent_backup(data_dir, base_dir, dest)
    blob = _zip_bytes(dest)
    for needle in (b"CANARY", b"user-sub-A", b"oaiapp_", b"urn:uuid:", b"lumina.chatgpt."):
        assert needle not in blob, needle
    verdict = ab.verify_agent_backup(dest)
    assert verdict["valid"], verdict["errors"]
    assert manifest["members"]


@pytest.mark.parametrize("trick", ["hardlink", "symlink", "moved", "copied", "host_copied"])
def test_agent_backup_refuses_session_documents_in_collected_positions(tmp_path, fake, clock, trick):
    m = ready_store(tmp_path, fake, clock)
    data_dir, base_dir = _backup_env(tmp_path)
    planted = os.path.join(base_dir, "personas", "innocent.json")
    src = m.store.sessions_path
    if trick == "hardlink":
        os.link(src, planted)
    elif trick == "symlink":
        os.symlink(src, planted)
    elif trick == "moved":
        os.replace(src, planted)
    elif trick == "copied":
        shutil.copyfile(src, planted)
    elif trick == "host_copied":
        shutil.copyfile(m.store.host_path, planted)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError) as exc:
        _agent_backup(data_dir, base_dir, dest)
    assert "CANARY" not in str(exc.value)
    assert not os.path.exists(dest)


def test_agent_backup_tripwire_ignores_prose_naming_the_schema(tmp_path):
    data_dir, base_dir = _backup_env(tmp_path)
    with open(os.path.join(base_dir, "skills", "notes.md"), "w") as fh:
        fh.write("# Notes\nThe session store uses schema lumina.chatgpt.sessions/1 on disk.\n")
    dest = str(tmp_path / "backup.zip")
    _agent_backup(data_dir, base_dir, dest)
    assert os.path.exists(dest)


# ---------------------------------------------------------------------------
# Memory Backup (the one-click data-dir zip)
# ---------------------------------------------------------------------------

@pytest.fixture
def _memory_backup_no_db(monkeypatch):
    import core.memory_backup as mb

    class _Conn:
        def execute(self, *a, **k):
            pass

        def close(self):
            pass
    monkeypatch.setattr(mb, "_db", lambda: _Conn())
    return mb


@pytest.mark.parametrize("trick", ["none", "hardlink", "symlink", "moved", "copied", "host_symlink"])
def test_memory_backup_never_contains_session_material(tmp_path, fake, clock, _memory_backup_no_db, trick):
    m = ready_store(tmp_path, fake, clock)
    data_dir = tmp_path / "data"
    (data_dir / "memory").mkdir(parents=True)
    (data_dir / "memory" / "lumina.db").write_bytes(b"fake db")
    planted = str(data_dir / "memory" / "notes.json")
    if trick == "hardlink":
        os.link(m.store.sessions_path, planted)
    elif trick == "symlink":
        os.symlink(m.store.sessions_path, planted)
    elif trick == "moved":
        os.replace(m.store.sessions_path, planted)
    elif trick == "copied":
        shutil.copyfile(m.store.sessions_path, planted)
    elif trick == "host_symlink":
        os.symlink(m.store.host_path, planted)
    dest = tmp_path / "mem.zip"
    _memory_backup_no_db.build_memory_backup(str(data_dir), str(dest))
    blob = _zip_bytes(dest)
    assert b"CANARY" not in blob and b"lumina.chatgpt." not in blob and b"urn:uuid:" not in blob
    assert b"fake db" in blob                       # ordinary data still backed up


# ---------------------------------------------------------------------------
# Fuel isolation
# ---------------------------------------------------------------------------

class _Tripwire:
    def __init__(self, allowed_origin):
        self.allowed_origin = allowed_origin
        self.requests = []
        self.dns = []
        self.secret_reads = 0

    def foreign_requests(self):
        # The auth provider fake and the loopback callback are both 127.0.0.1.
        return [u for _m, u in self.requests if not u.startswith("http://127.0.0.1:")]


@pytest.fixture
def paid_keys_and_tripwire(monkeypatch, fake, tmp_path):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-proj-" + "A" * 40)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-api03-" + "B" * 40)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-" + "C" * 40)
    for var, val in (("OPENAI_API_KEY", "sk-proj-" + "E" * 40), ("ANTHROPIC_API_KEY", "sk-ant-" + "F" * 40),
                     ("OPENROUTER_API_KEY", "sk-or-" + "G" * 40)):
        monkeypatch.setenv(var, val)
    import core.secrets as secrets_mod
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"openai_api_key": "sk-proj-" + "H" * 40,
                                 "anthropic_api_key": "sk-ant-" + "I" * 40,
                                 "openrouter_api_key": "sk-or-" + "J" * 40}))
    monkeypatch.setattr(secrets_mod, "SECRETS_PATH", str(creds))
    tw = _Tripwire(fake.issuer)
    real_load = secrets_mod._load

    def counting_load():
        tw.secret_reads += 1
        return real_load()
    monkeypatch.setattr(secrets_mod, "_load", counting_load)
    real_request = requests.sessions.Session.request

    def request(self, method, url, *a, **kw):
        tw.requests.append((method, str(url)))
        if not str(url).startswith("http://127.0.0.1:"):
            raise requests.ConnectionError("tripwire: only the auth provider is reachable")
        return real_request(self, method, url, *a, **kw)
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):
        if host not in ("127.0.0.1", "localhost"):
            tw.dns.append(str(host))
            raise OSError("tripwire: no DNS")
        return real_getaddrinfo(host, *a, **kw)
    monkeypatch.setattr(requests.sessions.Session, "request", request)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return tw


def _expect_auth_error(fn):
    with pytest.raises(ChatGPTAuthError):
        fn()


def test_no_auth_failure_reaches_any_other_fuel(tmp_path, fake, clock, paid_keys_and_tripwire):
    tw = paid_keys_and_tripwire
    # 1. consent denied
    m = make_manager(tmp_path / "s1", fake, clock, browser=fake.browser(consent=False))
    _expect_auth_error(lambda: sign_in(m))
    _expect_auth_error(m.get_valid_access_token)
    # 2. missing plan scope
    m = make_manager(tmp_path / "s2", fake, clock, browser=fake.browser(scopes=NO_PLAN_SCOPES))
    sign_in(m)
    with pytest.raises(PlanPermissionNotGranted):
        m.get_valid_access_token()
    # Identity granted without offline access remains connected but unusable.
    fake.code_scope_override = "openid profile email"
    fake.code_response_overrides["refresh_token"] = DROP
    m = make_manager(tmp_path / "s2_identity_only", fake, clock)
    sign_in(m)
    with pytest.raises(PlanPermissionNotGranted):
        m.get_valid_access_token()
    fake.code_scope_override = None
    fake.code_response_overrides.clear()
    # 3. terminal refresh failure
    m = make_manager(tmp_path / "s3", fake, clock)
    sign_in(m)
    clock.advance(3600)
    fake.refresh_faults.append((400, {"error": "refresh_token_reused"}))
    with pytest.raises(ReauthRequired):
        m.get_valid_access_token()
    _expect_auth_error(m.get_valid_access_token)
    # 4. revoked session
    m = make_manager(tmp_path / "s4", fake, clock)
    sign_in(m)
    m.disconnect()
    with pytest.raises(SignInRequired):
        m.get_valid_access_token()
    # 5. temporary failure (5xx)
    m = make_manager(tmp_path / "s5", fake, clock)
    sign_in(m)
    clock.advance(3600)
    fake.refresh_faults.append((503, {}))
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    with pytest.raises(ReauthRequired):
        m.get_valid_access_token()
    assert m.disconnect().remote_revocation_confirmed is False
    # 6. corrupt session
    with open(m.store.sessions_path, "wb") as fh:
        fh.write(b"{broken")
    with pytest.raises(SessionStorageError):
        m.get_valid_access_token()
    # 7. identity verification failure
    fake.code_id_token_overrides = {"aud": "oaiapp_other"}
    m = make_manager(tmp_path / "s7", fake, clock)
    _expect_auth_error(lambda: sign_in(m))
    fake.code_id_token_overrides = {}
    # A forged first-registration callback cannot create reusable custody.
    m = make_manager(tmp_path / "s8", fake, clock, browser=fake.browser(
        mutate_callback=lambda cb: {**cb, "code": "junk"}))
    _expect_auth_error(lambda: sign_in(m))
    with m.store.locked():
        assert m.store.read().pending_registrations == []

    assert tw.foreign_requests() == []
    assert tw.dns == []
    assert tw.secret_reads == 0
    assert tw.requests, "instrument sanity: the auth provider itself was reached"
    for _m, url in tw.requests:
        assert "/v1/responses" not in url and "/models" not in url


def test_tripwire_instrument_is_live(fake, paid_keys_and_tripwire):
    s = requests.Session()
    with pytest.raises(requests.ConnectionError):
        s.post("https://api.openai.com/v1/responses", json={})
    assert paid_keys_and_tripwire.foreign_requests() == ["https://api.openai.com/v1/responses"]


def test_ready_session_does_not_make_the_plan_lane_constructible_or_selectable(tmp_path, fake, clock):
    m = ready_store(tmp_path, fake, clock)
    assert m.get_session_state().state is SessionState.READY
    assert PLAN not in loader.BACKENDS
    assert bi.LANES[PLAN].constructible is False
    assert not bi.LANES[PLAN].admitted_operations
    with pytest.raises(ReservedBackendLaneError):
        loader.get_llm_backend(PLAN)
    src = open(os.path.join(ROOT, "ui", "settings", "general_tab.py"), encoding="utf-8").read()
    assert PLAN not in src and "chatgpt" not in src.lower()


def test_access_grant_is_bearer_material_for_the_plan_lane_only(tmp_path, fake, clock):
    """The grant is not an API key and nothing maps it onto one."""
    m = ready_store(tmp_path, fake, clock)
    grant = m.get_valid_access_token()
    assert not grant.access_token.startswith("sk-")
    assert getattr(config, "OPENAI_API_KEY", "") != grant.access_token


# ---------------------------------------------------------------------------
# Static: no inference, no API-key/config/backends dependency, no printing
# ---------------------------------------------------------------------------

_STDLIB_OK = {"__future__", "base64", "contextlib", "dataclasses", "datetime", "enum", "hashlib",
              "hmac", "http", "http.server", "json", "math", "os", "re", "secrets", "stat",
              "threading", "time", "typing", "urllib", "urllib.parse", "uuid", "webbrowser",
              "socketserver",
              "fcntl", "msvcrt"}


def _package_sources():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            path = os.path.join(PKG, name)
            yield name, open(path, encoding="utf-8").read()


def test_auth_package_imports_are_allowlisted():
    allowed = _STDLIB_OK | {"inspect", "jwt", "requests", "core.test_isolation", "core.flight_recorder",
                            "core.chatgpt_auth", "core.chatgpt_auth.store", "core.chatgpt_auth.oauth",
                            "core.chatgpt_auth.callback", "core.chatgpt_auth.session"}
    for name, src in _package_sources():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                assert mod in allowed, f"{name} imports {mod}"


@pytest.mark.parametrize("needle", ["/responses", "/models", "api_key", "API_KEY", "get_secret",
                                    "core.secrets", "core.backends", "import config",
                                    "anthropic", "openrouter", "id_token_hint", "login_hint"])
def test_auth_package_has_no_inference_or_api_key_surface(needle):
    for name, src in _package_sources():
        code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
        if needle in ("id_token_hint", "login_hint"):
            # Only allowed inside the docstring that explains why they are never sent.
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and needle in node.value:
                    assert node.value.count("\n") > 2, f"{needle} used as a value in {name}"
            continue
        assert needle not in code, f"{needle!r} in core/chatgpt_auth/{name}"


def test_auth_package_never_prints_or_logs():
    for name, src in _package_sources():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", name
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                assert not any(m == "logging" or m.startswith("logging.") for m in mods), name
            if isinstance(node, ast.Attribute) and node.attr in ("stderr", "stdout"):
                raise AssertionError(f"{name} writes to {node.attr}")


def _imports_of(path):
    mods = set()
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_only_the_settings_card_consumes_the_session_manager_in_01c():
    """01D will add the plan transport; until then nothing else in product
    code may obtain a ChatGPT bearer token."""
    users = []
    for top in ("core", "ui", "tools", "comms"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            if "__pycache__" in dirpath or dirpath.startswith(PKG):
                continue
            for fname in files:
                if fname.endswith(".py"):
                    path = os.path.join(dirpath, fname)
                    if any(m == "core.chatgpt_auth" or m.startswith("core.chatgpt_auth.")
                           for m in _imports_of(path)):
                        users.append(os.path.relpath(path, ROOT))
    assert sorted(users) == ["ui/settings/chatgpt_plan_card.py"]


def test_runtime_auth_http_goes_only_to_the_issuer(fake):
    """Discovery may not redirect token/revoke/JWKS traffic to another origin."""
    import http.server
    import threading

    class Evil(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"issuer": issuer, "authorization_endpoint": issuer + "/a",
                               "token_endpoint": "https://api.openai.com/v1/responses",
                               "revocation_endpoint": issuer + "/r", "jwks_uri": issuer + "/j"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.HTTPServer(("127.0.0.1", 0), Evil)
    issuer = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        provider = oauth.ProviderClient(issuer, session=provider_session(), allow_loopback_http=True)
        with pytest.raises(oauth.ProviderUnavailable) as exc:
            provider.endpoints()
        assert exc.value.error_class == "discovery_invalid"
    finally:
        srv.shutdown()
        srv.server_close()
