"""BROWSER-COMPANION-01A -- runs the extension's JavaScript tests under Node and
proves the JS restricted-surface policy is identical to the Python one.

These are security guards, so they must BLOCK in CI: when Node is missing,
they fail under CI (the CI env var GitHub Actions sets) and only skip on a
developer machine without Node."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from chrome_companion import policy
from chrome_companion_testkit import require_node

REPO = Path(__file__).resolve().parents[1]
JS_DIR = Path(__file__).resolve().parent / "chrome_companion_js"
POLICY_JS = REPO / "chrome_companion" / "extension" / "policy.js"

URL_CORPUS = [
    "https://www.reddit.com/r/AgentsInteractive/", "https://mail.google.com/mail/u/0/#inbox",
    "http://localhost:17493/app", "https://example.test:443/x", "https://example.test:8443/x",
    "http://[::1]:8080/", "https://WWW.Reddit.COM/r/x", "chrome://settings/passwords",
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop/popup.html", "chrome-untrusted://terminal/",
    "devtools://devtools/bundled/inspector.html", "view-source:https://example.test/",
    "file:///etc/passwd", "data:text/html,x", "blob:https://example.test/uuid", "about:blank",
    "javascript:alert(1)", "ftp://example.test/", "https://passwords.google.com/",
    "https://pay.google.com/x", "https://accounts.google.com/signin", "https://myaccount.google.com/",
    "https://chrome.google.com/webstore", "https://chromewebstore.google.com/", "https://wallet.google.com/",
    "", "not a url", "https://",
    # BROWSER-COMPANION-01A-R1 / F2: equivalent DNS spellings (trailing dots,
    # case) canonicalize identically on both sides.
    "https://accounts.google.com@evil.test/", "https://example.com./", "https://Example.COM../a",
    "http://127.0.0.1./", "http://127.0.0.1.:8080/", "https://a_b.example.test/", "https://.accounts.google.com/",
    "https://accounts..google.com/", "https://a!b.test/",
]
# Canonical-form spellings of restricted hosts (part of the exact-parity corpus).
RESTRICTED_ALIASES = [
    "https://accounts.google.com./", "https://ACCOUNTS.GOOGLE.COM./", "https://passwords.google.com./",
    "https://PASSWORDS.GOOGLE.COM./", "https://accounts.google.com../", "https://accounts.google.com.../x",
    "https://accounts.google.com.:443/", "https://user@accounts.google.com./", "https://passwords.google./",
]
URL_CORPUS += RESTRICTED_ALIASES

# Spellings only a URL parser canonicalizes (percent-escapes, full-width,
# soft hyphen). Chrome never reports these; the JS side canonicalizes them
# (WHATWG URL), the Python side refuses them as non-canonical. They must be
# refused on BOTH sides -- a restricted alias is never readable anywhere.
NON_CANONICAL_RESTRICTED_ALIASES = [
    "https://accounts.google%2Ecom/", "https://accounts%2egoogle.com/", "https://%61ccounts.google.com/",
    "https://accounts.google.com%2E/", "https://ａｃｃｏｕｎｔｓ.google.com/",
    "https://acc­ounts.google.com/", "https://PASSWORDS.GOOGLE%2eCOM./",
]


def _node() -> str:
    return require_node()


def test_extension_worker_behaviour_suite():
    result = subprocess.run([_node(), str(JS_DIR / "worker_test.js")], capture_output=True, text=True,
                            timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    # Every test ran to completion: a hung test must not pass as an early exit.
    summary = re.search(r"^(\d+)/(\d+) worker tests passed$", result.stdout, re.M)
    assert summary and summary.group(1) == summary.group(2), result.stdout[-2000:]
    assert result.stdout.count("\nok - ") + result.stdout.startswith("ok - ") == int(summary.group(2))


def test_extension_sources_parse():
    node = _node()
    for path in (REPO / "chrome_companion" / "extension").glob("*.js"):
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30,
                                check=False)
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def _js_policy(urls):
    script = (
        "const vm = require('node:vm'); const fs = require('node:fs');"
        "const ctx = vm.createContext({ URL }); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
        "const P = ctx.LuminaPolicy; const urls = JSON.parse(process.argv[2]);"
        "process.stdout.write(JSON.stringify({ schemes: [...P.READABLE_SCHEMES], hosts: [...P.RESTRICTED_HOSTS],"
        " verdicts: urls.map((u) => { const v = P.classifyUrl(u); return [v.readable, v.reason, v.origin, v.pattern]; }) }));"
    )
    result = subprocess.run([_node(), "-e", script, str(POLICY_JS), json.dumps(urls)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_and_python_policies_are_identical():
    js = _js_policy(URL_CORPUS)
    assert js["schemes"] == list(policy.READABLE_SCHEMES)
    assert sorted(js["hosts"]) == sorted(policy.RESTRICTED_HOSTS)
    for url, js_verdict in zip(URL_CORPUS, js["verdicts"]):
        py = policy.classify_url(url)
        assert [py.readable, py.reason, py.origin, py.pattern] == js_verdict, url


def test_restricted_host_aliases_are_refused_on_both_sides():
    corpus = RESTRICTED_ALIASES + NON_CANONICAL_RESTRICTED_ALIASES
    for url, (js_readable, js_reason, _, _) in zip(corpus, _js_policy(corpus)["verdicts"]):
        py = policy.classify_url(url)
        assert not js_readable and not py.readable, url
        assert js_reason in {"restricted_host", "invalid_url"} and py.reason in {"restricted_host", "invalid_url"}
    for url, (js_readable, js_reason, _, _) in zip(NON_CANONICAL_RESTRICTED_ALIASES,
                                                   _js_policy(NON_CANONICAL_RESTRICTED_ALIASES)["verdicts"]):
        assert js_reason == "restricted_host", url  # the browser-side parser sees through them
