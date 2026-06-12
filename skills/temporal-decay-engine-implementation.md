# Skill: Temporal Decay Engine Implementation

## Procedure

### 1. Define the Decay Constant (λ)
Choose your λ based on desired retention:
- **0.1/day** (~67% retention after 30 days, ~25% after 90 days) - Moderate forgetting
- **0.15/day** (~48% retention after 30 days, ~15% after 90 days) - Fast forgetting (good for short-term context)
- **0.05/day** (~86% retention after 30 days) - Slow forgetting (for long-term memory preservation)

### 2. Calculate Weights per Memory
```python
import math
def decay_weight(timestamp, lambda_rate=0.1):
    current_time = datetime.now().timestamp()
    mem_time = float(timestamp)
    delta_t = current_time - mem_time  # seconds
    tau = 1.0 / (lambda_rate * 86400)  # Convert to seconds
    weight = math.exp(-delta_t / tau)
    return max(0.01, min(1.0, weight))  # Clamp to [0.01, 1]
```

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

Test with known timestamps:
```python
# Should see: 0 (today), ~0.96 (1 day ago), ~0.82 (3 days ago)
w = decay_weight("2024-12-19T10:00:00")
w = decay_weight("2024-12-18T10:00:00")  # 1 day
w = decay_weight("2024-12-16T10:00:00")  # 3 days
```

**Expected:** ~1.0, ~0.958, ~0.741 (for λ=0.1)

## Use Cases

- **Contextual awareness:** Weight recent conversations higher when answering questions
- **Session management:** Auto-archive memories below weight threshold (e.g., < 0.1) to save space
- **Personalization:** Update λ based on user behavior (frequent revisits = reduce λ for that category)
- **Search ranking:** Combine with TF-IDF or semantic similarity scores using weights as confidence multipliers