import logging

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle

logger = logging.getLogger(__name__)


class RouteRateThrottle(AnonRateThrottle):
    """Per-client limit on route planning.

    The API is open and every new trip costs a call to a free public routing
    server, so one client should not be able to spend that on everyone's behalf.
    The rate comes from API_RATE_LIMIT ("120/min"); empty switches it off.
    """

    def get_rate(self):
        return settings.API_RATE_LIMIT or None

    def allow_request(self, request, view):
        # Counters live in the cache. If the cache is down, serve the request.
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning('Rate limit check failed; allowing the request', exc_info=True)
            return True
