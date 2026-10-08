"""Plan a trip: resolve the endpoints, get the route ready, choose the fuel stops.

Two things are cached, and an identical request reads both in one round trip:

- the route made ready for planning: a line light enough to send, and the
  stations along it. It depends on the trip and the corridor only, so every
  plan of that trip shares it, and asking again with a different stop cost or
  starting fuel makes no routing call and matches no stations;
- the stops chosen, keyed by the trip and every setting that shapes them. That
  entry is a kilobyte or two.

The route as the provider sent it is not kept. It is five times the size of the
ready one and is only needed to match the stations, which is done once.

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
from ..exceptions import InvalidRequest, RoutingProviderError
from ..providers import Route, RoutingProvider, get_provider
from . import server_settings
from .optimizer import Candidate, plan_fuel_stops
from .places import Location, resolve_location
from .stations import RouteStation, Station, stations_along

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')
# The provider's full geometry runs to tens of thousands of points on a
# cross-country route. Matching uses all of it; what is kept and sent is a
# thinned copy that still draws cleanly.
MAX_GEOMETRY_POINTS = 3000
# While one request is fetching a route, others wanting the same one look for
# its answer this often, and for no longer than a routing call may take plus
# this margin for the work after it.
WAIT_STEP_SECONDS = 0.1
WAIT_MARGIN_SECONDS = 5

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
class ReadyRoute:
    """A route as the planner needs it from then on, shared by every plan of the trip."""

    provider: str
    distance_miles: float
    duration_seconds: float
    geometry: list[list[float]]  # the route line, thinned: [[lon, lat], ...]
    corridor_miles: float
    candidates: tuple[RouteStation, ...]  # every station within the corridor, in route order


@dataclass(frozen=True)
class TripPlan:
    """A planned trip in full: the route's side of it, and the stops chosen under these settings."""

    provider: str
    distance_miles: float
    duration_seconds: float
    geometry: list[list[float]]
    range_miles: float
    mpg: float
    initial_range_miles: float
    stop_cost: float
    corridor_miles: float
    stops: tuple[FuelStop, ...]
    candidates: tuple[RouteStation, ...]

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
    def fuel_used_cost(self) -> Decimal | None:
        """What all the fuel burned would cost, the fuel the vehicle set off with included.

        total_cost is the fuel bought on the way, which is nothing at all on a
        trip the starting fuel covers. This prices every gallon burned at what
        the plan pays per gallon on average or, when it buys none, at the
        cheapest station along the route. None if the route passes no station.
        """
        if self.stops:
            price = float(self.total_cost) / self.gallons_purchased
        elif self.candidates:
            price = min(on_route.station.price for on_route in self.candidates)
        else:
            return None
        return _money(self.gallons_used * price)

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


def _cache_get_many(keys: list[str]) -> dict[str, Any]:
    try:
        return cache.get_many(keys)
    except Exception:
        logger.warning('Cache read failed', exc_info=True)
        return {}


def _cache_set(key: str, value: Any) -> None:
    try:
        cache.set(key, value, settings.ROUTE_CACHE_SECONDS)
    except Exception:
        logger.warning('Cache write failed', exc_info=True)


def _trip_key(kind: str, provider: RoutingProvider, start: Location, finish: Location, *parameters: float) -> str:
    trip = (provider.name, round(start.lat, 5), round(start.lon, 5), round(finish.lat, 5), round(finish.lon, 5))
    return f'{kind}:' + hashlib.sha256(repr(trip + parameters).encode()).hexdigest()[:32]


def _thin(coordinates: np.ndarray) -> list[list[float]]:
    """At most MAX_GEOMETRY_POINTS points, rounded to 5 decimals (about a metre)."""
    # Evenly spaced along the line, the first and last points always among them.
    keep = np.linspace(0, len(coordinates) - 1, min(len(coordinates), MAX_GEOMETRY_POINTS)).round().astype(int)
    return np.round(coordinates[keep], 5).tolist()


def _make_ready(route: Route, corridor_miles: float) -> ReadyRoute:
    """Match the stations against the full route once, then keep only what later plans need."""
    return ReadyRoute(
        provider=route.provider,
        distance_miles=route.distance_miles,
        duration_seconds=route.duration_seconds,
        geometry=_thin(route.coordinates),
        corridor_miles=corridor_miles,
        candidates=tuple(stations_along(route.coordinates, route.distance_miles, corridor_miles)),
    )


def _fetch_ready_route(
    key: str, provider: RoutingProvider, start: Location, finish: Location, corridor_miles: float
) -> tuple[ReadyRoute, int]:
    """Get a route that is not cached yet. Returns it with the number of routing calls this request made.

    Several requests for the same new trip can arrive together. Only one of them
    at a time calls the provider: it holds a short-lived marker in the cache
    while it works, and the others wait for its answer. If its call fails, one
    of those waiting takes the marker and tries, and the rest go on waiting, so
    a provider in trouble is never sent the whole burst at once. Nobody waits
    longer than a routing call may take, and the marker expires by itself, so a
    request that dies mid-call cannot hold the rest up for long.
    """
    marker, patience = key + ':fetching', settings.ROUTING_TIMEOUT_SECONDS + WAIT_MARGIN_SECONDS
    give_up_at = time.monotonic() + patience
    while not _claim(marker, patience):
        ready = _wait_for(key, marker, give_up_at)
        if ready is not None:
            return ready, 0
        if time.monotonic() >= give_up_at:
            raise RoutingProviderError(f'{provider.label} did not respond in time.')
    try:
        ready = _make_ready(provider.route(start, finish), corridor_miles)
        _cache_set(key, ready)
        return ready, 1
    finally:
        _release(marker)


def _claim(marker: str, seconds: float) -> bool:
    """True if this request may fetch the route: nobody else is, or the cache cannot say."""
    try:
        return cache.add(marker, 1, seconds)
    except Exception:
        logger.warning('Cache write failed', exc_info=True)
        return True


def _release(marker: str) -> None:
    try:
        cache.delete(marker)
    except Exception:
        logger.warning('Cache write failed', exc_info=True)


def _wait_for(key: str, marker: str, give_up_at: float) -> ReadyRoute | None:
    """The route another request is fetching, once it is there. None if that request failed, or time ran out."""
    while time.monotonic() < give_up_at:
        time.sleep(WAIT_STEP_SECONDS)
        found = _cache_get_many([key, marker])
        if key in found:
            return found[key]
        if marker not in found:
            return None
    return None


def _choose_stops(
    ready: ReadyRoute, range_miles: float, mpg: float, stop_cost: float, initial_range_miles: float
) -> tuple[FuelStop, ...]:
    purchases = plan_fuel_stops(
        [Candidate(mile=s.mile, price=s.station.price, ref=s) for s in ready.candidates],
        trip_miles=ready.distance_miles,
        range_miles=range_miles,
        mpg=mpg,
        start_range_miles=initial_range_miles,
        stop_cost=stop_cost,
    )
    return tuple(
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


def plan_trip(
    start_text: str,
    finish_text: str,
    provider_name: str | None = None,
    initial_range_miles: float | None = None,
    stop_cost: float | None = None,
) -> Trip:
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

    # The number in each key changes whenever what is cached under it changes shape,
    # so entries written by older code are never read back.
    route_key = _trip_key('ready1', provider, start, finish, corridor_miles)
    stops_key = _trip_key(
        'stops1', provider, start, finish, corridor_miles, range_miles, mpg, stop_cost, initial_range_miles
    )
    cached = _cache_get_many([route_key, stops_key])

    ready, stops, routing_calls = cached.get(route_key), cached.get(stops_key), 0
    if ready is None:
        # The stops belong to the route they were chosen on. A route fetched afresh
        # may differ by a mile, so stops cached from an earlier one are not reused.
        ready, routing_calls = _fetch_ready_route(route_key, provider, start, finish, corridor_miles)
        stops = None
    if stops is not None:
        served_from = FROM_PLAN_CACHE
    else:
        served_from = FROM_PROVIDER if routing_calls else FROM_ROUTE_CACHE
        stops = _choose_stops(ready, range_miles, mpg, stop_cost, initial_range_miles)
        _cache_set(stops_key, stops)

    return Trip(
        start=start,
        finish=finish,
        plan=TripPlan(
            provider=ready.provider,
            distance_miles=ready.distance_miles,
            duration_seconds=ready.duration_seconds,
            geometry=ready.geometry,
            range_miles=range_miles,
            mpg=mpg,
            initial_range_miles=initial_range_miles,
            stop_cost=stop_cost,
            corridor_miles=ready.corridor_miles,
            stops=stops,
            candidates=ready.candidates,
        ),
        routing_calls=routing_calls,
        served_from=served_from,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
