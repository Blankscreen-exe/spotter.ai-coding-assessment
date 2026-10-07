from dataclasses import dataclass
from functools import cached_property
from typing import Any

import numpy as np
import requests
from django.conf import settings

from ..exceptions import RoutingProviderError
from ..services.places import Location

METERS_PER_MILE = 1609.344
CONNECT_TIMEOUT_SECONDS = 3


@dataclass(frozen=True, eq=False)
class Route:
    provider: str
    coordinates: np.ndarray  # shape (N, 2), rows of [lon, lat]
    distance_miles: float
    duration_seconds: float


class RoutingProvider:
    """A routing API. A new one is a subclass with these three names and a route() method."""

    name = ''
    label = ''
    base_url_setting = ''

    @property
    def base_url(self) -> str:
        return getattr(settings, self.base_url_setting)

    @cached_property
    def session(self) -> requests.Session:
        """Kept for as long as the process lives, so the TLS connection to the provider stays
        open between requests and only the first call pays for the handshake."""
        session = requests.Session()
        session.headers['User-Agent'] = 'fuel-route-planner/1.0'
        return session

    def route(self, start: Location, finish: Location) -> Route:
        """Return the driving Route between two Locations, in one HTTP call."""
        raise NotImplementedError

    def connect(self) -> bool:
        """Open the connection ahead of the first route request. Returns success.

        A bare HEAD on the host: it completes the TLS handshake and leaves the
        connection in the pool without asking the provider to compute anything.
        """
        try:
            self.session.head(self.base_url, timeout=CONNECT_TIMEOUT_SECONDS)
        except requests.RequestException:
            return False
        return True

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.request(method, url, timeout=settings.ROUTING_TIMEOUT_SECONDS, **kwargs)
        except requests.Timeout:
            raise RoutingProviderError(f'{self.label} did not respond in time.')
        except requests.RequestException as exc:
            raise RoutingProviderError(f'Could not reach {self.label}: {exc.__class__.__name__}.')

    def _json(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise RoutingProviderError(f'{self.label} returned HTTP {response.status_code} with a non-JSON body.')
