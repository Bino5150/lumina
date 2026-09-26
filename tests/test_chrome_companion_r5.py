"""BROWSER-COMPANION-01A-R5 / AR5 -- "Revoked." means Chrome's grant is gone.

Goblin's AR5: chrome.permissions.remove() resolves false when it did not
remove the permission (Chrome's API contract), yet the R4 worker answered
{ok: true} for any fulfilled removal and the popup said "Revoked." -- while
Chrome still granted the site, and once the revoke ended, reads went through
again. The behaviour is tested against the REAL worker.js and popup.js in
tests/chrome_companion_js/worker_test.js ("R5 ..." cases; run by
test_chrome_companion_extension.py) and through the real worker, host and hub
in test_chrome_companion_e2e.py. This file pins what the owner is told."""
from __future__ import annotations

import json

from test_chrome_companion_installer import REPO

EXTENSION = REPO / "chrome_companion" / "extension"


def test_readme_says_a_revoke_blocks_until_the_popups_allow_and_revoked_means_chromes_grant_is_gone():
    text = " ".join((REPO / "chrome_companion" / "README.md").read_text().split())
    assert "A revoke from the popup also blocks the site in Lumina's companion itself, durably" in text
    assert ("it stays blocked across restarts, and even if the site is allowed again in Chrome, until you "
            "click **Allow Lumina to read this site** in the popup") in text
    assert "Chrome's settings can take Lumina's access away, but they cannot give it back" in text
    assert "*Revoked* also means Chrome was seen to no longer grant the site" in text
    assert "no new one starts, even if you allow the site again" not in text, "R4 wording the Allow contradicts"


def test_extension_version_bumped_for_the_r5_1_revoke_contract():
    """A live harness gates on extension_version: Chrome still running the R5
    worker/popup (0.1.5, which lifted the block on a confirmed removal) must
    not pass for this candidate."""
    assert json.loads((EXTENSION / "manifest.json").read_text())["version"] == "0.2.0"
