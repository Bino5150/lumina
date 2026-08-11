"""skills/temporal-decay-engine-implementation.md, section 6 --
progressive_decay() "Optional Enhancement" code sample.

This function is documentation-only: it isn't imported anywhere in
tools/ or core/, so there's no production module to import from here.
The implementation below is a VERBATIM copy of the corrected code block
in the markdown doc -- if that block changes, this copy must change with
it, or this test stops meaning anything.

Two bugs fixed in the same pass (both found while building MB-11's
session-pinning test, logged on the murderboard, independent of the
separate tools/temporal_decay.py units fix in commit 6a47513):

1. Same units bug as decay_weight() -- t was raw seconds with a stray
   x86400 in the phase-1 branch. Fixed by converting to t_days once,
   up front.
2. Phase discontinuity -- the old phase 2 always started from a
   hardcoded 0.5 + 0.5*..., assuming phase 1 always left off at 1.0. It
   doesn't (e.g. ~0.497 at the 7-day boundary for lambda_rate=0.1), so
   the curve visibly jumped back up at the phase boundary. Fixed by
   deriving phase 2's starting point (w1) from phase 1's own formula at
   the boundary, and asymptoting toward a named long_term_baseline
   parameter instead of a bare 0.5 literal.
"""
import math
from datetime import datetime, timedelta

import pytest


def progressive_decay(timestamp, lambda_rate=0.1, long_term_baseline: float = 0.1):
    """Verbatim copy of the corrected function in the skill doc -- keep in
    sync with skills/temporal-decay-engine-implementation.md section 6."""
    t_days = (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds() / 86400.0
    phase1_days, phase2_days = 7, 90

    if t_days < phase1_days:
        return math.exp(-lambda_rate * t_days)
    else:
        w1 = math.exp(-lambda_rate * phase1_days)
        return long_term_baseline + (w1 - long_term_baseline) * math.exp(-((t_days - phase1_days) / phase2_days) ** 0.8)


def _progressive_decay_days(t_days, lambda_rate=0.1, long_term_baseline=0.1, phase1_days=7, phase2_days=90):
    """Same formula, taking t_days directly instead of an ISO timestamp --
    lets tests probe exact boundary/sweep points without fighting
    wall-clock timing noise from datetime.now()."""
    if t_days < phase1_days:
        return math.exp(-lambda_rate * t_days)
    else:
        w1 = math.exp(-lambda_rate * phase1_days)
        return long_term_baseline + (w1 - long_term_baseline) * math.exp(-((t_days - phase1_days) / phase2_days) ** 0.8)


def _timestamp_days_ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


# ── Continuity at the phase boundary (the actual bug) ────────────────────

@pytest.mark.parametrize("lambda_rate", [0.05, 0.1, 0.15])
def test_continuous_at_phase_boundary(lambda_rate):
    """The bug: phase 1 landed at exp(-lambda_rate*7) just before the
    boundary, phase 2 jumped to 1.0 just after it. Both formulas must now
    agree at t_days=7 exactly (phase 2's branch is used there, by the
    strict '<' comparison, but its value must equal what phase 1's own
    formula would give)."""
    phase1_formula_at_boundary = math.exp(-lambda_rate * 7)
    phase2_branch_at_boundary = _progressive_decay_days(7.0, lambda_rate=lambda_rate)

    assert phase2_branch_at_boundary == pytest.approx(phase1_formula_at_boundary, rel=1e-9)


@pytest.mark.parametrize("lambda_rate", [0.05, 0.1, 0.15])
def test_no_jump_crossing_the_boundary(lambda_rate):
    """Approaching from just below and just above t_days=7 must land on
    nearly the same value -- not the old ~0.5 -> 1.0 jump."""
    just_below = _progressive_decay_days(7 - 1e-6, lambda_rate=lambda_rate)
    just_above = _progressive_decay_days(7 + 1e-6, lambda_rate=lambda_rate)

    assert just_above == pytest.approx(just_below, abs=1e-5)


# ── Monotonicity across the full range (no second discontinuity) ─────────

@pytest.mark.parametrize("lambda_rate", [0.05, 0.1, 0.15])
def test_monotonically_non_increasing_across_two_years(lambda_rate):
    n = 2000
    t_values = [i * (730 / n) for i in range(n + 1)]
    weights = [_progressive_decay_days(t, lambda_rate=lambda_rate) for t in t_values]

    for i in range(len(weights) - 1):
        assert weights[i + 1] <= weights[i] + 1e-9, \
            f"weight rose between t={t_values[i]:.3f}d and t={t_values[i+1]:.3f}d"


# ── Asymptotic approach to long_term_baseline ─────────────────────────────

def test_settles_near_baseline_at_long_horizon():
    w1 = math.exp(-0.1 * 7)
    weight_at_2_years = _progressive_decay_days(730, lambda_rate=0.1, long_term_baseline=0.1)

    assert weight_at_2_years == pytest.approx(0.101988, abs=1e-5)
    assert 0.1 < weight_at_2_years < w1  # strictly between baseline and the phase-1 boundary value


def test_baseline_is_a_named_parameter_not_a_hidden_constant():
    """The old 0.5, 0.5 pair had no way to be anything but 0.5 -- confirm
    the corrected version actually respects a caller-supplied baseline."""
    weight_default = _progressive_decay_days(730, lambda_rate=0.1, long_term_baseline=0.1)
    weight_custom = _progressive_decay_days(730, lambda_rate=0.1, long_term_baseline=0.3)

    assert weight_custom > weight_default
    assert weight_custom == pytest.approx(0.3, abs=0.02)


# ── Checkpoint table in the doc's Verification section ────────────────────

@pytest.mark.parametrize("days,expected", [
    (0, 1.000000),
    (1, 0.904837),
    (3, 0.740818),
    (7, 0.496585),
    (14, 0.448371),
    (30, 0.383486),
    (60, 0.306070),
    (90, 0.255340),
    (365, 0.119394),
    (730, 0.101988),
])
def test_matches_documented_checkpoint_table(days, expected):
    weight = _progressive_decay_days(days, lambda_rate=0.1, long_term_baseline=0.1)
    assert weight == pytest.approx(expected, abs=1e-5)


# ── The ISO-timestamp entry point (ensures the wrapper is wired right) ───

def test_iso_timestamp_entry_point_matches_days_based_computation():
    """Confirms progressive_decay()'s public (timestamp-based) signature
    produces the same result as the days-based helper used for the
    boundary/sweep tests above -- not just that the internal math is
    correct in isolation."""
    days = 30
    weight_from_timestamp = progressive_decay(_timestamp_days_ago(days), lambda_rate=0.1)
    weight_from_days = _progressive_decay_days(days, lambda_rate=0.1)

    assert weight_from_timestamp == pytest.approx(weight_from_days, abs=1e-3)


def test_iso_timestamp_entry_point_does_not_need_float_conversion():
    """Regression guard against the separate float(timestamp) bug found
    in decay_weight()'s doc sample -- progressive_decay() already took an
    ISO string via datetime.fromisoformat(), confirmed here rather than
    just asserted in the doc's prose."""
    timestamp = datetime.now().isoformat()
    weight = progressive_decay(timestamp, lambda_rate=0.1)
    assert weight == pytest.approx(1.0, abs=1e-3)
