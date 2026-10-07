"""Plan a trip: resolve the endpoints, fetch the route, pick the fuel stops.

Two caches sit in front of the work, checked in this order:

- the finished plan, keyed by the trip and every setting that shapes it, so an
  identical request is answered without recomputing anything;
- the provider's route, keyed by the trip alone, so asking for the same trip
  with a different stop cost or starting fuel still makes no routing call.
"""

import hashlib
import logging
import time
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .. import conf
from ..exceptions import InvalidRequest
from ..providers import get_provider
from .optimizer import Candidate, plan_fuel_stops
from .places import resolve_location
from .stations import stations_along

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')
# The provider's full geometry runs to tens of thousands of points on a
# cross-country route. Matching uses all of it; the response carries a thinned
# copy that still draws cleanly.
MAX_GEOMETRY_POINTS = 3000

FROM_PLAN_CACHE = 'plan cache'
FROM_ROUTE_CACHE = 'route cache'
FROM_PROVIDER = 'routing provider'


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# The cache is an optimisation: if it is down, plan the trip without it.

def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        logger.warning('Cache read failed', exc_info=True)
        return None


def _cache_set(key, value):
    try:
        cache.set(key, value, settings.ROUTE_CACHE_SECONDS)
    except Exception:
        logger.warning('Cache write failed', exc_info=True)


def _trip_key(kind, provider, start, finish, *parameters):
    trip = (provider.name, round(start.lat, 5), round(start.lon, 5), round(finish.lat, 5), round(finish.lon, 5))
    return f'{kind}:' + hashlib.sha256(repr(trip + parameters).encode()).hexdigest()[:32]


def _fetch_route(provider, start, finish):
    """Return (route, external calls made)."""
    key = _trip_key('route', provider, start, finish)
    route = _cache_get(key)
    if route is not None:
        return route, 0
    route = provider.route(start, finish)
    _cache_set(key, route)
    return route, 1


def _thin(coordinates):
    """At most MAX_GEOMETRY_POINTS points, rounded to 5 decimals (about a metre)."""
    step = max(1, -(-len(coordinates) // MAX_GEOMETRY_POINTS))
    keep = np.arange(0, len(coordinates), step)
    if keep[-1] != len(coordinates) - 1:
        keep = np.append(keep, len(coordinates) - 1)
    return np.round(coordinates[keep], 5).tolist()


def _located(on_route_station):
    """What the response says about a station and where it sits on the route."""
    station = on_route_station.station
    return {
        'station_id': station['opis_id'],
        'name': station['name'],
        'address': station['address'],
        'city': station['city'],
        'state': station['state'],
        'lat': station['lat'],
        'lon': station['lon'],
        'mile_marker': round(on_route_station.mile, 1),
        'miles_off_route': round(on_route_station.off_route_miles, 1),
        'price_per_gallon': round(station['price'], 3),
    }


def _build_plan(route, range_miles, mpg, corridor_miles, stop_cost, initial_range_miles):
    """Everything in the response that depends only on the trip and the settings."""
    on_route = stations_along(route.coordinates, route.distance_miles, corridor_miles)
    purchases = plan_fuel_stops(
        [Candidate(mile=s.mile, price=s.station['price'], ref=s) for s in on_route],
        trip_miles=route.distance_miles,
        range_miles=range_miles,
        mpg=mpg,
        start_range_miles=initial_range_miles,
        stop_cost=stop_cost,
    )

    stops, total_cost, total_gallons = [], Decimal('0'), 0.0
    for order, purchase in enumerate(purchases, start=1):
        cost = _money(purchase.gallons * purchase.candidate.price)
        total_cost += cost
        total_gallons += purchase.gallons
        stops.append({
            'order': order,
            **_located(purchase.candidate.ref),
            'gallons_on_arrival': round(purchase.arrival_range_miles / mpg, 2),
            'gallons_purchased': round(purchase.gallons, 2),
            'cost': float(cost),
        })

    return {
        'summary': {
            'distance_miles': round(route.distance_miles, 1),
            'duration_hours': round(route.duration_seconds / 3600, 2),
            'fuel_stops': len(stops),
            'total_fuel_cost': float(total_cost),
            'gallons_purchased': round(total_gallons, 2),
            'gallons_used': round(route.distance_miles / mpg, 2),
            'currency': 'USD',
        },
        'vehicle': {
            'max_range_miles': range_miles,
            'miles_per_gallon': mpg,
            'initial_range_miles': initial_range_miles,
        },
        'planning': {
            'stop_cost': stop_cost,
            'corridor_miles': corridor_miles,
        },
        'fuel_stops': stops,
        # Every station the planner chose from, the chosen ones included, in route order.
        'candidate_stations': [_located(s) for s in sorted(on_route, key=lambda s: s.mile)],
        'route': {
            'type': 'Feature',
            'properties': {'provider': route.provider},
            'geometry': {'type': 'LineString', 'coordinates': _thin(route.coordinates)},
        },
        'stations_considered': len(on_route),
    }


def plan_trip(start_text, finish_text, provider_name=None, initial_range_miles=None, stop_cost=None,
              include_geometry=True, include_candidates=False):
    started = time.perf_counter()
    config = conf.load_settings()
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
    plan_key = _trip_key('plan2', provider, start, finish, *parameters)  # 2: plans carry their candidates
    built = _cache_get(plan_key)
    if built is not None:
        external_calls, served_from = 0, FROM_PLAN_CACHE
    else:
        route, external_calls = _fetch_route(provider, start, finish)
        served_from = FROM_PROVIDER if external_calls else FROM_ROUTE_CACHE
        built = _build_plan(route, *parameters)
        _cache_set(plan_key, built)

    plan = {
        'start': {'query': start.query, 'name': start.name, 'lat': start.lat, 'lon': start.lon},
        'finish': {'query': finish.query, 'name': finish.name, 'lat': finish.lat, 'lon': finish.lon},
        'summary': built['summary'],
        'vehicle': built['vehicle'],
        'planning': built['planning'],
        'fuel_stops': built['fuel_stops'],
    }
    if include_candidates:
        plan['candidate_stations'] = built['candidate_stations']
    if include_geometry:
        plan['route'] = built['route']
    plan['meta'] = {
        'routing_provider': provider.name,
        'routing_api_calls': external_calls,
        'served_from': served_from,
        'stations_considered': built['stations_considered'],
        'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
    }
    return plan
