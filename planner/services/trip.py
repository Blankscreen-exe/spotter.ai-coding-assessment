"""Plan a trip: resolve the endpoints, fetch the route, pick the fuel stops.

Two caches sit in front of the work, checked in this order:

- the finished plan, keyed by the trip and every setting that shapes it, so an
  identical request is answered without recomputing anything;
- the provider's route, keyed by the trip alone, so asking for the same trip
  with a different stop cost or starting fuel still makes no routing call.

What comes back is plain objects: a Trip holding a TripPlan. How they read as
JSON, down to the rounding, is the serializers' business, so anything else that
wants a plan (a command, a report, another API version) can use the same call.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .. import conf
from ..exceptions import InvalidRequest
from ..providers import Route, RoutingProvider, get_provider
from . import server_settings
from .optimizer import Candidate, plan_fuel_stops
from .places import Location, resolve_location
from .stations import RouteStation, Station, stations_along

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')
# The provider's full geometry runs to tens of thousands of points on a
# cross-country route. Matching uses all of it; a plan carries a thinned copy
# that still draws cleanly.
MAX_GEOMETRY_POINTS = 3000

FROM_PLAN_CACHE = 'plan cache'
FROM_ROUTE_CACHE = 'route cache'
FROM_PROVIDER = 'routing provider'


@dataclass(frozen=True)
class FuelStop:
    """One purchase: at which station, where along the route, and how much."""

    order: int
    station: Station
    mile: float
    off_route_miles: float
    gallons_on_arrival: float
    gallons_purchased: float
    cost: Decimal  # to the cent


@dataclass(frozen=True)
class TripPlan:
    """A planned trip, as far as it depends only on the route and the settings.

    That is what makes it safe to cache and hand to any request for the same
    trip under the same settings.
    """

    provider: str
    distance_miles: float
    duration_seconds: float
    geometry: list[list[float]]  # the route line, thinned: [[lon, lat], ...]
    range_miles: float
    mpg: float
    initial_range_miles: float
    stop_cost: float
    corridor_miles: float
    stops: tuple[FuelStop, ...]
    candidates: tuple[RouteStation, ...]  # every station the stops were chosen from, in route order

    @property
    def total_cost(self) -> Decimal:
        return sum((stop.cost for stop in self.stops), Decimal('0'))

    @property
    def gallons_purchased(self) -> float:
        return sum(stop.gallons_purchased for stop in self.stops)

    @property
    def gallons_used(self) -> float:
        return self.distance_miles / self.mpg

    @property
    def duration_hours(self) -> float:
        return self.duration_seconds / 3600


@dataclass(frozen=True)
class Trip:
    """A plan as it answered one request: whose trip it is, and how the answer was come by."""

    start: Location
    finish: Location
    plan: TripPlan
    routing_calls: int
    served_from: str
    elapsed_ms: float


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# The cache is an optimisation: if it is down, plan the trip without it.

def _cache_get(key: str) -> Any:
    try:
        return cache.get(key)
    except Exception:
        logger.warning('Cache read failed', exc_info=True)
        return None


def _cache_set(key: str, value: Any) -> None:
    try:
        cache.set(key, value, settings.ROUTE_CACHE_SECONDS)
    except Exception:
        logger.warning('Cache write failed', exc_info=True)


def _trip_key(kind: str, provider: RoutingProvider, start: Location, finish: Location, *parameters: float) -> str:
    trip = (provider.name, round(start.lat, 5), round(start.lon, 5), round(finish.lat, 5), round(finish.lon, 5))
    return f'{kind}:' + hashlib.sha256(repr(trip + parameters).encode()).hexdigest()[:32]


def _fetch_route(provider: RoutingProvider, start: Location, finish: Location) -> tuple[Route, int]:
    """Return (route, external calls made)."""
    key = _trip_key('route', provider, start, finish)
    route = _cache_get(key)
    if route is not None:
        return route, 0
    route = provider.route(start, finish)
    _cache_set(key, route)
    return route, 1


def _thin(coordinates: np.ndarray) -> list[list[float]]:
    """At most MAX_GEOMETRY_POINTS points, rounded to 5 decimals (about a metre)."""
    step = max(1, -(-len(coordinates) // MAX_GEOMETRY_POINTS))
    keep = np.arange(0, len(coordinates), step)
    if keep[-1] != len(coordinates) - 1:
        keep = np.append(keep, len(coordinates) - 1)
    return np.round(coordinates[keep], 5).tolist()


def _build_plan(route: Route, range_miles: float, mpg: float, corridor_miles: float, stop_cost: float,
                initial_range_miles: float) -> TripPlan:
    on_route = stations_along(route.coordinates, route.distance_miles, corridor_miles)
    purchases = plan_fuel_stops(
        [Candidate(mile=s.mile, price=s.station.price, ref=s) for s in on_route],
        trip_miles=route.distance_miles,
        range_miles=range_miles,
        mpg=mpg,
        start_range_miles=initial_range_miles,
        stop_cost=stop_cost,
    )
    stops = tuple(
        FuelStop(
            order=order,
            station=purchase.candidate.ref.station,
            mile=purchase.candidate.ref.mile,
            off_route_miles=purchase.candidate.ref.off_route_miles,
            gallons_on_arrival=purchase.arrival_range_miles / mpg,
            gallons_purchased=purchase.gallons,
            cost=_money(purchase.gallons * purchase.candidate.price),
        )
        for order, purchase in enumerate(purchases, start=1)
    )
    return TripPlan(
        provider=route.provider,
        distance_miles=route.distance_miles,
        duration_seconds=route.duration_seconds,
        geometry=_thin(route.coordinates),
        range_miles=range_miles,
        mpg=mpg,
        initial_range_miles=initial_range_miles,
        stop_cost=stop_cost,
        corridor_miles=corridor_miles,
        stops=stops,
        candidates=tuple(on_route),
    )


def plan_trip(start_text: str, finish_text: str, provider_name: str | None = None,
              initial_range_miles: float | None = None, stop_cost: float | None = None) -> Trip:
    """Plan the drive between two places. Whatever is left out comes from the server settings."""
    started = time.perf_counter()
    config = server_settings.load()
    range_miles, mpg, corridor_miles = config[conf.RANGE_MILES], config[conf.MPG], config[conf.CORRIDOR_MILES]
    if initial_range_miles is None:
        initial_range_miles = range_miles
    elif initial_range_miles > range_miles:
        raise InvalidRequest(f'initial_range_miles cannot exceed the vehicle range of {range_miles:g} miles.')
    if stop_cost is None:
        stop_cost = config[conf.STOP_COST]

    start = resolve_location(start_text)
    finish = resolve_location(finish_text)
    provider = get_provider(provider_name or config[conf.ROUTING_PROVIDER])

    parameters = (range_miles, mpg, corridor_miles, stop_cost, initial_range_miles)
    # The number changes whenever what is cached changes shape, so older entries are never read back.
    plan_key = _trip_key('plan3', provider, start, finish, *parameters)
    plan = _cache_get(plan_key)
    if plan is not None:
        routing_calls, served_from = 0, FROM_PLAN_CACHE
    else:
        route, routing_calls = _fetch_route(provider, start, finish)
        served_from = FROM_PROVIDER if routing_calls else FROM_ROUTE_CACHE
        plan = _build_plan(route, *parameters)
        _cache_set(plan_key, plan)

    return Trip(
        start=start,
        finish=finish,
        plan=plan,
        routing_calls=routing_calls,
        served_from=served_from,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
