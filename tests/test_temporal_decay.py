"""tools/temporal_decay.py — MB-11 follow-up: tau units fix.

tau used to be computed as 1/(lambda_rate*86400) -- a fraction of a
millisecond -- while decay_weight() compared it against delta_t measured
in raw seconds. Any two closets more than ~1ms apart in age both
collapsed to the 0.01 floor immediately, so sort_by_recency() silently
degraded into load_layer()'s own ORDER BY (wing, room) instead of doing
anything recency-based. Found while building MB-11's session-pinning test
(tests/test_palace_pinning.py).

Fix: tau is now kept in the same "days" unit lambda_rate is documented in
(tau = 1/lambda_rate), with delta_t converted to days at the point of use
in decay_weight() rather than folding an 86400 factor into tau itself.
"""
import math
from datetime import datetime, timedelta

import pytest

from tools.temporal_decay import TemporalDecayEngine, decay_engine


def _timestamp_days_ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


# ── Default construction ─────────────────────────────────────────────────

def test_default_lambda_rate_is_0_0083():
    engine = TemporalDecayEngine()
    assert engine.lambda_rate == 0.0083


def test_tau_is_in_days_not_seconds():
    """The actual units bug: tau used to be 1/(lambda_rate*86400) --
    sub-millisecond. It must now be the reciprocal of lambda_rate, in
    days (~120.5 days for the default 0.0083)."""
    engine = TemporalDecayEngine(lambda_rate=0.0083)
    assert engine.tau == pytest.approx(1.0 / 0.0083)
    assert engine.tau == pytest.approx(120.48, abs=0.01)


def test_module_singleton_inherits_the_new_default():
    assert decay_engine.lambda_rate == 0.0083


# ── decay_weight() against ground-truth w(t) = e^(-lambda*t), t in days ──

def _clamp(weight: float) -> float:
    return max(0.01, min(1.0, weight))


@pytest.mark.parametrize("lambda_rate,days", [
    (0.0083, 30), (0.0083, 60), (0.0083, 90), (0.05, 30), (0.5, 30),
])
def test_decay_weight_matches_the_clamped_formula(lambda_rate, days):
    """Proves the implementation computes w(t)=e^(-lt), t in days, in the
    right units -- not the docstring's numbers specifically (that's the
    separate test below). ground_truth is clamped the same [0.01, 1.0] way
    decay_weight() itself clamps, since the 0.5/30d case's raw value
    (~3e-7) is intentionally below the floor -- that's the floor doing its
    documented job, not a units bug."""
    engine = TemporalDecayEngine(lambda_rate=lambda_rate)
    timestamp = _timestamp_days_ago(days)

    weight = engine.decay_weight(timestamp)

    ground_truth = _clamp(math.exp(-lambda_rate * days))
    assert weight == pytest.approx(ground_truth, rel=1e-3)


@pytest.mark.parametrize("lambda_rate,days,expected_pct", [
    (0.0083, 30, 78),
    (0.0083, 60, 61),
    (0.0083, 90, 47),
    (0.05, 30, 22),
    (0.5, 30, 0.00003),
])
def test_docstring_percentages_match_the_raw_unclamped_formula(lambda_rate, days, expected_pct):
    """Independent of decay_weight()'s 0.01 floor -- these are the
    unclamped w(t)=e^(-lt) values the class docstring's Args block claims.
    Regression guard against the exact failure mode found in the skill
    doc's parallel numbers: hand-written examples that were never actually
    run against the formula. A relative tolerance keeps this meaningful
    all the way down to the 0.00003% case -- a fixed +/-N point band would
    let that one pass against almost anything."""
    raw_pct = math.exp(-lambda_rate * days) * 100
    assert raw_pct == pytest.approx(expected_pct, rel=0.02)


def test_decay_weight_is_not_immediately_clamped_to_floor():
    """Regression guard for the exact bug found: a memory from a few
    milliseconds ago must read as near-1.0 freshness, not fall straight
    through to the 0.01 floor the old sub-millisecond tau produced."""
    timestamp = datetime.now().isoformat()

    weight = decay_engine.decay_weight(timestamp)

    assert weight > 0.99


def test_decay_weight_floor_still_applies_for_very_old_memories():
    timestamp = _timestamp_days_ago(365 * 5)  # 5 years

    weight = decay_engine.decay_weight(timestamp)

    assert weight == 0.01


def test_empty_timestamp_returns_max_weight():
    assert decay_engine.decay_weight("") == 1.0


def test_malformed_timestamp_returns_neutral_default():
    assert decay_engine.decay_weight("not-a-real-timestamp") == 0.5


# ── sort_by_recency(): the actual downstream behavior this bug broke ─────

def test_sort_by_recency_actually_orders_by_real_age_not_alphabetical_fallback():
    """Before the fix, every closet's weight collapsed to the shared 0.01
    floor and sort_by_recency()'s stable sort just preserved input order
    (alphabetical by wing/room, from load_layer()'s SQL). Room names below
    are deliberately in the OPPOSITE order from their actual age, so a
    surviving alphabetical-fallback bug would sort them wrong."""
    closets = [
        {"room": "aaa-oldest", "updated_at": _timestamp_days_ago(90)},
        {"room": "mmm-middle", "updated_at": _timestamp_days_ago(30)},
        {"room": "zzz-newest", "updated_at": _timestamp_days_ago(1)},
    ]

    ordered = decay_engine.sort_by_recency(closets)

    assert [c["room"] for c in ordered] == ["zzz-newest", "mmm-middle", "aaa-oldest"]
