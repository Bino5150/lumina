"""tools/palace.py — MB-11 Step 4: session-pinning read-side fix.

A closet whose drawers carry pin_tag (e.g. "session:{chat_id}") must
resurface in the L2 injection block regardless of inject_limit and
regardless of how many other, more recent closets exist -- the same
always-present treatment L0/L1 already get. pin_tag=None (the default)
must reproduce prior behavior exactly, since every existing caller that
doesn't know about compaction/session-pinning goes through that path.

Tags live on palace_drawers, not palace_closets (confirmed against
init_palace_db()'s schema) -- _find_pinned_closet_ids() joins through
drawers the same way list_flagged_writes() already does.
"""
import pytest
import config
from tools import palace


@pytest.fixture
def isolated_palace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "palace_test.db"))
    palace.init_palace_db()
    return palace


def _seed(isolated_palace, chat_id):
    """One pinned closet plus 10 unrelated L2 closets. decay_weight()'s tau
    is sub-millisecond given its current constants (lambda_rate=0.05,
    tau = 1/(lambda_rate*86400) seconds) -- any real test-loop timing gap
    clamps every row to the same 0.01 floor, at which point
    sort_by_recency()'s stable sort just preserves load_layer()'s own
    ORDER BY wing, room. Room names are chosen ("other-0".."other-9" <
    "zzzpinned-...") so the intended "pinned is the one that would
    otherwise be excluded" setup holds regardless of which ordering path
    actually fires -- this test must not be sensitive to that timing
    quirk in unrelated code."""
    pin_tag = f"session:{chat_id}"
    isolated_palace.palace_store(
        content="ZZPINNEDMARKERZZ rolling session summary",
        wing="nightstand", room=f"zzzpinned-{chat_id}", layer=2,
        tags=["auto-compaction", pin_tag],
    )
    for i in range(10):
        isolated_palace.palace_store(
            content=f"ZZOTHERMARKERZZ{i} unrelated closet content",
            wing="nightstand", room=f"other-{i}", layer=2,
            tags=["dream-sweep", f"session:{9000 + i}"],
        )
    return pin_tag


def test_pinned_closet_surfaces_despite_inject_limit(isolated_palace):
    pin_tag = _seed(isolated_palace, chat_id=4242)

    block = isolated_palace.build_context_block(max_tokens=5000, inject_limit=3, pin_tag=pin_tag)

    assert "ZZPINNEDMARKERZZ" in block


def test_inject_limit_still_caps_non_pinned_closets(isolated_palace):
    """The pin isn't a backdoor that disables inject_limit globally --
    only the pinned closet is exempt; the other 10 are still capped at 3."""
    pin_tag = _seed(isolated_palace, chat_id=4242)

    block = isolated_palace.build_context_block(max_tokens=5000, inject_limit=3, pin_tag=pin_tag)

    other_marker_count = sum(1 for i in range(10) if f"ZZOTHERMARKERZZ{i}" in block)
    assert other_marker_count == 3


def test_pin_tag_none_reproduces_prior_behavior(isolated_palace):
    """Without a pin_tag (headless/no-session callers, or simply not
    passed), the old closet is just the oldest of 11 -- inject_limit=3
    excludes it exactly like it did before pin_tag existed."""
    _seed(isolated_palace, chat_id=4242)

    block = isolated_palace.build_context_block(max_tokens=5000, inject_limit=3, pin_tag=None)

    assert "ZZPINNEDMARKERZZ" not in block
    other_marker_count = sum(1 for i in range(10) if f"ZZOTHERMARKERZZ{i}" in block)
    assert other_marker_count == 3


def test_pin_tag_omitted_matches_pin_tag_none(isolated_palace):
    """Default parameter value produces identical output to explicit None --
    guards the signature default itself, not just an explicit caller."""
    _seed(isolated_palace, chat_id=4242)

    with_default = isolated_palace.build_context_block(max_tokens=5000, inject_limit=3)
    with_none = isolated_palace.build_context_block(max_tokens=5000, inject_limit=3, pin_tag=None)

    assert with_default == with_none


def test_pinned_closet_still_respects_token_budget(isolated_palace):
    """Pinning bypasses inject_limit, not the token ceiling -- a near-zero
    max_tokens still yields nothing (docstring's stated contract)."""
    pin_tag = _seed(isolated_palace, chat_id=4242)

    block = isolated_palace.build_context_block(max_tokens=1, inject_limit=3, pin_tag=pin_tag)

    assert block == ""


def test_find_pinned_closet_ids_matches_only_exact_tag(isolated_palace):
    pin_tag = _seed(isolated_palace, chat_id=4242)

    ids = isolated_palace._find_pinned_closet_ids(pin_tag)

    assert len(ids) == 1
    # A near-miss tag (different chat_id) must not match via substring luck.
    assert isolated_palace._find_pinned_closet_ids("session:424") == set()
    assert isolated_palace._find_pinned_closet_ids("session:42420") == set()
