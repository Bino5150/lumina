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
def progressive_decay(timestamp, lambda_rate=0.1):
    """
    Implements Ebbinghaus forgetting curve approximation.
    Phase 1: Rapid initial decay
    Phase 2: Slower asymptotic approach to baseline
    """
    t = (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds()
    phase1, phase2 = 3600*24*7, 3600*24*90  # First week, first 90 days
    
    if t < phase1:
        return math.exp(-lambda_rate * 86400 * (t/phase1))  # Exponential in first week
    else:
        # Power law tail for long-term retention
        return 0.5 + 0.5 * math.exp(-((t-phase1)/phase2) ** 0.8)
```

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

## Use Cases

- **Contextual awareness:** Weight recent conversations higher when answering questions
- **Session management:** Auto-archive memories below weight threshold (e.g., < 0.1) to save space
- **Personalization:** Update λ based on user behavior (frequent revisits = reduce λ for that category)
- **Search ranking:** Combine with TF-IDF or semantic similarity scores using weights as confidence multipliers