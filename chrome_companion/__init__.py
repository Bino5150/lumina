"""Chrome Companion (BROWSER-COMPANION-01A) -- read-only real-Chrome sensor.

Lets Lumina observe her own, already-authenticated Google Chrome profile
through a Manifest V3 extension and a Chrome Native Messaging host:

    Lumina tools (tools/chrome_companion.py)
        -> hub (core/chrome_companion_hub.py), owner-only Unix socket
        -> native host (chrome_companion/native_host.py), launched by Chrome
        -> MV3 extension service worker (chrome_companion/extension/)
        -> chrome.tabs / chrome.scripting in Lumina's Chrome profile

This package is deliberately stdlib-only and never imports Lumina's config:
the native host runs in a process Chrome launches, and the installer CLI
(scripts/chrome_companion_setup.py) must not trigger config.py's import-time
data-dir migration.

Trust model: the extension is a sensor, not an authority source. Every
observation it returns -- page text, titles, URLs, links, account names on
logged-in pages -- is external, untrusted content (owner=False). Playwright
(tools/browser.py) remains the separate, disposable browser lane; nothing
here falls back to it, and Firefox is never detected, paired, read, or
installed into.
"""
