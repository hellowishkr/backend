"""Gift fulfillment sources — pluggable adapters for suggest_gifts.

Add a new partner by:
1. Implement SourceAdapter in <name>_source.py
2. Register it in provider_registry.py
3. Add brand titles / categories to gift_taxonomy.py
"""

from app.bot.sources.base import (
    GiftCandidate,
    ProviderCapabilities,
    ProviderPolicy,
    ProviderQuote,
    SourceAdapter,
)
from app.bot.sources.hubble_source import HubbleSource
from app.bot.sources.provider_registry import DEFAULT_PROVIDER_REGISTRY, ProviderRegistry, enabled_sources
from app.bot.sources.vendor_source import VendorSource

SOURCES: list[SourceAdapter] = enabled_sources()

__all__ = [
    "GiftCandidate",
    "ProviderCapabilities",
    "ProviderPolicy",
    "ProviderQuote",
    "SourceAdapter",
    "ProviderRegistry",
    "DEFAULT_PROVIDER_REGISTRY",
    "enabled_sources",
    "SOURCES",
    "HubbleSource",
    "VendorSource",
]
