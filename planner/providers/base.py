from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings

from ..exceptions import RoutingProviderError

METERS_PER_MILE = 1609.344

# One session per process keeps the TLS connection to the provider open
# between requests, which saves a handshake on every call after the first.
session = requests.Session()
session.headers['User-Agent'] = 'fuel-route-planner/1.0'


@dataclass(frozen=True, eq=False)
class Route:
    provider: str
    coordinates: np.ndarray  # shape (N, 2), rows of [lon, lat]
    distance_miles: float
    duration_seconds: float


class RoutingProvider:
    name = ''
    label = ''

    def route(self, start, finish):
        """Return the driving Route between two Locations, in one HTTP call."""
        raise NotImplementedError

    def _send(self, method, url, **kwargs):
        try:
            return session.request(method, url, timeout=settings.ROUTING_TIMEOUT_SECONDS, **kwargs)
        except requests.Timeout:
            raise RoutingProviderError(f'{self.label} did not respond in time.')
        except requests.RequestException as exc:
            raise RoutingProviderError(f'Could not reach {self.label}: {exc.__class__.__name__}.')

    def _json(self, response):
        try:
            return response.json()
        except ValueError:
            raise RoutingProviderError(f'{self.label} returned HTTP {response.status_code} with a non-JSON body.')
