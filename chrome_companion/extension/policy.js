// Lumina Chrome Companion restricted-surface policy (BROWSER-COMPANION-01A).
// Mirror of chrome_companion/policy.py -- tests/test_chrome_companion_extension.py
// proves the two tables are identical. Loaded by the service worker
// (importScripts) and the popup (<script>). Pure: no Chrome APIs.
//
// Allowlist the readable schemes (http, https); every other scheme --
// chrome://, chrome-extension://, devtools://, view-source:, file:, data:,
// blob:, about:, javascript:, ... -- is restricted. Then deny a small set of
// credential / payment / account-security hosts.
//
// Hosts are compared in CANONICAL form (BROWSER-COMPANION-01A-R1 / F2): the
// WHATWG parser's hostname, lowercased, with EVERY trailing dot trimmed --
// exactly how Chrome matches hosts for extension permissions
// (url_pattern.cc CanonicalizeHostForMatching) -- then required to be
// dot-separated non-empty [a-z0-9_-] labels. "accounts.google.com." is
// accounts.google.com. Origins and permission patterns use the canonical host.
"use strict";

globalThis.LuminaPolicy = (() => {
  const READABLE_SCHEMES = Object.freeze(["http", "https"]);
  const RESTRICTED_HOSTS = Object.freeze([
    "passwords.google.com",
    "passwords.google",
    "pay.google.com",
    "payments.google.com",
    "wallet.google.com",
    "accounts.google.com",
    "myaccount.google.com",
    "chrome.google.com",
    "chromewebstore.google.com",
  ]);

  const CANONICAL_DNS_HOST = /^[a-z0-9_-]+(?:\.[a-z0-9_-]+)*$/;
  const IPV6_HOST = /^\[[0-9a-f:.]+\]$/;

  function refused(reason) {
    return { readable: false, reason, origin: null, pattern: null };
  }

  // Mirror of canonical_host() in chrome_companion/policy.py.
  function canonicalHost(hostname) {
    if (typeof hostname !== "string") return null;
    const host = hostname.toLowerCase();
    if (host.startsWith("[")) return IPV6_HOST.test(host) ? host : null;
    const trimmed = host.replace(/\.+$/, "");
    return CANONICAL_DNS_HOST.test(trimmed) ? trimmed : null;
  }

  function classifyUrl(raw) {
    if (typeof raw !== "string" || raw.length === 0) return refused("invalid_url");
    let url;
    try {
      url = new URL(raw);
    } catch {
      return refused("invalid_url");
    }
    const scheme = url.protocol.replace(/:$/, "").toLowerCase();
    if (!READABLE_SCHEMES.includes(scheme)) return refused("restricted_scheme");
    const host = canonicalHost(url.hostname);
    if (!host) return refused("invalid_url");
    if (RESTRICTED_HOSTS.includes(host)) return refused("restricted_host");
    const port = url.port ? `:${url.port}` : ""; // WHATWG already drops a default port
    return { readable: true, reason: null, origin: `${scheme}://${host}${port}`, pattern: `${scheme}://${host}/*` };
  }

  // BROWSER-COMPANION-COMPLETION-01 Slice 1. Mirror of sensitive_navigation()
  // in chrome_companion/policy.py: a heuristic tripwire refusing generic
  // navigation to a GET that is conventionally an account action. Not a proof
  // that any other URL is harmless.
  const SENSITIVE_PATH_WORDS = Object.freeze(["logout", "logoff", "signout", "signoff", "unsubscribe",
    "deactivate", "deleteaccount", "closeaccount", "removeaccount"]);
  const SENSITIVE_PATH_SEGMENTS = Object.freeze(["delete", "destroy"]);

  function sensitiveNavigation(raw) {
    if (!classifyUrl(raw).readable) return null;
    // ASCII percent-escapes decoded first, same rule as policy.py.
    const path = new URL(raw).pathname
      .replace(/%([0-7][0-9a-fA-F])/g, (_, hex) => String.fromCharCode(parseInt(hex, 16))).toLowerCase();
    for (const segment of path.split("/")) {
      const squashed = segment.replace(/[-_]/g, "");
      if (SENSITIVE_PATH_SEGMENTS.includes(segment)
        || SENSITIVE_PATH_WORDS.some((word) => squashed.includes(word))) return "sensitive_path";
    }
    return null;
  }

  return Object.freeze({ READABLE_SCHEMES, RESTRICTED_HOSTS, SENSITIVE_PATH_WORDS, SENSITIVE_PATH_SEGMENTS,
    canonicalHost, classifyUrl, sensitiveNavigation });
})();
