"""
Temporal Decay Engine for Palace Memory System
Implements exponential forgetting curve: w(t) = e^(-λ × t)

λ (lambda_rate): decay constant in days⁻¹
τ (tau):         time constant in seconds = 1 / (λ × 86400)

Default λ=0.05 → ~78% retention after 30 days — gentle, suits a personal assistant.
"""

import math
from datetime import datetime
from typing import List, Dict, Any


class TemporalDecayEngine:

    def __init__(self, lambda_rate: float = 0.05):
        """
        Args:
            lambda_rate: Decay constant per day.
                         0.05 → ~78% retention after 30 days  (gentle, good for palace)
                         0.1  → ~50% retention after 30 days
                         0.5  → ~0.01% retention after 30 days (aggressive)
        """
        self.lambda_rate = lambda_rate
        self.tau = 1.0 / (lambda_rate * 86400)  # time constant in seconds

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
            delta_t = datetime.now().timestamp() - mem_time
            weight = math.exp(-delta_t / self.tau)
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


# Module-level singleton — import and use directly
decay_engine = TemporalDecayEngine()