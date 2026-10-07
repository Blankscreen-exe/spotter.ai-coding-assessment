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
        plan = plan_trip(start, finish)
    except PlannerError as exc:
        print(f'{start} -> {finish}: {exc.code}: {exc.message}')
        return
    elapsed = (time.perf_counter() - began) * 1000
    summary, meta = plan['summary'], plan['meta']
    print(
        f'{start} -> {finish}: {summary["distance_miles"]} mi, {summary["fuel_stops"]} stops, '
        f'${summary["total_fuel_cost"]:.2f} | {elapsed:.0f} ms, {meta["routing_api_calls"]} routing call(s), '
        f'{meta["stations_considered"]} stations on route'
    )
    for stop in plan['fuel_stops']:
        print(
            f'   {stop["order"]}. mile {stop["mile_marker"]:>7} {stop["name"]}, {stop["city"]}, {stop["state"]}'
            f' | ${stop["price_per_gallon"]:.3f} x {stop["gallons_purchased"]} gal = ${stop["cost"]:.2f}'
            f' (arrived with {stop["gallons_on_arrival"]} gal, {stop["miles_off_route"]} mi off route)'
        )


if __name__ == '__main__':
    trips = [tuple(sys.argv[1:3])] if len(sys.argv) >= 3 else DEFAULT_TRIPS
    for trip in trips:
        show(*trip)
        show(*trip)  # second run shows the cached-route timing
