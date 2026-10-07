import json
import re
from pathlib import Path
from unittest import mock

import numpy as np
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from planner import conf
from planner.models import FuelStation, Place, Setting
from planner.providers import PROVIDERS, Route
from planner.services.places import normalize
from planner.services.stations import reset_index

MILES_PER_DEGREE_LON_AT_40N = 52.93
# A road along 40N from 100W to 80W, one point every half degree: about 1,059 miles.
ROAD = np.array([[-100.0 + step / 2, 40.0] for step in range(41)])
ROAD_MILES = 20 * MILES_PER_DEGREE_LON_AT_40N


class TripFixture(TestCase):
    """Two towns 1,059 miles apart, four priced stations between them, routing mocked."""

    @classmethod
    def setUpTestData(cls):
        def place(name, state, lon):
            return Place.objects.create(name=name, state=state, key=normalize(name), lat=40.0, lon=lon)

        place('Alpha', 'KS', -100.0)
        place('Omega', 'OH', -80.0)
        # (town, longitude, price): miles 159, 370, 635 and 847 along the road.
        for number, (town, lon, price) in enumerate(
            [('Wayne', -97.0, '3.50'), ('Brook', -93.0, '3.00'), ('Carmel', -88.0, '3.20'), ('Dover', -84.0, '2.90')],
            start=1,
        ):
            FuelStation.objects.create(
                opis_id=number, name=f'{town} Truck Stop', address='I-70, EXIT 1',
                city=town, state='KS', price=price, place=place(town, 'KS', lon),
            )
        FuelStation.objects.create(
            opis_id=99, name='Nowhere Fuel', address='?', city='Nowhere', state='KS', price='1.00', place=None,
        )

    def setUp(self):
        cache.clear()
        reset_index()
        self.addCleanup(reset_index)
        patcher = mock.patch.object(
            PROVIDERS[conf.PROVIDER_OSRM], 'route',
            return_value=Route(conf.PROVIDER_OSRM, ROAD, ROAD_MILES, 16 * 3600),
        )
        self.route_call = patcher.start()
        self.addCleanup(patcher.stop)


class RouteApiTests(TripFixture):
    url = reverse('route-plan')

    def plan(self, **extra):
        return self.client.post(
            self.url, {'start': 'Alpha, KS', 'finish': 'Omega, OH', **extra}, content_type='application/json'
        )

    def test_plans_a_trip(self):
        response = self.plan()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['start']['name'], 'Alpha, KS')
        self.assertEqual(body['summary']['distance_miles'], round(ROAD_MILES, 1))
        self.assertEqual(body['route']['geometry']['type'], 'LineString')
        self.assertEqual(body['meta']['stations_considered'], 4)

        stops = body['fuel_stops']
        self.assertEqual([stop['order'] for stop in stops], list(range(1, len(stops) + 1)))
        self.assertEqual(body['summary']['fuel_stops'], len(stops))
        self.assertAlmostEqual(body['summary']['total_fuel_cost'], sum(stop['cost'] for stop in stops), places=2)
        # Starts full, arrives empty: buys exactly the fuel the tank could not hold.
        self.assertAlmostEqual(body['summary']['gallons_purchased'], (ROAD_MILES - 500) / 10, places=1)
        self.assertNotIn('Nowhere Fuel', [stop['name'] for stop in stops])

    def test_picks_the_cheap_stations(self):
        stops = self.plan(stop_cost=0).json()['fuel_stops']
        # Cheapest way to cover 1,059 miles: top up at $3.00, then fill the rest at $2.90.
        self.assertEqual([stop['city'] for stop in stops], ['Brook', 'Dover'])

    def test_one_routing_call_then_none(self):
        first, second = self.plan().json(), self.plan().json()
        self.assertEqual(first['meta']['routing_api_calls'], 1)
        self.assertEqual(second['meta']['routing_api_calls'], 0)
        self.route_call.assert_called_once()
        self.assertEqual(first['fuel_stops'], second['fuel_stops'])

    def test_repeat_is_served_from_the_plan_cache(self):
        first, second = self.plan().json(), self.plan().json()
        self.assertEqual(first['meta']['served_from'], 'routing provider')
        self.assertEqual(second['meta']['served_from'], 'plan cache')

    def test_same_trip_with_other_parameters_reuses_the_route(self):
        self.plan()
        body = self.plan(stop_cost=0).json()
        self.assertEqual(body['meta']['served_from'], 'route cache')
        self.route_call.assert_called_once()

    def test_changed_setting_is_not_served_a_stale_plan(self):
        self.plan()
        Setting.objects.filter(key=conf.MPG).update(value='20')
        body = self.plan().json()
        self.assertEqual(body['vehicle']['miles_per_gallon'], 20.0)
        self.assertEqual(body['meta']['served_from'], 'route cache')

    def test_cached_plan_echoes_each_request_as_typed(self):
        self.plan()
        body = self.plan(start='alpha ks').json()
        self.assertEqual(body['meta']['served_from'], 'plan cache')
        self.assertEqual(body['start']['query'], 'alpha ks')

    def test_get_with_query_string(self):
        response = self.client.get(self.url, {'start': 'Alpha, KS', 'finish': 'Omega, OH'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('route', response.json())
        self.assertIn('/map/?start=Alpha', response.json()['map_url'])

    def test_geometry_can_be_left_out(self):
        self.assertNotIn('route', self.plan(include_geometry=False).json())

    def test_initial_range_changes_the_plan(self):
        body = self.plan(initial_range_miles=200).json()
        self.assertEqual(body['fuel_stops'][0]['city'], 'Wayne')
        self.assertAlmostEqual(body['summary']['gallons_purchased'], (ROAD_MILES - 200) / 10, places=1)

    def test_stop_cost_trades_money_for_fewer_stops(self):
        cheapest = self.plan(stop_cost=0).json()['summary']
        fewest = self.plan(stop_cost=1000).json()['summary']
        self.assertLessEqual(fewest['fuel_stops'], cheapest['fuel_stops'])
        self.assertGreaterEqual(fewest['total_fuel_cost'], cheapest['total_fuel_cost'])

    def test_missing_field(self):
        response = self.client.post(self.url, {'start': 'Alpha, KS'}, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_request')
        self.assertIn('finish', response.json()['error']['fields'])

    def test_unknown_location(self):
        response = self.plan(finish='Atlantis, TX')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'location_not_found')
        self.route_call.assert_not_called()

    def test_initial_range_above_the_tank_size(self):
        response = self.plan(initial_range_miles=900)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_request')

    def test_no_station_in_reach(self):
        response = self.plan(initial_range_miles=50)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'no_feasible_fuel_plan')

    def test_provider_without_a_key(self):
        response = self.plan(provider='openrouteservice')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'routing_provider_not_configured')

    def test_default_provider_comes_from_the_settings_table(self):
        Setting.objects.filter(key=conf.ROUTING_PROVIDER).update(value=conf.PROVIDER_ORS)
        self.assertEqual(self.plan().status_code, 503)
        self.assertEqual(self.plan(provider='osrm').status_code, 200)

    def test_vehicle_settings_come_from_the_settings_table(self):
        Setting.objects.filter(key=conf.MPG).update(value='20')
        body = self.plan().json()
        self.assertEqual(body['vehicle']['miles_per_gallon'], 20.0)
        self.assertAlmostEqual(body['summary']['gallons_purchased'], (ROAD_MILES - 500) / 20, places=1)


class RouteMapTests(TestCase):
    """The page plans nothing on the server; its script calls the API. These cover what the server hands it."""

    url = reverse('route-map')

    def config(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        embedded = re.search(r'<script id="planner-config" type="application/json">(.*?)</script>', response.content.decode())
        return json.loads(embedded.group(1))

    def script(self):
        return Path(finders.find('planner/map.js')).read_text(encoding='utf-8')

    def test_page_is_told_where_the_api_is(self):
        config = self.config()
        self.assertEqual(config['apiUrl'], reverse('route-plan'))
        self.assertEqual(config['healthUrl'], reverse('health'))

    def test_sliders_start_from_the_server_settings(self):
        self.assertEqual(
            self.config()['defaults'], {'stopCost': 5.0, 'rangeMiles': 500.0, 'mpg': 10.0, 'provider': 'osrm'}
        )
        Setting.objects.filter(key=conf.STOP_COST).update(value='8')
        Setting.objects.filter(key=conf.RANGE_MILES).update(value='400')
        defaults = self.config()['defaults']
        self.assertEqual((defaults['stopCost'], defaults['rangeMiles']), (8.0, 400.0))

    def test_a_link_with_a_trip_is_not_planned_on_the_server(self):
        # /map/?start=...&finish=... is what the API returns as map_url. The script reads the
        # trip from the address, so the server neither plans it nor echoes it into the page.
        with mock.patch('planner.views.plan_trip') as plan:
            response = self.client.get(self.url, {'start': '<script>alert(1)</script>', 'finish': 'Omega, OH'})
        self.assertEqual(response.status_code, 200)
        plan.assert_not_called()
        self.assertNotContains(response, 'alert(1)')

    def test_links_to_the_settings_admin_and_loads_its_assets(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse('admin:planner_setting_changelist'))
        self.assertContains(response, 'planner/map.js')
        self.assertContains(response, 'planner/map.css')

    def test_map_tiles_are_requested_with_a_referer(self):
        # OpenStreetMap serves "Access blocked" tiles to requests without a Referer,
        # and the page's own Referrer-Policy header would otherwise withhold it.
        self.assertEqual(self.client.get(self.url).headers['Referrer-Policy'], 'same-origin')
        self.assertIn("referrerPolicy: 'strict-origin-when-cross-origin'", self.script())

    def test_script_escapes_response_text_it_writes_as_html(self):
        # Station names and error messages come from data and from what the user typed, and
        # parts of the page are built as HTML strings. A bare ${...} of such text would be
        # an injection hole, so each must be wrapped in esc(). Popups use textContent instead.
        html_builders = self.script().split('function popup(')[0] + self.script().split('// ---------- fuel timeline')[1]
        for text in ('stop.name', 's.name', 's.city', 's.state', 'stop.city', 'body.start.name', 'body.finish.name',
                     'error.message', 'error.code', 'example.label', 'meta.served_from', 'meta.routing_provider'):
            self.assertNotRegex(html_builders, r'\$\{' + re.escape(text) + r'[^)]', text)
