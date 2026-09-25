"""Chrome Companion restricted-surface policy (BROWSER-COMPANION-01A).

Python mirror of chrome_companion/extension/policy.js. The extension is the
primary enforcement point (it refuses to read restricted tabs at all); the
hub re-applies this policy to every returned tab/URL as defense in depth, so
a single-side regression cannot leak a restricted surface.
tests/test_chrome_companion_extension.py proves both tables are identical.

Rule: ALLOWLIST the readable schemes (http, https) -- every other scheme is
restricted, which covers chrome://, chrome-extension://, chrome-untrusted://,
devtools://, view-source:, file://, data:, blob:, filesystem:, javascript:,
about:, and anything Chrome adds later. Then DENY a small set of
credential/payment/account-security hosts even though they are https.
Chrome's API permitting access to a surface does not mean Lumina reads it.

Hosts are compared in CANONICAL form (BROWSER-COMPANION-01A-R1 / F2), so an
equivalent spelling of a restricted host stays restricted:
  * lowercase, then trim EVERY trailing dot -- exactly how Chrome itself
    matches hosts for extension permissions (extensions/common/url_pattern.cc,
    CanonicalizeHostForMatching: TrimString(host, ".", TRIM_TRAILING)), so
    "accounts.google.com." and "ACCOUNTS.GOOGLE.COM.." are accounts.google.com;
  * a DNS host must then be dot-separated non-empty labels of [a-z0-9_-]. That
    is the only form Chrome reports tab URLs in (lowercase, punycode, percent-
    decoded). The JS side gets it from the WHATWG URL parser; this side does
    not canonicalize percent-escapes, IDN, or full-width forms itself, so any
    host it cannot prove canonical is refused (invalid_url) -- never guessed
    readable.
Origins and permission patterns are built from the canonical host.

Parser drift fails closed (BROWSER-COMPANION-01A-R2 / P2). WHATWG URL (the
browser, and policy.js) treats "\\" like "/" in an http(s) URL's slashes,
authority, and path; Python's urlsplit does not. The two can therefore split
the SAME string into different hosts -- https://accounts.google.com\\@evil.test/
is accounts.google.com to Chrome but evil.test to urlsplit. Chrome never
reports an http(s) URL with a backslash before its query or fragment, so this
side refuses one outright (invalid_url) instead of guessing what the browser
meant. Nothing here needs exact reason-code parity for such input -- only
that it is never readable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_BEFORE_QUERY_OR_FRAGMENT = re.compile(r"[^?#]*")

READABLE_SCHEMES = ("http", "https")

RESTRICTED_HOSTS = frozenset({
    # Google password manager / payments / wallet / account security.
    "passwords.google.com",
    "passwords.google",
    "pay.google.com",
    "payments.google.com",
    "wallet.google.com",
    "accounts.google.com",
    "myaccount.google.com",
    # Chrome Web Store (extension install surface; Chrome also blocks
    # scripting there).
    "chrome.google.com",
    "chromewebstore.google.com",
})


_CANONICAL_DNS_HOST = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")
_IPV6_HOST = re.compile(r"[0-9a-f:.]+")  # urlsplit strips the [brackets]


def canonical_host(hostname) -> str | None:
    """The canonical form restricted-host comparison uses, or None if the
    host is not in (or reducible to) canonical ASCII form. Mirror of
    canonicalHost() in extension/policy.js."""
    if not isinstance(hostname, str):
        return None
    host = hostname.lower()
    if ":" in host:
        return host if _IPV6_HOST.fullmatch(host) else None
    host = host.rstrip(".")
    return host if _CANONICAL_DNS_HOST.fullmatch(host) else None


@dataclass(frozen=True)
class UrlClass:
    readable: bool
    reason: str | None      # "restricted_scheme" | "restricted_host" | "invalid_url" | None
    origin: str | None      # scheme://host[:port] for readable URLs
    pattern: str | None     # Chrome host-permission match pattern, scheme://host/*


def classify_url(url) -> UrlClass:
    if not isinstance(url, str) or not url:
        return UrlClass(False, "invalid_url", None, None)
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return UrlClass(False, "invalid_url", None, None)
    if not scheme:  # WHATWG URL (the JS side) refuses scheme-less input outright
        return UrlClass(False, "invalid_url", None, None)
    if scheme not in READABLE_SCHEMES:
        return UrlClass(False, "restricted_scheme", None, None)
    if "\\" in _BEFORE_QUERY_OR_FRAGMENT.match(url).group():
        return UrlClass(False, "invalid_url", None, None)  # noncanonical: parsers disagree
    host = canonical_host(hostname)
    if not host:
        return UrlClass(False, "invalid_url", None, None)
    if host in RESTRICTED_HOSTS:
        return UrlClass(False, "restricted_host", None, None)
    default_port = 80 if scheme == "http" else 443
    host_for_origin = f"[{host}]" if ":" in host else host
    origin = f"{scheme}://{host_for_origin}"
    if port is not None and port != default_port:
        origin += f":{port}"
    return UrlClass(True, None, origin, f"{scheme}://{host_for_origin}/*")
