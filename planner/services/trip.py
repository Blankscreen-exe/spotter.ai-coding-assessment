"""Plan a trip: resolve the endpoints, fetch the route, pick the fuel stops."""

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


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _fetch_route(provider, start, finish):
    """Return (route, external calls made). Identical trips reuse the cached route."""
    key = f'route:{provider.name}:{start.lat:.5f},{start.lon:.5f}:{finish.lat:.5f},{finish.lon:.5f}'
    # The cache is an optimisation: if it is down, plan the trip without it.
    try:
        route = cache.get(key)
    except Exception:
        logger.warning('Route cache read failed', exc_info=True)
        route = None
    if route is not None:
        return route, 0
    route = provider.route(start, finish)
    try:
        cache.set(key, route, settings.ROUTE_CACHE_SECONDS)
    except Exception:
        logger.warning('Route cache write failed', exc_info=True)
    return route, 1


def _thin(coordinates):
    """At most MAX_GEOMETRY_POINTS points, rounded to 5 decimals (about a metre)."""
    step = max(1, -(-len(coordinates) // MAX_GEOMETRY_POINTS))
    keep = np.arange(0, len(coordinates), step)
    if keep[-1] != len(coordinates) - 1:
        keep = np.append(keep, len(coordinates) - 1)
    return np.round(coordinates[keep], 5).tolist()


def plan_trip(start_text, finish_text, provider_name=None, initial_range_miles=None, stop_cost=None,
              include_geometry=True):
    started = time.perf_counter()
    config = conf.load_settings()
    range_miles, mpg = config[conf.RANGE_MILES], config[conf.MPG]
    if initial_range_miles is None:
        initial_range_miles = range_miles
    elif initial_range_miles > range_miles:
        raise InvalidRequest(f'initial_range_miles cannot exceed the vehicle range of {range_miles:g} miles.')

    if stop_cost is None:
        stop_cost = config[conf.STOP_COST]

    start = resolve_location(start_text)
    finish = resolve_location(finish_text)
    provider = get_provider(provider_name or config[conf.ROUTING_PROVIDER])
    route, external_calls = _fetch_route(provider, start, finish)

    on_route = stations_along(route.coordinates, route.distance_miles, config[conf.CORRIDOR_MILES])
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
        on_route_station = purchase.candidate.ref
        station = on_route_station.station
        cost = _money(purchase.gallons * station['price'])
        total_cost += cost
        total_gallons += purchase.gallons
        stops.append({
            'order': order,
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
            'gallons_on_arrival': round(purchase.arrival_range_miles / mpg, 2),
            'gallons_purchased': round(purchase.gallons, 2),
            'cost': float(cost),
        })

    plan = {
        'start': {'query': start.query, 'name': start.name, 'lat': start.lat, 'lon': start.lon},
        'finish': {'query': finish.query, 'name': finish.name, 'lat': finish.lat, 'lon': finish.lon},
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
            'corridor_miles': config[conf.CORRIDOR_MILES],
        },
        'fuel_stops': stops,
    }
    if include_geometry:
        plan['route'] = {
            'type': 'Feature',
            'properties': {'provider': route.provider},
            'geometry': {'type': 'LineString', 'coordinates': _thin(route.coordinates)},
        }
    plan['meta'] = {
        'routing_provider': route.provider,
        'routing_api_calls': external_calls,
        'stations_considered': len(on_route),
        'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
    }
    return plan
