"""FINGER-STALE-DOC: Stage 2 FINGER bound to the extension's per-document identity.

Real Chrome 155 runs executeScript({documentIds:[STALE]}) in the tab's NEW
document after a cross-document navigation (reproduced in the campaign's
real-Chrome harness; a same-URL reload IS refused). For FINGER a same-URL
new document would pass the URL and unique-link checks, so the resolver and
the fixed actuator now refuse in any document whose isolated-world identity
differs from the one get_links delivered from. The behaviour is proven in
tests/chrome_companion_js/worker_test.js ("FINGER-STALE-DOC ...") and the
real worker+host+hub test; these are static guards on the single mechanism.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1] / "chrome_companion" / "extension" / "worker.js"
DOC_KEY = "__luminaCompanionDocument_v1"


def _source():
    return WORKER.read_text(encoding="utf-8")


def _function(name):
    worker = _source()
    start = worker.index(f"function {name}(")
    return worker[start:worker.index("\n}\n", start)]


def test_one_identity_mechanism_minted_in_one_shape():
    """Exactly the two first-contact readers mint it; no competing nonce."""
    worker = _source()
    injected = ["extractVisibleText", "extractLinks", "resolveLinkTarget", "dispatchVerifiedLink",
                "observePage", "resolveRefTarget", "dispatchRefLink"]
    minters = [name for name in injected if "crypto.getRandomValues" in _function(name)]
    assert minters == ["extractLinks", "observePage"], minters
    for minting in ("extractLinks", "observePage"):
        body = _function(minting)
        assert f'const DOC_KEY = "{DOC_KEY}";' in body
        assert "globalThis[DOC_KEY] = Object.freeze({ nonce:" in body, minting
    observe = _function("observePage")
    assert "nonce: globalThis[DOC_KEY].nonce," in observe, "the element registry adopts the document identity"
    assert "nonceBytes" not in worker, "the Slice 1 registry-only nonce is gone"


@pytest.mark.parametrize("name", ["resolveLinkTarget", "dispatchVerifiedLink"])
def test_finger_checks_document_identity_before_anything_else(name):
    body = _function(name)
    check = body.index("claim.document_nonce")
    for later in ("querySelectorAll", "location.assign", "location.href !== claim.source_url"):
        if later in body:
            assert check < body.index(later), f"{name}: identity checked before {later}"


@pytest.mark.parametrize("name", ["resolveRefTarget", "dispatchRefLink"])
def test_element_refs_use_the_same_identity(name):
    body = _function(name)
    assert "reg.nonce !== claim.document_nonce" in body
    assert "claim.registry" not in body


def test_the_identity_is_never_part_of_a_wire_result():
    worker = _source()
    get_links = worker[worker.index("  async get_links(req, live) {"):]
    get_links = get_links[:get_links.index("\n  },\n")]
    result = re.search(r"result: \{([^}]*)\}", get_links).group(1)
    assert "nonce" not in result, "get_links' wire result carries no identity"
    assert "documentNonce: page.document_nonce" in get_links, "kept privately on the outcome"
    from chrome_companion import protocol
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_result("get_links", {"links": [], "total_links": 0, "document_nonce": "a" * 32})
