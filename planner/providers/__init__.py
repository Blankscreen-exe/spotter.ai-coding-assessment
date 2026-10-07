from .base import Route, RoutingProvider
from .openrouteservice import OpenRouteServiceProvider
from .osrm import OSRMProvider

PROVIDERS = {provider.name: provider for provider in (OSRMProvider(), OpenRouteServiceProvider())}


def get_provider(name: str) -> RoutingProvider:
    return PROVIDERS[name]


__all__ = ['PROVIDERS', 'Route', 'RoutingProvider', 'get_provider']
