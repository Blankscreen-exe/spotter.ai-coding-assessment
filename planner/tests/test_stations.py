from django.test import SimpleTestCase

from planner.services.stations import Station, StationIndex, stations_along

# A straight west-to-east line along 40N from 100W to 97W, about 159 miles.
LINE = [[-100.0, 40.0], [-97.0, 40.0]]
MILES_PER_DEGREE_LON_AT_40N = 52.93


def index(*positions):
    return StationIndex([
        Station(opis_id=n, name='', address='', city='', state='', price=3.0, lat=lat, lon=lon)
        for n, (lat, lon) in enumerate(positions)
    ])


class StationsAlongTests(SimpleTestCase):
    def test_keeps_stations_inside_the_corridor_only(self):
        stations = index((40.03, -99.0), (40.2, -98.5), (40.0, -98.0))  # ~2, ~14 and 0 miles off the line
        found = stations_along(LINE, 159.0, corridor_miles=5, index=stations)
        self.assertEqual([s.station.opis_id for s in found], [0, 2])

    def test_mile_markers_follow_the_route_in_order(self):
        stations = index((40.0, -98.0), (40.0, -99.0))
        found = stations_along(LINE, 3 * MILES_PER_DEGREE_LON_AT_40N, corridor_miles=5, index=stations)
        self.assertEqual([s.station.opis_id for s in found], [1, 0])
        self.assertAlmostEqual(found[0].mile, MILES_PER_DEGREE_LON_AT_40N, delta=1.0)
        self.assertAlmostEqual(found[1].mile, 2 * MILES_PER_DEGREE_LON_AT_40N, delta=1.0)

    def test_mile_markers_are_scaled_to_the_road_distance(self):
        # The provider's road distance is longer than the straight line drawn here.
        found = stations_along(LINE, 300.0, corridor_miles=5, index=index((40.0, -98.5)))
        self.assertAlmostEqual(found[0].mile, 150.0, delta=1.0)

    def test_off_route_distance(self):
        found = stations_along(LINE, 159.0, corridor_miles=5, index=index((40.03, -99.0)))
        self.assertAlmostEqual(found[0].off_route_miles, 0.03 * 69.05, delta=0.1)

    def test_wide_corridor_reaches_across_grid_cells(self):
        found = stations_along(LINE, 159.0, corridor_miles=40, index=index((40.5, -98.5)))
        self.assertEqual(len(found), 1)

    def test_station_beyond_the_end_of_the_route_is_ignored(self):
        self.assertEqual(stations_along(LINE, 159.0, corridor_miles=5, index=index((40.0, -96.5))), [])

    def test_no_stations(self):
        self.assertEqual(stations_along(LINE, 159.0, corridor_miles=5, index=index()), [])
