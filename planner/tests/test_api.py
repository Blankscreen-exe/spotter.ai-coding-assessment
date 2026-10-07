import json
import re
from pathlib import Path
from unittest import mock

import numpy as np
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from planner import conf
from planner.models import FuelStation, Place, ProviderCredential, Setting
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
                opis_id=number,
                name=f'{town} Truck Stop',
                address='I-70, EXIT 1',
                city=town,
                state='KS',
                price=price,
                place=place(town, 'KS', lon),
            )
        FuelStation.objects.create(
            opis_id=99,
            name='Nowhere Fuel',
            address='?',
            city='Nowhere',
            state='KS',
            price='1.00',
            place=None,
        )

    def setUp(self):
        cache.clear()
        reset_index()
        self.addCleanup(reset_index)
        patcher = mock.patch.object(
            PROVIDERS[conf.PROVIDER_OSRM],
            'route',
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

    def test_stations_it_chose_from_are_listed_only_when_asked_for(self):
        self.assertNotIn('candidate_stations', self.plan().json())  # the default response stays lean
        body = self.plan(include_candidates=True).json()
        stations = body['candidate_stations']
        self.assertEqual(len(stations), body['meta']['stations_considered'])
        self.assertEqual([s['city'] for s in stations], ['Wayne', 'Brook', 'Carmel', 'Dover'])  # in route order
        self.assertEqual(
            sorted(stations[0]),
            [
                'address',
                'city',
                'lat',
                'lon',
                'mile_marker',
                'miles_off_route',
                'name',
                'price_per_gallon',
                'state',
                'station_id',
            ],
        )
        # The chosen stops are among them, described the same way.
        for stop in body['fuel_stops']:
            self.assertIn({key: stop[key] for key in stations[0]}, stations)

    def test_stations_it_chose_from_also_come_with_a_cached_plan_and_by_get(self):
        first = self.plan(include_candidates=True).json()
        again = self.client.get(
            self.url, {'start': 'Alpha, KS', 'finish': 'Omega, OH', 'include_candidates': 'true'}
        ).json()
        self.assertEqual(again['meta']['served_from'], 'plan cache')
        self.assertEqual(again['candidate_stations'], first['candidate_stations'])
        self.assertNotIn('include_', again['map_url'])  # the link to the map carries the trip, not response options

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


class SettingsApiTests(TestCase):
    url = reverse('settings')

    def settings(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_lists_every_setting_with_its_current_value(self):
        Setting.objects.filter(key=conf.STOP_COST).update(value='8')
        by_key = {setting['key']: setting for setting in self.settings()['settings']}
        self.assertEqual(set(by_key), set(conf.DEFINITIONS))
        self.assertEqual(
            by_key[conf.STOP_COST],
            {
                'key': 'stops.cost_per_stop',
                'label': 'Cost per stop',
                'value': 8.0,
                'default': 5.0,
                'unit': 'USD',
                'description': conf.DEFINITIONS[conf.STOP_COST].help_text,
            },
        )
        self.assertEqual(by_key[conf.RANGE_MILES]['value'], 500.0)
        self.assertEqual(by_key[conf.ROUTING_PROVIDER]['value'], 'osrm')

    def test_says_which_provider_is_in_use(self):
        providers = {provider['name']: provider for provider in self.settings()['providers']}
        self.assertEqual(
            providers['osrm'],
            {
                'name': 'osrm',
                'label': 'OSRM public server (no key)',
                'active': True,
                'needs_key': False,
                'has_key': False,
                'key_page': None,
            },
        )
        self.assertEqual(
            (providers['openrouteservice']['active'], providers['openrouteservice']['needs_key']), (False, True)
        )

    def test_says_where_to_get_a_key_for_a_provider_that_needs_one(self):
        providers = {provider['name']: provider for provider in self.settings()['providers']}
        self.assertEqual(providers['openrouteservice']['key_page'], 'https://openrouteservice.org/dev/#/signup')
        for provider in providers.values():
            self.assertEqual(provider['needs_key'], provider['key_page'] is not None)

    def test_reports_that_a_key_is_stored_but_never_the_key(self):
        with override_settings(CREDENTIALS_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key='super-secret-key')
            response = self.client.get(self.url)
        providers = {provider['name']: provider for provider in response.json()['providers']}
        self.assertTrue(providers['openrouteservice']['has_key'])
        self.assertNotIn('super-secret-key', response.content.decode())
        self.assertNotIn('api_key', response.content.decode())

    def test_a_visitor_cannot_write(self):
        # Saving is PATCH and needs a signed-in account (see test_settings_api). A visitor is told
        # to sign in whatever the method, before learning which methods exist.
        body = {'settings': {conf.STOP_COST: 0}}
        for method in (self.client.patch, self.client.post, self.client.put, self.client.delete):
            self.assertEqual(method(self.url, body, content_type='application/json').status_code, 401)
        self.assertEqual(Setting.objects.get(key=conf.STOP_COST).value, '5')

    def test_patch_is_the_only_way_to_write(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', password=None))
        body = {'settings': {conf.STOP_COST: 0}}
        for method in (self.client.post, self.client.put, self.client.delete):
            self.assertEqual(method(self.url, body, content_type='application/json').status_code, 405)
        self.assertEqual(Setting.objects.get(key=conf.STOP_COST).value, '5')


class RouteMapTests(TestCase):
    """The page plans nothing on the server; its script calls the API. These cover what the server hands it."""

    url = reverse('route-map')

    def config(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        embedded = re.search(
            r'<script id="planner-config" type="application/json">(.*?)</script>', response.content.decode()
        )
        return json.loads(embedded.group(1))

    def script(self):
        return Path(finders.find('planner/map.js')).read_text(encoding='utf-8')

    def test_site_root_opens_the_page(self):
        response = self.client.get('/', {'start': 'Alpha, KS', 'finish': 'Omega, OH'})
        self.assertRedirects(
            response, self.url + '?start=Alpha%2C+KS&finish=Omega%2C+OH', fetch_redirect_response=False
        )
        self.assertEqual(response.status_code, 302)  # temporary, so browsers do not pin it

    def test_page_is_told_where_the_api_is(self):
        config = self.config()
        self.assertEqual(config['apiUrl'], reverse('route-plan'))
        self.assertEqual(config['healthUrl'], reverse('health'))
        self.assertEqual(config['settingsUrl'], reverse('settings'))
        self.assertEqual(config['sessionUrl'], reverse('session'))

    def test_counters_start_from_the_server_settings(self):
        self.assertEqual(
            self.config()['defaults'], {'stopCost': 5.0, 'rangeMiles': 500.0, 'mpg': 10.0, 'provider': 'osrm'}
        )
        Setting.objects.filter(key=conf.STOP_COST).update(value='8')
        Setting.objects.filter(key=conf.RANGE_MILES).update(value='400')
        defaults = self.config()['defaults']
        self.assertEqual((defaults['stopCost'], defaults['rangeMiles']), (8.0, 400.0))

    def test_cost_and_fuel_are_counters_with_an_arrow_either_side(self):
        content = self.client.get(self.url).content.decode()
        self.assertNotIn('type="range"', content)
        for name in ('cost', 'fuel'):
            counter = content[content.index(f'<div class="knob" id="{name}"') :]
            counter = counter[: counter.index('</div>')]
            # Lower on the left, raise on the right, the number between them.
            self.assertLess(counter.index('data-step="-1"'), counter.index('<output>'))
            self.assertLess(counter.index('<output>'), counter.index('data-step="1"'))
            self.assertEqual(counter.count('aria-label='), 2)
        # Scrolling over a counter changes it, so the script has to be allowed to stop the page scrolling.
        self.assertRegex(self.script(), r"addEventListener\('wheel',[\s\S]*?\{ passive: false \}")

    def test_a_link_with_a_trip_is_not_planned_on_the_server(self):
        # /map/?start=...&finish=... is what the API returns as map_url. The script reads the
        # trip from the address, so the server neither plans it nor echoes it into the page.
        with mock.patch('planner.views.plan_trip') as plan:
            response = self.client.get(self.url, {'start': '<script>alert(1)</script>', 'finish': 'Omega, OH'})
        self.assertEqual(response.status_code, 200)
        plan.assert_not_called()
        self.assertNotContains(response, 'alert(1)')

    def test_has_the_settings_drawer_and_loads_its_assets(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'id="showSettings"')
        self.assertContains(response, 'id="settings"')
        # The settings button is only an icon, so it must carry its name for screen readers and on hover.
        self.assertContains(response, 'id="showSettings" aria-label="Server settings" title="Server settings"')
        self.assertContains(response, 'planner/map.js')
        self.assertContains(response, 'planner/map.css')

    def test_zoom_buttons_sit_in_the_trip_bar_in_place_of_the_map_corner_control(self):
        content = self.client.get(self.url).content.decode()
        trip_bar = content[content.index('id="tripBar"') : content.index('id="tools"')]
        self.assertLess(trip_bar.index('id="change"'), trip_bar.index('id="zoomOut"'))
        self.assertIn('id="zoomOut" aria-label="Zoom out" title="Zoom out"', trip_bar)
        self.assertIn('id="zoomIn" aria-label="Zoom in" title="Zoom in"', trip_bar)
        self.assertIn("L.map('map', { zoomControl: false })", self.script())

    def test_bottom_drawer_starts_closed_with_only_its_tab_names(self):
        content = self.client.get(self.url).content.decode()
        dock = content[content.index('id="dock"') : content.index('id="onboarding"')]
        self.assertEqual(dock.count('class="tab" aria-expanded="false" aria-controls="panel"'), 5)
        self.assertIn('id="collapse" hidden', dock)
        self.assertNotIn('dock-open', content.split('<body')[1].split('>')[0])
        styles = Path(finders.find('planner/map.css')).read_text(encoding='utf-8')
        self.assertRegex(styles, r'#panel \{ height: 0;')
        self.assertRegex(styles, r'body\.dock-open #panel \{ height: var\(--panel\)')

    def test_tab_bar_cannot_grow_a_vertical_scrollbar(self):
        # A tab that overlapped the line under the bar by a pixel once made the bar scroll.
        styles = Path(finders.find('planner/map.css')).read_text(encoding='utf-8')
        self.assertRegex(styles, r'\.tabs \{[^}]*overflow-y: hidden')
        tab_rule = re.search(r'^\.tab \{([^}]*)\}', styles, re.MULTILINE).group(1)
        self.assertNotIn('margin', tab_rule)

    def test_settings_drawer_sits_beside_the_page_rather_than_over_it(self):
        content = self.client.get(self.url).content.decode()
        self.assertIn('<aside class="drawer" id="settings" aria-labelledby="settingsTitle">', content)  # not a modal
        self.assertIn('aria-expanded="false" aria-controls="settings"', content)
        styles = Path(finders.find('planner/map.css')).read_text(encoding='utf-8')
        self.assertIn('body.settings-open { padding-right: var(--side); }', styles)

    def test_admin_button_opens_the_admin_panel(self):
        response = self.client.get(self.url)
        self.assertContains(response, f'id="openAdmin" href="{reverse("admin:index")}" target="_blank" rel="noopener"')
        self.assertEqual(reverse('admin:index'), '/admin/')

    def test_map_tiles_are_requested_with_a_referer(self):
        # OpenStreetMap serves "Access blocked" tiles to requests without a Referer,
        # and the page's own Referrer-Policy header would otherwise withhold it.
        self.assertEqual(self.client.get(self.url).headers['Referrer-Policy'], 'same-origin')
        self.assertIn("referrerPolicy: 'strict-origin-when-cross-origin'", self.script())

    def test_script_escapes_response_text_it_writes_as_html(self):
        # Station names and error messages come from data and from what the user typed, and
        # parts of the page are built as HTML strings. A bare ${...} of such text would be
        # an injection hole, so each must be wrapped in esc(). Popups use textContent instead.
        html_builders = (
            self.script().split('function popup(')[0] + self.script().split('// ---------- fuel timeline')[1]
        )
        for text in (
            'stop.name',
            's.name',
            's.city',
            's.state',
            'stop.city',
            'body.start.name',
            'body.finish.name',
            'error.message',
            'error.code',
            'example.label',
            'meta.served_from',
            'meta.routing_provider',
            'setting.label',
            'setting.description',
            'setting.key',
            'provider.label',
            'value.now',
            'value.usual',
        ):
            self.assertNotRegex(html_builders, r'\$\{\s*' + re.escape(text) + r'\s*\}', text)
