#!/usr/bin/env python3
"""Offline builder: turn the vibe taxonomy into a pre-computed embedding cache.

Usage:

    GEMINI_API_KEY=... python scripts/build_vibe_embeddings.py

Writes ``app/bot/vibe_embeddings.json`` — a committed, deterministic artifact
used by ``app/bot/vibe_embedder.py`` at runtime. The embedder does NOT call
this script or the embedding API at import; startup is instant.

Re-run this script whenever ``gift_taxonomy.py`` changes (new vibe ID,
renamed vibe, changed brand/vendor list). Commit the updated JSON with the
same PR as the taxonomy change.

The stored vectors are pre-normalised (unit length). Cosine similarity at
runtime reduces to a single dot product per vibe — no divide, no sqrt.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Allow running from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.bot.gift_taxonomy import (  # noqa: E402
    all_vibe_ids,
    VIBE_TO_HUBBLE_BRANDS,
    VIBE_TO_VENDOR_CATEGORIES,
)

# Pin a concrete embedding model. If you bump this, every cached vector
# must be rebuilt — the cache file's `model` field is version-stamped and
# the runtime embedder re-embeds queries with the SAME model name so
# vector spaces match.
#
# As of 2026-04, the stable model on v1beta is `gemini-embedding-001`
# (supports output_dimensionality ∈ {768, 1536, 3072}). The older
# `text-embedding-004` is no longer served via v1beta on most keys.
EMBED_MODEL = "gemini-embedding-001"
OUTPUT_DIM = 768
OUTPUT_PATH = REPO_ROOT / "app" / "bot" / "vibe_embeddings.json"


def _vibe_descriptor(vibe_id: str) -> str:
    """Compose the text we send to the embedding model for a single vibe.

    Pulls brand and vendor-category signal from the taxonomy itself — zero
    manual descriptor authoring. Adding a new vibe means editing only
    ``gift_taxonomy.py``; this descriptor regenerates automatically.

    Human-readable form because the embedding model was trained on natural
    language, not on snake_case identifiers.
    """
    human = vibe_id.replace("_", " ")
    brands = VIBE_TO_HUBBLE_BRANDS.get(vibe_id, [])
    vendors = VIBE_TO_VENDOR_CATEGORIES.get(vibe_id, [])

    bits = [f"{human}."]
    if brands:
        brand_str = ", ".join(brands[:6])
        bits.append(f"Brands: {brand_str}.")
    if vendors:
        bits.append(f"Categories: {', '.join(vendors)}.")
    return " ".join(bits)


def _normalise(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _taxonomy_hash() -> str:
    """Stable fingerprint of the taxonomy state that matters for embeddings.

    If this changes between build and runtime, the embedder warns and falls
    back gracefully instead of returning noise.
    """
    payload = {
        "vibes": sorted(all_vibe_ids()),
        "brands": {k: sorted(v) for k, v in VIBE_TO_HUBBLE_BRANDS.items()},
        "vendors": {k: sorted(v) for k, v in VIBE_TO_VENDOR_CATEGORIES.items()},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY env var is required", file=sys.stderr)
        return 2

    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=api_key)

    vibes = all_vibe_ids()
    print(f"Embedding {len(vibes)} vibes with model={EMBED_MODEL} …")

    embeddings: dict[str, list[float]] = {}
    descriptors: dict[str, str] = {}

    t0 = time.time()
    for i, vid in enumerate(vibes, start=1):
        descriptor = _vibe_descriptor(vid)
        descriptors[vid] = descriptor

        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=descriptor,
            config=genai_types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=OUTPUT_DIM,
            ),
        )
        # SDK returns embeddings[0].values — one per content sent.
        values = list(resp.embeddings[0].values)
        if len(values) != OUTPUT_DIM:
            print(
                f"WARN: {vid} returned {len(values)} dims (expected {OUTPUT_DIM})",
                file=sys.stderr,
            )
        embeddings[vid] = _normalise(values)

        if i % 10 == 0 or i == len(vibes):
            print(f"  {i}/{len(vibes)} done")

    elapsed = time.time() - t0

    artifact = {
        "schema_version": 1,
        "model": EMBED_MODEL,
        "output_dim": OUTPUT_DIM,
        "taxonomy_hash": _taxonomy_hash(),
        "vibe_count": len(vibes),
        "normalised": True,
        "descriptors": descriptors,
        "embeddings": embeddings,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(artifact))
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(
        f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} "
        f"({size_kb:.0f} KB, {len(vibes)} vibes, {elapsed:.1f}s total)"
    )
    print(f"taxonomy_hash={artifact['taxonomy_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
