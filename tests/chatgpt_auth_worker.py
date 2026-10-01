"""
SUBSCRIPTION-PLAN-BACKENDS-01C multi-process worker: a separate Python
process (a "second Lumina") that shares one session store with the test and
asks it for a valid access token against the fake provider.

Isolation is established from the environment BEFORE any project import
(the parent passes LUMINA_TESTING/LUMINA_DATA_DIR/LUMINA_SECRETS_PATH/
LUMINA_CHATGPT_AUTH_DIR pointing into its tmp_path). Prints one JSON line;
never prints a token (only a short digest of it).

usage: chatgpt_auth_worker.py <issuer> <store_dir> <mode> [<clock_epoch>]
modes:
  token                       get_valid_access_token
  crash_before_request        die before the refresh request is sent
  crash_after_response        die after the vendor rotated, before anything is persisted
  crash_after_checkpoint      die after the rotation is checkpointed, before identity check
  disconnect                  disconnect the active profile
"""
import hashlib
import json
import os
import sys

for key in ("LUMINA_TESTING", "LUMINA_DATA_DIR", "LUMINA_SECRETS_PATH", "LUMINA_CHATGPT_AUTH_DIR"):
    if not os.environ.get(key):
        print(json.dumps({"error": f"isolation env {key} missing"}))
        sys.exit(3)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from core.chatgpt_auth import oauth  # noqa: E402
from core.chatgpt_auth.session import ChatGPTAuthError, ChatGPTSessionManager  # noqa: E402
from core.chatgpt_auth.store import SessionStore  # noqa: E402


def main():
    issuer, store_dir, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    now = float(sys.argv[4]) if len(sys.argv) > 4 else None
    session = requests.Session()
    session.trust_env = False
    provider = oauth.ProviderClient(issuer, session=session, allow_loopback_http=True)
    manager = ChatGPTSessionManager(SessionStore(store_dir, lock_timeout=60), provider,
                                    clock=(lambda: now) if now is not None else __import__("time").time,
                                    open_browser=lambda url: False, recorder=lambda e, f: None)
    if mode == "crash_before_request":
        provider.refresh = lambda **kw: os._exit(9)
    elif mode == "crash_after_response":
        original = provider.refresh

        def refresh_then_die(**kw):
            original(**kw)
            os._exit(9)
        provider.refresh = refresh_then_die
    elif mode == "crash_after_checkpoint":
        provider.verify_id_token = lambda *a, **kw: os._exit(9)
    try:
        if mode == "disconnect":
            outcome = manager.disconnect()
            print(json.dumps({"ok": True, "state": outcome.state.value,
                              "remote": outcome.remote_revocation_confirmed}))
            return
        grant = manager.get_valid_access_token()
        print(json.dumps({"ok": True, "generation": grant.generation,
                          "token_digest": hashlib.sha256(grant.access_token.encode()).hexdigest()[:16]}))
    except ChatGPTAuthError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "class": exc.error_class}))


if __name__ == "__main__":
    main()
