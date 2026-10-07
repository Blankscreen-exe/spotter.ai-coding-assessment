"""A small made-up trip, shared by the API tests and the browser tests.

Two towns 1,059 miles apart on a straight road, four priced stations between
them, and a routing provider that answers with that road without being called.
"""

from unittest import mock

import numpy as np

from planner import conf
from planner.models import FuelStation, Place
from planner.providers import PROVIDERS, Route
from planner.services.places import normalize

MILES_PER_DEGREE_LON_AT_40N = 52.93
# A road along 40N from 100W to 80W, one point every half degree: about 1,059 miles.
ROAD = np.array([[-100.0 + step / 2, 40.0] for step in range(41)])
ROAD_MILES = 20 * MILES_PER_DEGREE_LON_AT_40N
# (town, longitude, price): miles 159, 370, 635 and 847 along the road.
STATIONS = [('Wayne', -97.0, '3.50'), ('Brook', -93.0, '3.00'), ('Carmel', -88.0, '3.20'), ('Dover', -84.0, '2.90')]


def create_trip_data(station_name='{town} Truck Stop'):
    """The two towns and the stations. One more station has no position and must never be offered."""

    def place(name, state, lon):
        return Place.objects.create(name=name, state=state, key=normalize(name), lat=40.0, lon=lon)

    place('Alpha', 'KS', -100.0)
    place('Omega', 'OH', -80.0)
    for number, (town, lon, price) in enumerate(STATIONS, start=1):
        FuelStation.objects.create(
            opis_id=number,
            name=station_name.format(town=town),
            address='I-70, EXIT 1',
            city=town,
            state='KS',
            price=price,
            place=place(town, 'KS', lon),
        )
    FuelStation.objects.create(
        opis_id=99, name='Nowhere Fuel', address='?', city='Nowhere', state='KS', price='1.00', place=None
    )


def routing_mock():
    """A patch that makes OSRM answer with the road above. Start it; the mock it returns counts the calls."""
    return mock.patch.object(
        PROVIDERS[conf.PROVIDER_OSRM], 'route', return_value=Route(conf.PROVIDER_OSRM, ROAD, ROAD_MILES, 16 * 3600)
    )
