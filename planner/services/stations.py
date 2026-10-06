"""Find the fuel stations that lie along a route.

Stations are held in memory as numpy arrays, loaded once per process, so a
request never queries the station table. Matching is two vectorised steps:

1. A coarse grid picks out the few hundred stations in cells the route passes
   through or next to, out of roughly 6,000.
2. Those stations and the route are converted to unit vectors on the sphere,
   and one matrix product gives every station's distance to every route point.
"""

import threading
from dataclasses import dataclass

import numpy as np
from django.db.models import F

from ..models import FuelStation

EARTH_RADIUS_MILES = 3958.7613
SAMPLE_SPACING_MILES = 1.0
CELL_DEGREES = 0.25
CHUNK = 1024

_index = None
_lock = threading.Lock()


@dataclass(frozen=True)
class RouteStation:
    station: dict
    mile: float
    off_route_miles: float


class StationIndex:
    def __init__(self, stations):
        self.stations = stations
        self.lat = np.array([s['lat'] for s in stations], dtype=np.float64)
        self.lon = np.array([s['lon'] for s in stations], dtype=np.float64)
        self.vectors = unit_vectors(self.lat, self.lon)
        self.cells = cell_ids(self.lat, self.lon)


def unit_vectors(lat, lon):
    lat, lon = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def cell_ids(lat, lon, row_shift=0, column_shift=0):
    """Grid cell of each point as one integer, optionally shifted by whole cells."""
    rows = np.floor(np.asarray(lat) / CELL_DEGREES).astype(np.int64) + row_shift
    columns = np.floor(np.asarray(lon) / CELL_DEGREES).astype(np.int64) + column_shift
    return rows * 100_000 + columns


def cumulative_miles(lat, lon):
    """Distance from the first point to each point, along the line (haversine)."""
    lat, lon = np.radians(lat), np.radians(lon)
    a = np.sin(np.diff(lat) / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2
    steps = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))
    return np.concatenate(([0.0], np.cumsum(steps)))


def get_index():
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                rows = FuelStation.objects.filter(place__isnull=False).values(
                    'opis_id', 'name', 'address', 'city', 'state', 'price',
                    lat=F('place__lat'), lon=F('place__lon'),
                )
                _index = StationIndex([{**row, 'price': float(row['price'])} for row in rows])
    return _index


def reset_index():
    global _index
    _index = None


def stations_along(coordinates, route_miles, corridor_miles, index=None):
    """Stations within corridor_miles of the route, with their mile marker.

    coordinates is the route as [[lon, lat], ...]. Mile markers are scaled so
    the last point sits at route_miles, the distance the routing provider gave.
    """
    index = index or get_index()
    line = np.asarray(coordinates, dtype=np.float64)
    if len(index.stations) == 0 or len(line) < 2:
        return []
    lon, lat = line[:, 0], line[:, 1]
    along = cumulative_miles(lat, lon)
    if along[-1] <= 0:
        return []

    # Resample to evenly spaced points so sparse straight stretches still have
    # a point near every station.
    samples = np.linspace(0.0, along[-1], max(2, int(along[-1] / SAMPLE_SPACING_MILES) + 2))
    sample_lat, sample_lon = np.interp(samples, along, lat), np.interp(samples, along, lon)
    sample_vectors = unit_vectors(sample_lat, sample_lon)

    # Cells the route touches, widened by enough neighbours to cover the corridor.
    lat_reach = corridor_miles / 69.0
    lon_reach = lat_reach / max(0.2, np.cos(np.radians(np.abs(lat).max())))
    row_span, column_span = int(lat_reach / CELL_DEGREES) + 1, int(lon_reach / CELL_DEGREES) + 1
    route_cells = np.unique(np.concatenate([
        cell_ids(sample_lat, sample_lon, row, column)
        for row in range(-row_span, row_span + 1) for column in range(-column_span, column_span + 1)
    ]))
    nearby = np.flatnonzero(np.isin(index.cells, route_cells))

    scale = route_miles / along[-1]
    found = []
    for start in range(0, len(nearby), CHUNK):
        chunk = nearby[start:start + CHUNK]
        closeness = index.vectors[chunk] @ sample_vectors.T
        nearest = closeness.argmax(axis=1)
        chord = np.sqrt(np.clip(2.0 - 2.0 * closeness[np.arange(len(chunk)), nearest], 0.0, None))
        distance = 2 * EARTH_RADIUS_MILES * np.arcsin(np.minimum(chord / 2, 1.0))
        for row, sample, miles_off in zip(chunk, nearest, distance):
            if miles_off <= corridor_miles:
                found.append(RouteStation(index.stations[row], float(samples[sample] * scale), float(miles_off)))
    found.sort(key=lambda s: s.mile)
    return found
