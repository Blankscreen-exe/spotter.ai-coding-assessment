"""Per-client rate limits.

A client is the address its connection comes from. X-Forwarded-For is believed
only for as many proxies as NUM_PROXIES says stand in front of the server (none
by default), so a request cannot name its own address and get a fresh allowance.
"""

import logging

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle

logger = logging.getLogger(__name__)


class ClientRateThrottle(SimpleRateThrottle):
    """Counts every request from one address, signed in or not."""

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class RouteRateThrottle(ClientRateThrottle):
    """Per-client limit on route planning.

    The API is open and every new trip costs a call to a free public routing
    server, so one client should not be able to spend that on everyone's behalf.
    The rate comes from API_RATE_LIMIT ("120/min"); empty switches it off.
    """

    scope = 'route'

    def get_rate(self):
        return settings.API_RATE_LIMIT or None

    def allow_request(self, request, view):
        # Counters live in the cache. If the cache is down, serve the request.
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning('Rate limit check failed; allowing the request', exc_info=True)
            return True


class LoginRateThrottle(ClientRateThrottle):
    """Slows down password guessing on the sign-in endpoint. LOGIN_RATE_LIMIT, "10/min" by default."""

    scope = 'login'

    def get_rate(self):
        return settings.LOGIN_RATE_LIMIT or None
