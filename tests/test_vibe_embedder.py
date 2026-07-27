"""Unit tests for the embedding-based vibe resolver.

No live API calls — we seed a deterministic cache in-place and stub
``_embed_query`` so cosine similarity is the only thing being tested.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from app.bot import vibe_embedder as ve


def _unit(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


@pytest.fixture
def seeded_cache(monkeypatch):
    """Install a tiny hand-built cache with orthogonal-ish vectors.

    Vectors are in 4-D for readability — resolver doesn't care about
    dimensionality as long as query and stored vectors match.
    """
    cache = {
        "schema_version": 1,
        "model": "text-embedding-004",
        "output_dim": 4,
        "taxonomy_hash": "deadbeef",
        "vibe_count": 4,
        "normalised": True,
        "descriptors": {},
        "embeddings": {
            "gym_fitness":     _unit([1.0, 0.1, 0.0, 0.0]),
            "book_lover":      _unit([0.0, 1.0, 0.1, 0.0]),
            "coffee_lover":    _unit([0.0, 0.0, 1.0, 0.1]),
            "home_decor_modern": _unit([0.0, 0.0, 0.0, 1.0]),
        },
    }
    monkeypatch.setattr(ve, "_cache", cache)
    return cache


async def _run(coro):
    return await coro


def test_resolver_reports_ready_when_cache_present(seeded_cache):
    assert ve.is_ready()
    status = ve.cache_status()
    assert status["ready"] is True
    assert status["vibe_count"] == 4


def test_resolve_returns_top_match_above_threshold(seeded_cache, monkeypatch):
    """A query vector aligned with gym_fitness should resolve to it."""
    async def fake_embed(text):
        return _unit([1.0, 0.05, 0.0, 0.0])
    monkeypatch.setattr(ve, "_embed_query", fake_embed)

    out = asyncio.run(ve.resolve("gym buddy"))
    assert out == ["gym_fitness"], out


def test_resolve_can_return_multiple_when_query_straddles(seeded_cache, monkeypatch):
    """A query equidistant to two vibes must surface both under top_k=3."""
    async def fake_embed(text):
        # Halfway between gym_fitness and book_lover.
        return _unit([1.0, 1.0, 0.0, 0.0])
    monkeypatch.setattr(ve, "_embed_query", fake_embed)

    out = asyncio.run(ve.resolve("fitness and reading"))
    assert "gym_fitness" in out
    assert "book_lover" in out
    # home_decor_modern is orthogonal → must NOT appear
    assert "home_decor_modern" not in out


def test_resolve_respects_threshold(seeded_cache, monkeypatch):
    """All-orthogonal query should drop below threshold and return []."""
    async def fake_embed(text):
        return _unit([0.3, 0.3, 0.3, 0.3])  # equidistant, low cosine
    monkeypatch.setattr(ve, "_embed_query", fake_embed)

    out = asyncio.run(ve.resolve("something generic", threshold=0.9))
    assert out == []


def test_resolve_respects_top_k(seeded_cache, monkeypatch):
    """Even if everything clears the threshold, only top_k are returned."""
    async def fake_embed(text):
        return _unit([1.0, 1.0, 1.0, 1.0])  # above threshold for all
    monkeypatch.setattr(ve, "_embed_query", fake_embed)

    out = asyncio.run(ve.resolve("everything", top_k=2, threshold=0.0))
    assert len(out) == 2


def test_resolve_is_safe_when_cache_missing(monkeypatch):
    """No cache → empty list, no exception. Callers rely on this
    contract to fall through to the keyword fallback without try/except
    in the hot path."""
    monkeypatch.setattr(ve, "_cache", None)
    out = asyncio.run(ve.resolve("gym"))
    assert out == []


def test_resolve_is_safe_when_embed_fails(seeded_cache, monkeypatch):
    """Embedding API failure → []. _embed_query already swallows
    exceptions and returns None; resolve must treat that as no-op."""
    async def fake_embed(text):
        return None
    monkeypatch.setattr(ve, "_embed_query", fake_embed)
    out = asyncio.run(ve.resolve("gym"))
    assert out == []


def test_resolve_returns_only_canonical_vibe_ids(seeded_cache, monkeypatch):
    """Whatever the resolver emits MUST be a key in the cache — callers
    pass the result straight into downstream code that assumes canonical
    IDs. Never fabricate a new ID from the embedding."""
    async def fake_embed(text):
        return _unit([0.9, 0.1, 0.0, 0.0])
    monkeypatch.setattr(ve, "_embed_query", fake_embed)

    out = asyncio.run(ve.resolve("gym", threshold=0.0, top_k=10))
    for vid in out:
        assert vid in seeded_cache["embeddings"]


def test_cosine_prenorm_is_symmetric():
    """Sanity check on the private cosine helper."""
    a = _unit([1.0, 0.0])
    b = _unit([0.0, 1.0])
    assert abs(ve._cosine_prenorm(a, b)) < 1e-9
    assert abs(ve._cosine_prenorm(a, a) - 1.0) < 1e-9
