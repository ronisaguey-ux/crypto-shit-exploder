"""Trader-discovery and trade-streaming providers."""
from .base import Provider, ProviderError
from .birdeye import BirdeyeProvider
from .helius import HeliusProvider
from .keyless import KeylessProvider
from .solanatracker import SolanaTrackerProvider
from .vybe import VybeProvider

__all__ = [
    "Provider",
    "ProviderError",
    "SolanaTrackerProvider",
    "VybeProvider",
    "BirdeyeProvider",
    "HeliusProvider",
    "KeylessProvider",
    "get_provider",
]

_REGISTRY = {
    "solanatracker": SolanaTrackerProvider,
    "vybe": VybeProvider,
    "birdeye": BirdeyeProvider,
    "helius": HeliusProvider,
    "keyless": KeylessProvider,
}


def get_provider(name: str, api_key: str = "", **kwargs) -> Provider:
    try:
        cls = _REGISTRY[name.lower()]
    except KeyError:
        raise ProviderError(f"unknown provider: {name!r}") from None
    return cls(api_key=api_key, **kwargs)
