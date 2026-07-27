"""Provider registry for marketplace discovery and future fulfillment.

The registry gives us one place to decide which providers are active. It keeps
the current Hubble/vendor behavior but stops hardcoding source lists inside
gift recommendation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.bot.sources.base import SourceAdapter
from app.bot.sources.hubble_source import HubbleSource
from app.bot.sources.vendor_source import VendorSource
from app.core.config import settings


@dataclass
class ProviderRegistry:
    providers: list[SourceAdapter] = field(default_factory=list)

    def enabled(self) -> list[SourceAdapter]:
        out: list[SourceAdapter] = []
        for provider in self.providers:
            if provider.source_id == "hubble" and not settings.hubble_enabled:
                continue
            out.append(provider)
        return out

    def get(self, source_id: str) -> SourceAdapter | None:
        return next((p for p in self.enabled() if p.source_id == source_id), None)


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry([
    HubbleSource(),
    VendorSource(),
])


def enabled_sources() -> list[SourceAdapter]:
    return DEFAULT_PROVIDER_REGISTRY.enabled()
