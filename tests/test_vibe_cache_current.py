"""The committed vibe embedding cache must match the live taxonomy.

Production (30 Jul 2026) ran for an unknown period with `mobile_gaming` absent
from the cache: gaming asks resolved no vibe and suggest_gifts failed, while the
only signal was a warning nobody was reading. The cache is a committed artifact,
so drift is detectable at test time — fail here instead of degrading silently.

Fix a failure by re-running:

    GEMINI_API_KEY=... python scripts/build_vibe_embeddings.py

and committing the updated app/bot/vibe_embeddings.json.
"""

from app.bot import vibe_embedder


def test_vibe_cache_matches_live_taxonomy():
    drift = vibe_embedder.taxonomy_drift()
    assert drift is None, (
        f"vibe embedding cache is stale ({drift}). "
        "Re-run scripts/build_vibe_embeddings.py and commit the JSON."
    )


def test_cache_loaded_and_ready():
    """A cache that fails to load makes every gift suggestion fall back."""
    assert vibe_embedder.is_ready(), (
        f"vibe cache did not load: {vibe_embedder._cache_load_err}"
    )


def test_live_hash_is_stable():
    """The hash must be deterministic, or every load reports false drift."""
    assert vibe_embedder.live_taxonomy_hash() == vibe_embedder.live_taxonomy_hash()
