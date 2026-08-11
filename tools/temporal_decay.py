"""
Temporal Decay Engine for Palace Memory System
Implements exponential forgetting curve: w(t) = e^(-λ × t), t in days

λ (lambda_rate): decay constant in days⁻¹
τ (tau):         time constant in days = 1 / λ

Default λ=0.0083 → ~78% retention after 30 days — gentle, suits a personal assistant.

MB-11 fix: tau used to be computed as 1/(λ×86400) — a fraction of a
MILLISECOND, not the intended timescale of days-to-weeks — while
decay_weight() compared it against a delta_t measured in raw seconds. Any
two closets more than ~1ms apart in age (i.e. every realistic pair) both
collapsed to the 0.01 floor immediately, so sort_by_recency()'s stable
sort silently fell back to load_layer()'s own ORDER BY (wing, room)
instead of doing anything recency-based. tau is now kept in the same
"days" unit lambda_rate is already documented in, with delta_t converted
to days at the point of use in decay_weight() below — no hidden ×86400 for
a future reader to reverse-engineer. Found via tests/test_palace_pinning.py
while building MB-11's session-pinning fix; logged on the murderboard,
fixed as its own standalone pass.
"""

import math
from datetime import datetime
from typing import List, Dict, Any


class TemporalDecayEngine:

    def __init__(self, lambda_rate: float = 0.0083):
        """
        Args:
            lambda_rate: Decay constant per day. Figures below are computed
                         directly from w(t) = e^(-λt), t in days — not
                         hand-estimated.
                         0.0083 → ~78% retention after 30 days (~61% at 60d,
                                  ~47% at 90d) — gentle, default
                         0.05   → ~22% retention after 30 days — aggressive
                         0.5    → ~0.00003% retention after 30 days —
                                  essentially immediate forgetting
        """
        self.lambda_rate = lambda_rate
        self.tau = 1.0 / lambda_rate  # time constant in days

    def decay_weight(self, timestamp: str) -> float:
        """
        Calculate weight for a memory based on its updated_at timestamp.

        Args:
            timestamp: ISO format string e.g. '2024-01-15T14:30:00'

        Returns:
            Float in [0.01, 1.0]. Higher = more recent.
        """
        if not timestamp:
            return 1.0

        try:
            mem_time = datetime.fromisoformat(timestamp).timestamp()
            delta_t_days = (datetime.now().timestamp() - mem_time) / 86400.0
            weight = math.exp(-delta_t_days / self.tau)
            return max(0.01, min(1.0, weight))
        except Exception:
            return 0.5

    def sort_by_recency(self, closets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Sort a list of closet dicts by temporal decay weight, descending.
        Expects each dict to have an 'updated_at' key (already present in load_layer results).
        """
        return sorted(
            closets,
            key=lambda c: self.decay_weight(c.get("updated_at", "")),
            reverse=True
        )


# Module-level singleton — import and use directly. Takes no explicit
# lambda_rate, so it inherits __init__'s class default (0.0083) rather than
# a value visible here — if that default ever changes, this singleton
# follows it automatically with no separate edit needed.
decay_engine = TemporalDecayEngine()