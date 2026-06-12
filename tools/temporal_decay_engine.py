#!/usr/bin/env python3
"""
Temporal Decay Engine for Palace Memory System
Implements exponential forgetting curve: w(t) = e^(-\u03bb \u00d7 t)

Where:
- \u03bb (lambda): decay constant, typically 0.1/day or similar
- t: time elapsed since memory creation
"""
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import json

class TemporalDecayEngine:
    """
    Manages temporal decay weights for memory palace retrievals.
    
    Uses exponential decay function to gradually reduce weight of older memories,
    while keeping recent ones prominent in retrieval operations.
    """
    def __init__(self, lambda_rate: float = 0.1):
        """
        Initialize temporal decay engine.
        
        Args:
            lambda_rate: Decay constant (per day). Higher = faster forgetting.
                        0.1 = ~67% retention after 30 days
                        0.5 = ~94% retention after 30 days (slower)
        """
        self.lambda_rate = lambda_rate
        # Convert to seconds for calculations: t in days \u2192 86400 seconds/day
        self.tau = 1.0 / (lambda_rate * 86400)  # Time constant in seconds
    
    def decay_weight(self, timestamp: str) -> float:
        """
        Calculate current weight for a memory based on its creation time.
        
        Args:
            timestamp: ISO format datetime string (e.g., '2024-01-15T14:30:00')
                      or Unix timestamp
        
        Returns:
            Weight between 0 and 1. Higher = more recent.
        """
        if not timestamp or isinstance(timestamp, (int, float)):
            # Treat as immediate/recent memory
            return 1.0
            
        try:
            current_time = datetime.now().timestamp()
            mem_time = float(timestamp) if isinstance(timestamp, str) else timestamp
            delta_t = current_time - mem_time  # seconds elapsed
            
            # Exponential decay: e^(-t/tau)
            weight = math.exp(-delta_t / self.tau)
            return max(0.01, min(1.0, weight))  # Clamp to [0.01, 1]
        except Exception as e:
            print(f"[Temporal Decay] Error calculating weight for {timestamp}: {e}")
            return 0.5
    
    def weighted_score(self, memories: List[Dict[str, Any]]) -> Dict[int, float]:
        """
        Calculate temporal decay weights for a list of memories.
        
        Args:
            memories: List of dicts with 'id' and optionally 'timestamp'
                     or 'created_at' key
        
        Returns:
            Dict mapping memory IDs to their normalized weights
        """
        if not memories:
            return {}
            
        # Calculate raw weights
        raw_weights = [(m['id'], self.decay_weight(m.get('timestamp', m.get('created_at', ''))))
                       for m in memories]
        
        # Normalize to sum=1 (softmax-like)
        total = sum(w for _, w in raw_weights)
        if total == 0:
            return {m[0]: 1.0/len(memories) for m in raw_weights}
            
        normalized = {mem_id: w / total for mem_id, w in raw_weights}
        return normalized
    
    def retrieval_filter(self, memories: List[Dict[str, Any]], min_weight: float = 0.1,
                        max_n: int = None) -> Tuple[List[int], List[float]]:
        """
        Filter memories by temporal weight and return ranked list.
        
        Returns:
            Tuples of (memory ID, normalized weight)
        """
        # Calculate weights
        weights = self.weighted_score(memories)
        
        # Filter by minimum threshold and sort by relevance
        filtered = [(m['id'], w) for m in memories if w >= min_weight]
        return sorted(filtered, key=lambda x: -x[1])
    
    def weighted_aggregation(self, scores: List[float], memory_ids: List[str]) -> Dict[int, float]:
        """
        Aggregate multiple retrieval results with temporal weighting.
        
        Use case: Multiple queries return overlapping memories, apply weights
        to aggregate consensus over time.
        """
        # Create weight lookup
        weights = {m[0]: self.decay_weight(m[1]) if len(m) > 1 else m[0] for m in scores}
        
        # Aggregate by memory ID, applying weights as confidence
        aggregated = {}
        total_confidence = sum(weights.values())
        
        for score, mem_id in zip(scores, memory_ids):
            w = weights.get(mem_id, 0)
            if w > 0:
                aggregated[mem_id] = aggregated.get(mem_id, 0) + (score * w / total_confidence)
        
        return {k: v/len(aggregated) for k, v in aggregated.items()}

# Expose globally for Lumina to use
decay_engine = TemporalDecayEngine()

def register_temporal_decay_engine_tool(registry):
    """Register tool with the registry so it can be called."""
    registry.register('temporal_decay', decay_engine)

if __name__ == "__main__":
    # Demo usage
    engine = TemporalDecayEngine(lambda_rate=0.15)  # ~68% retention after 30 days
    
    test_timestamps = [
        "2024-12-19T10:00:00",  # Today (baseline)
        "2024-12-15T10:00:00",  # 4 days ago
        "2024-12-01T10:00:00",  # 18 days ago
        "2024-11-01T10:00:00",  # 48 days ago
    ]
    
    print("Temporal Decay Weights (\u03bb=0.15/day):")
    print(f"{'Days':<6} {'Weight':<12} {'Retention %':<12}")
    print("-" * 34)
    for ts in test_timestamps:
        days_ago = (datetime.now() - datetime.fromisoformat(ts)).days
        w = engine.decay_weight(ts)
        print(f"{days_ago:<6} {w:.2f}<{10} {w*100:.1f}%")
    
    # Demo retrieval simulation
    memories = [
        {'id': 1, 'timestamp': '2024-12-19T10:00:00'},
        {'id': 2, 'timestamp': '2024-12-15T10:00:00'},
        {'id': 3, 'timestamp': '2024-12-01T10:00:00'},
        {'id': 4, 'timestamp': '2024-11-01T10:00:00'},
    ]
    
    filtered = engine.retrieval_filter(memories)
    print(f"\nRetrieval ranking (top 3):")
    for mem_id, weight in filtered[:3]:
        w_norm = engine.weighted_score([{'id': mem_id}])[mem_id]
        print(f"ID: {mem_id:<2} | Weight: {w_norm:.4f}")