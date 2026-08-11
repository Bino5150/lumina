# Skill: Temporal Decay Engine Implementation

## Procedure

### 1. Define the Decay Constant (λ)
Choose your λ based on desired retention. Figures below are computed directly from
`w(t) = e^(-λt)`, t in days -- not hand-estimated (the previous version of this list wasn't,
and every figure in it was off by an order of magnitude or more):
- **0.05/day** (~22% retention after 30 days) - Slowest of the three, still fairly aggressive
- **0.1/day** (~5.0% retention after 30 days, ~0.01% after 90 days) - Fast forgetting
- **0.15/day** (~1.1% retention after 30 days, ~0.0001% after 90 days) - Very fast forgetting (short-term context only)

For gentler, long-term-preservation retention (e.g. ~78% after 30 days), use a much smaller λ
such as **0.0083/day** -- see `tools/temporal_decay.py`'s shipped default for a worked example.

### 2. Calculate Weights per Memory
```python
import math
from datetime import datetime

def decay_weight(timestamp, lambda_rate=0.1):
    current_time = datetime.now().timestamp()
    mem_time = datetime.fromisoformat(timestamp).timestamp()
    delta_t_days = (current_time - mem_time) / 86400.0  # seconds -> days
    tau = 1.0 / lambda_rate  # time constant in days -- NOT seconds
    weight = math.exp(-delta_t_days / tau)
    return max(0.01, min(1.0, weight))  # Clamp to [0.01, 1]
```
(`timestamp` is an ISO-format string, e.g. `datetime.now().isoformat()` -- matches the
Verification section's calls below and `tools/temporal_decay.py`'s real implementation. A
previous version of this sample used `float(timestamp)`, which the Verification section's own
ISO-string example calls would have raised `ValueError` against.)

A previous version of this sample also computed `tau = 1.0 / (lambda_rate * 86400)` and divided a
raw-seconds `delta_t` by it -- that puts `tau` at sub-millisecond scale, so any two timestamps
more than ~1ms apart both collapse to the 0.01 floor immediately, regardless of actual age. Keep
`tau` in the same "days" unit `lambda_rate` is documented in, and convert `delta_t` to days at
the point of use, as above.

### 3. Normalize Across Memory Set (Softmax-like)
```python
def weighted_scores(memories):
    raw_weights = [(m['id'], decay_weight(m.get('timestamp', ''))) for m in memories]
    total = sum(w for _, w in raw_weights)
    return {mem_id: w/total for mem_id, w in raw_weights}
```

### 4. Integration with Palace System
Add timestamp metadata to every `palace_remember()` call:
```python
content = f"{user_input}\n[timestamp]: {datetime.now().isoformat()}"
palace_remember(content=content, wing="sessions", layer=2)
```

### 5. Weighted Retrieval Querying
```python
def temporal_query(wings=["identity", "projects"]):
    results = []
    for wing in wings:
        mems = get_memories(wing)
        weights = weighted_scores(mems)
        ranked = sorted(mems, key=lambda m: weights[m['id']], reverse=True)
        # Filter by min_weight if needed
```

### 6. Progressive Decay (Optional Enhancement)
For more realistic biological forgetting:
```python
def progressive_decay(timestamp, lambda_rate=0.1, long_term_baseline: float = 0.1):
    """
    Implements Ebbinghaus forgetting curve approximation.
    Phase 1 (first week): plain exponential decay, e^(-lambda_rate * t_days) --
        same formula decay_weight() uses.
    Phase 2 (after the first week): power-law approach to long_term_baseline,
        starting exactly where phase 1 left off, not a fresh curve pinned to 1.0.
    long_term_baseline: the floor phase 2 asymptotically approaches as t -> infinity.
        A real parameter, not a hidden constant -- distinct from decay_weight()'s
        hard 0.01 clamp (an absolute minimum), this is a meaningful "memories that
        persist past the initial forgetting window settle here" asymptote. Must be
        less than phase 1's value at the boundary (exp(-lambda_rate * phase1_days))
        for the curve to stay monotonically decreasing -- true for any of this doc's
        example lambda_rate values (0.05/0.1/0.15) against the default 0.1, but not
        guaranteed for a much larger lambda_rate (e.g. lambda_rate > ~0.33 with this
        default baseline would make phase 1's boundary value drop below the baseline
        itself, so phase 2 would rise instead of decay).
    """
    t_days = (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds() / 86400.0
    phase1_days, phase2_days = 7, 90  # first week is exponential; 90-day scale for the tail

    if t_days < phase1_days:
        return math.exp(-lambda_rate * t_days)
    else:
        w1 = math.exp(-lambda_rate * phase1_days)  # phase 1's own value at the boundary
        return long_term_baseline + (w1 - long_term_baseline) * math.exp(-((t_days - phase1_days) / phase2_days) ** 0.8)
```
Two bugs fixed here, both found the same session as `decay_weight()`'s units bug (commit
`6a47513`) but logged and fixed separately since this is a different, deeper issue:

1. **Same units bug as `decay_weight()`** -- `t` used to be raw seconds, multiplied by a stray
   `86400` inside the phase-1 branch. Converting `t` to `t_days` once, up front, removes the need
   for any `86400` factor inside either branch.
2. **Phase discontinuity** (the actual reason this needed more than a units patch) -- the old
   phase 2 always started from a hardcoded `0.5 + 0.5 * ...`, i.e. it assumed phase 1 always left
   off at exactly `1.0`. It doesn't: for this doc's own `lambda_rate=0.1` example, phase 1 is
   already down to `exp(-0.1*7) ≈ 0.497` by the 7-day boundary. The old code jumped from `~0.497`
   (phase 1, just before the boundary) straight to `1.0` (phase 2, just after it) -- the curve
   went back UP before continuing to decay. Phase 2 above starts from `w1` (phase 1's own value at
   the boundary) and decays toward `long_term_baseline` instead, so the two phases connect exactly
   at `t_days = phase1_days` by construction.

(`progressive_decay()`'s `timestamp` parameter was already an ISO-format string via
`datetime.fromisoformat(timestamp)` -- it didn't have the separate `float(timestamp)` bug
`decay_weight()`'s sample had; confirmed, not just assumed, while fixing the two bugs above.)

## Pitfalls

**Don't forget to handle missing timestamps:** If a memory has no timestamp, treat as recent (weight=1.0).

**Avoid over-weighting recency:** If you query "what did I learn?" after 3 days, very old memories will be filtered out completely. Use `min_weight=0.1` to keep distant but relevant context.

**Be careful with aggregation:** When combining results from different queries that overlap in memory IDs, normalize by total confidence so recent repeated access doesn't artificially boost older content.

## Verification

Test with known timestamps (regenerated from the corrected function above, not hand-estimated --
the previous version of this section had two different expected-value sets that didn't even
agree with each other, and neither matched λ=0.1's actual output):
```python
from datetime import datetime, timedelta

now = datetime.now()
w_today     = decay_weight(now.isoformat())                          # 0 days
w_1day_ago  = decay_weight((now - timedelta(days=1)).isoformat())    # 1 day
w_3days_ago = decay_weight((now - timedelta(days=3)).isoformat())    # 3 days
```

**Expected (for λ=0.1):** `w_today` ≈ 1.0, `w_1day_ago` ≈ 0.905, `w_3days_ago` ≈ 0.741 --
directly from `e^(-0.1*0)`, `e^(-0.1*1)`, `e^(-0.1*3)`.

### `progressive_decay()` checkpoints (λ=0.1, long_term_baseline=0.1, default phase windows)

Computed directly from the corrected function above -- these did not exist for the previous,
discontinuous version, since the bug meant no consistent set of checkpoints could have existed
in the first place.

| t_days | weight | % |
|---|---|---|
| 0 | 1.000000 | 100.00% |
| 1 | 0.904837 | 90.48% |
| 3 | 0.740818 | 74.08% |
| 7 (phase boundary) | 0.496585 | 49.66% |
| 14 | 0.448371 | 44.84% |
| 30 | 0.383486 | 38.35% |
| 60 | 0.306070 | 30.61% |
| 90 | 0.255340 | 25.53% |
| 365 | 0.119394 | 11.94% |
| 730 | 0.101988 | 10.20% |

The `t=7` row is the same value whether computed via phase 1's formula (`e^(-0.1*7)`, evaluated
just below the boundary) or phase 2's (evaluated just above it) -- that agreement is the actual
fix; the old version jumped to `1.0` there instead. Weight keeps decreasing (never rises) at every
step past that, settling toward `long_term_baseline=0.1` as t grows -- by `t=730` it's within
~0.02 of the asymptote.

## Use Cases

- **Contextual awareness:** Weight recent conversations higher when answering questions
- **Session management:** Auto-archive memories below weight threshold (e.g., < 0.1) to save space
- **Personalization:** Update λ based on user behavior (frequent revisits = reduce λ for that category)
- **Search ranking:** Combine with TF-IDF or semantic similarity scores using weights as confidence multipliers