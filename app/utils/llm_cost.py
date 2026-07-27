"""LLM cost estimation based on token usage and model pricing.

All prices are per-million-tokens. Easily updatable when pricing changes.
Caching costs are handled separately (usually lower per cached token).
"""

from decimal import Decimal
from typing import Optional

# Model pricing ($/1M tokens) — update these when Google/DeepSeek change pricing
# Source: ai.google.dev/pricing and platform.deepseek.com (as of May 2026, approximate)
MODEL_PRICING = {
    # Gemini 2.5 Flash (main assistant)
    "gemini-2.5-flash": {
        "provider": "google_genai",
        "input_cost_per_1m": Decimal("0.15"),
        "output_cost_per_1m": Decimal("0.60"),
        "cache_hit_cost_per_1m": Decimal("0.037"),  # ~25% of input cost
    },
    # Gemini 2.5 Flash Lite (classifier)
    "gemini-2.5-flash-lite": {
        "provider": "google_genai",
        "input_cost_per_1m": Decimal("0.10"),
        "output_cost_per_1m": Decimal("0.40"),
        "cache_hit_cost_per_1m": Decimal("0.025"),
    },
    # Gemini Text Embedding (fallback vibe resolver)
    "text-embedding-004": {
        "provider": "google_genai",
        "input_cost_per_1m": Decimal("0.00002"),
        "output_cost_per_1m": Decimal("0.0"),  # No output cost for embeddings
        "cache_hit_cost_per_1m": Decimal("0.0"),
    },
    "gemini-embedding-001": {
        "provider": "google_genai",
        "input_cost_per_1m": Decimal("0.15"),
        "output_cost_per_1m": Decimal("0"),
        "cache_hit_cost_per_1m": Decimal("0"),
    },
    # DeepSeek Chat v3 (fallback classifier)
    "deepseek-chat": {
        "provider": "deepseek",
        "input_cost_per_1m": Decimal("0.27"),
        "output_cost_per_1m": Decimal("1.10"),
        "cache_hit_cost_per_1m": Decimal("0.07"),
    },
}


def estimate_cost(
    model: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_hit_tokens: Optional[int] = None,
    cache_miss_tokens: Optional[int] = None,
) -> Decimal:
    """Estimate USD cost for one LLM call.

    Args:
        model: Model name (e.g. 'gemini-2.5-flash')
        input_tokens: Non-cached input tokens
        output_tokens: Output tokens (or thinking tokens for some models)
        cache_hit_tokens: Tokens served from cache (Gemini implicit caching)
        cache_miss_tokens: Cache misses (rare, usually None)

    Returns:
        Estimated cost in USD as Decimal (e.g., Decimal("0.00123"))
        Raises ValueError if model is not in MODEL_PRICING (pricing undefined).
        Returns Decimal(0) only if model is known but no tokens were used.

    Logic:
      - Input cost = (input_tokens + cache_miss_tokens) * input_rate
                   + cache_hit_tokens * cache_hit_rate
      - Output cost = output_tokens * output_rate
      - If caching is provided, it's cheaper than input cost.
    """
    if model not in MODEL_PRICING:
        raise ValueError(f"Unknown model: {model}. Add pricing to MODEL_PRICING.")

    pricing = MODEL_PRICING[model]
    cost = Decimal("0")

    # Handle None token counts gracefully
    input_count = input_tokens or 0
    output_count = output_tokens or 0
    cache_hit_count = cache_hit_tokens or 0
    cache_miss_count = cache_miss_tokens or 0

    # Input cost: regular + cache miss at full rate, cache hit at reduced rate
    if input_count > 0 or cache_miss_count > 0:
        regular_input_cost = (input_count + cache_miss_count) * pricing["input_cost_per_1m"] / Decimal("1_000_000")
        cost += regular_input_cost

    if cache_hit_count > 0:
        cache_cost = cache_hit_count * pricing["cache_hit_cost_per_1m"] / Decimal("1_000_000")
        cost += cache_cost

    # Output cost
    if output_count > 0:
        output_cost = output_count * pricing["output_cost_per_1m"] / Decimal("1_000_000")
        cost += output_cost

    return cost


def get_model_provider(model: str) -> Optional[str]:
    """Return the provider for a model (e.g., 'google_genai' for gemini models)."""
    if model not in MODEL_PRICING:
        return None
    return MODEL_PRICING[model].get("provider")
