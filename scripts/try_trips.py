"""Plan a few real trips and print the result. Handy for a quick manual check.

python scripts/try_trips.py "New York, NY" "Los Angeles, CA"
"""

import os
import sys
import time
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from planner.exceptions import PlannerError  # noqa: E402
from planner.services.trip import plan_trip  # noqa: E402

DEFAULT_TRIPS = [
    ('New York, NY', 'Los Angeles, CA'),
    ('Chicago, IL', 'Houston, TX'),
    ('Seattle, WA', 'Miami, FL'),
    ('Dallas, TX', 'Austin, TX'),
]


def show(start, finish):
    began = time.perf_counter()
    try:
        trip = plan_trip(start, finish)
    except PlannerError as exc:
        print(f'{start} -> {finish}: {exc.code}: {exc.message}')
        return
    elapsed = (time.perf_counter() - began) * 1000
    plan = trip.plan
    print(
        f'{start} -> {finish}: {plan.distance_miles:.1f} mi, {len(plan.stops)} stops, '
        f'${plan.total_cost:.2f} | {elapsed:.0f} ms, {trip.routing_calls} routing call(s), '
        f'{len(plan.candidates)} stations on route'
    )
    for stop in plan.stops:
        print(
            f'   {stop.order}. mile {stop.mile:>7.1f} {stop.station.name}, {stop.station.city}, {stop.station.state}'
            f' | ${stop.station.price:.3f} x {stop.gallons_purchased:.2f} gal = ${stop.cost:.2f}'
            f' (arrived with {stop.gallons_on_arrival:.2f} gal, {stop.off_route_miles:.1f} mi off route)'
        )


if __name__ == '__main__':
    trips = [tuple(sys.argv[1:3])] if len(sys.argv) >= 3 else DEFAULT_TRIPS
    for trip in trips:
        show(*trip)
        show(*trip)  # second run shows the cached-route timing
