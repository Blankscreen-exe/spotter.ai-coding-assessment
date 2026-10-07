"""The map page, driven in a real browser.

The other tests check what the server sends. These check what the page does
with it: that the drawer opens, a counter re-plans, text from data is never read
as markup. They need Playwright (pip install -r requirements-dev.txt) and a
Chrome or Edge that is already installed; without either they are skipped.

The page loads Leaflet from a CDN, so they also need the network. Map tiles are
answered here instead, to leave OpenStreetMap's servers alone.
"""

import json
import os
import re
import time
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache

from planner.services.stations import reset_index

from .fixtures import create_trip_data, routing_mock

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # the dependency is optional: see requirements-dev.txt
    sync_playwright = None

TRIP = 'start=Alpha, KS&finish=Omega, OH'
PANEL_HEIGHT = "document.getElementById('panel').getBoundingClientRect().height"
# The smallest PNG there is: one transparent pixel.
PIXEL = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082'
)


def launch(playwright):
    """An installed Chrome or Edge, or None. BROWSER_CHANNEL picks one (chrome, msedge...)."""
    wanted = os.environ.get('BROWSER_CHANNEL')
    for channel in [wanted] if wanted else ['chrome', 'msedge']:
        try:
            return playwright.chromium.launch(channel=channel, timeout=30_000)
        except Exception:
            continue
    return None


@unittest.skipIf(sync_playwright is None, 'Playwright is not installed (see requirements-dev.txt).')
class PageTestCase(StaticLiveServerTestCase):
    """The test trip in the database, the routing provider mocked, and a fresh browser page on the live server."""

    # The database is emptied after each of these tests, settings rows included. The server then
    # runs on the defaults in conf.py, which are the same values.
    station_name = '{town} Truck Stop'

    @classmethod
    def setUpClass(cls):
        # Playwright runs an event loop in this thread, and Django refuses to touch the database
        # from a thread that has one unless told that it is deliberate.
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
        cls.addClassCleanup(os.environ.pop, 'DJANGO_ALLOW_ASYNC_UNSAFE', None)
        super().setUpClass()
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = launch(playwright)
        if cls.browser is None:
            raise unittest.SkipTest('No Chrome or Edge is installed for Playwright to drive.')
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        cache.clear()
        reset_index()
        self.addCleanup(reset_index)
        create_trip_data(station_name=self.station_name)
        self.route_call = self.enterContext(routing_mock())

        context = self.browser.new_context(viewport={'width': 1400, 'height': 900})
        self.addCleanup(context.close)
        self.tiles = []  # (zoom, referer) of every map tile the page asked for
        context.route('https://tile.openstreetmap.org/**', self.answer_tile)
        self.page = context.new_page()
        self.page.set_default_timeout(15_000)
        self.script_errors = []
        self.page.on('pageerror', lambda error: self.script_errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(self.script_errors, []))

    def answer_tile(self, route):
        zoom = int(re.search(r'/(\d+)/\d+/\d+\.png', route.request.url).group(1))
        self.tiles.append((zoom, route.request.all_headers().get('referer')))
        route.fulfill(status=200, content_type='image/png', body=PIXEL)

    def open_trip(self, query=TRIP, tab=''):
        """Open the page on a trip, as a link from the API would, and wait for the plan."""
        self.page.goto(f'{self.live_server_url}/map/?{query}{tab}')
        expect(self.page.locator('#sumCost')).not_to_be_empty()

    def eventually(self, check, seconds=8):
        """Wait until check() is true, letting the page get on with things meanwhile."""
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() > deadline:
                self.fail('Waited, and it never happened.')
            self.page.wait_for_timeout(100)

    def plan_from_the_dialog(self, start, finish):
        self.page.goto(f'{self.live_server_url}/map/')
        expect(self.page.locator('#onboarding')).to_be_visible()
        self.page.fill('#start', start)
        self.page.press('#start', 'Enter')
        self.page.fill('#finish', finish)
        self.page.press('#finish', 'Enter')


class MapPageTests(PageTestCase):
    # ---------- getting a plan ----------

    def test_asks_where_from_and_where_to_then_shows_the_plan(self):
        self.plan_from_the_dialog('Alpha, KS', 'Omega, OH')
        page = self.page
        expect(page.locator('#onboarding')).to_be_hidden()
        expect(page.locator('#tripName')).to_contain_text('Alpha, KS')
        expect(page.locator('#tripName')).to_contain_text('Omega, OH')
        # 1,059 miles on a 500-mile tank is two stops whatever the prices.
        expect(page.locator('#sumStops')).to_have_text('2')
        expect(page.locator('.pin[data-station]')).to_have_count(2)
        expect(page.locator('.flag')).to_have_count(1)
        self.assertEqual(self.route_call.call_count, 1)

    def test_a_place_that_cannot_be_found_is_explained_in_the_dialog(self):
        self.plan_from_the_dialog('Alpha, KS', 'Atlantis, KS')
        expect(self.page.locator('#onboarding .error')).to_contain_text('Could not find "Atlantis" in Kansas')
        expect(self.page.locator('#onboarding')).to_be_visible()

    def test_a_link_can_open_straight_on_a_trip_and_a_tab(self):
        self.open_trip(tab='#plan')
        expect(self.page.locator('#onboarding')).to_be_hidden()
        expect(self.page.locator('.tab.on')).to_have_text('Fuel plan')
        expect(self.page.locator('#panel tr.stoprow')).to_have_count(2)

    # ---------- the bottom drawer ----------

    def test_bottom_drawer_starts_closed_and_a_tab_opens_and_closes_it(self):
        self.open_trip()
        self.assertEqual(self.page.evaluate(PANEL_HEIGHT), 0)
        self.page.click('[data-tab=plan]')
        self.page.wait_for_function(f'{PANEL_HEIGHT} > 100')
        expect(self.page.locator('#panel tr.stoprow')).to_have_count(2)
        self.page.click('[data-tab=plan]')  # the open tab again
        self.page.wait_for_function(f'{PANEL_HEIGHT} === 0')

    def test_tab_bar_never_shows_a_scrollbar(self):
        # A tab that overlapped the line under the bar by a pixel once made the bar scroll.
        self.open_trip()
        fits = "(() => { const bar = document.getElementById('tabs'); return bar.scrollHeight <= bar.clientHeight; })()"
        self.assertTrue(self.page.evaluate(fits))
        self.page.click('[data-tab=timeline]')
        self.page.wait_for_function(f'{PANEL_HEIGHT} > 100')
        self.assertTrue(self.page.evaluate(fits))

    def test_stops_against_cost_compares_the_two_settings_either_side(self):
        self.open_trip(tab='#tradeoff')
        expect(self.page.locator('#panel tr.pick')).to_have_count(5)
        self.page.wait_for_function(
            "![...document.querySelectorAll('#panel tr.pick td')].some((cell) => cell.textContent === '…')"
        )
        costs = self.page.evaluate("[...document.querySelectorAll('#panel tr.pick')].map((row) => row.dataset.cost)")
        self.assertEqual(costs, ['3', '4', '5', '6', '7'])
        expect(self.page.locator('#panel tr.pick.current')).to_contain_text('$5 (selected)')

    # ---------- the GeoJSON tab ----------

    def geojson(self):
        return json.loads(self.page.locator('#panel textarea').input_value())

    def test_geojson_tab_holds_the_plan_as_a_feature_collection(self):
        self.open_trip(tab='#geojson')
        collection = self.geojson()
        self.assertEqual(collection['type'], 'FeatureCollection')
        self.assertEqual(
            [(feature['geometry']['type'], feature['properties'].get('role')) for feature in collection['features']],
            [
                ('LineString', None),
                ('Point', 'start'),
                ('Point', 'fuel stop'),
                ('Point', 'fuel stop'),
                ('Point', 'finish'),
            ],
        )
        route, start, stop, _, finish = collection['features']
        # Longitude first, as GeoJSON has it, from one end of the road to the other.
        self.assertEqual(
            (route['geometry']['coordinates'][0], route['geometry']['coordinates'][-1]), ([-100, 40], [-80, 40])
        )
        self.assertEqual((start['geometry']['coordinates'], finish['geometry']['coordinates']), ([-100, 40], [-80, 40]))
        self.assertEqual(route['properties']['name'], 'Alpha, KS to Omega, OH')
        self.assertEqual(route['properties']['fuel_stops'], 2)
        self.assertEqual(stop['properties']['order'], 1)
        self.assertEqual(stop['geometry']['coordinates'][1], 40)
        for told in ('name', 'city', 'mile_marker', 'price_per_gallon', 'gallons_purchased', 'cost'):
            self.assertIn(told, stop['properties'])

    def test_geojson_can_also_list_the_stations_passed_over(self):
        self.open_trip(tab='#geojson')
        self.page.check('#panel input[type=checkbox]')
        # Four stations lie on the road and two are stops, so two more points.
        self.eventually(lambda: len(self.geojson()['features']) == 7)
        roles = [feature['properties'].get('role') for feature in self.geojson()['features']]
        self.assertEqual(roles.count('considered, not chosen'), 2)
        expect(self.page.locator('#panel input[type=checkbox]')).to_be_checked()  # the choice survives the redraw

    def test_copy_puts_the_geojson_on_the_clipboard(self):
        self.page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        self.open_trip(tab='#geojson')
        self.page.click('#panel .output-bar button')
        expect(self.page.locator('#panel .output-bar button')).to_have_text('Copied')
        copied = self.page.evaluate('navigator.clipboard.readText()')
        # Windows hands text back from the clipboard with its own line endings; the JSON is the same.
        self.assertEqual(json.loads(copied), self.geojson())
        self.assertEqual(json.loads(copied)['type'], 'FeatureCollection')

    # ---------- the settings drawer ----------

    def test_settings_drawer_sits_beside_the_page_rather_than_over_it(self):
        self.open_trip()
        width_before = self.page.locator('#map').bounding_box()['width']
        self.page.click('#showSettings')
        expect(self.page.locator('#settingsForm')).not_to_be_empty()

        def side_by_side():
            the_map, drawer = self.page.locator('#map').bounding_box(), self.page.locator('#settings').bounding_box()
            return the_map['width'] < width_before and abs(the_map['x'] + the_map['width'] - drawer['x']) <= 1

        self.eventually(side_by_side)  # the page slides aside to make room, and stops where the drawer begins

    # ---------- the two counters ----------

    def test_an_arrow_changes_the_number_and_the_trip_is_planned_again(self):
        self.open_trip()
        with self.page.expect_response(lambda response: '/api/v1/route/' in response.url) as answered:
            self.page.click('#cost [data-step="1"]')
            expect(self.page.locator('#cost output')).to_have_text('$6')  # at once, before the answer
        self.assertEqual(answered.value.request.post_data_json['stop_cost'], 6)
        self.page.wait_for_function("location.search.includes('stop_cost=6')")
        self.assertEqual(self.route_call.call_count, 1)  # the route was reused, not fetched again

    def test_scrolling_over_a_counter_changes_it_without_scrolling_the_page(self):
        self.open_trip()
        expect(self.page.locator('#fuel output')).to_have_text('500 mi (full)')
        self.page.hover('#fuel output')
        with self.page.expect_response(lambda response: '/api/v1/route/' in response.url):
            self.page.mouse.wheel(0, 100)  # down: less
            expect(self.page.locator('#fuel output')).to_have_text('450 mi')
        self.assertEqual(self.page.evaluate('window.scrollY'), 0)

    # ---------- the map ----------

    def test_zoom_buttons_in_the_trip_bar_zoom_the_map(self):
        self.open_trip()
        expect(self.page.locator('.leaflet-control-zoom')).to_have_count(0)  # not Leaflet's own corner control
        self.eventually(lambda: self.tiles)
        zoom = max(z for z, _ in self.tiles)
        self.page.click('#zoomIn')
        self.eventually(lambda: max(z for z, _ in self.tiles) == zoom + 1)

    def test_map_tiles_are_asked_for_with_a_referer(self):
        # OpenStreetMap serves "Access blocked" tiles to requests without one, and the page's own
        # Referrer-Policy header (Django's default, same-origin) would otherwise withhold it.
        self.open_trip()
        self.eventually(lambda: self.tiles)
        self.assertEqual({referer for _, referer in self.tiles}, {self.live_server_url + '/'})


class HostileTextTests(PageTestCase):
    """Station names come from a data file and place names from whoever is typing. Neither may become markup."""

    station_name = '<img src=x onerror="window.hacked = 1"> {town}'

    def assert_nothing_ran(self):
        self.assertEqual(self.page.locator('img[src="x"]').count(), 0)
        self.assertIsNone(self.page.evaluate('window.hacked ?? null'))

    def test_a_station_name_is_shown_as_text_wherever_it_appears(self):
        self.open_trip(tab='#plan')
        expect(self.page.locator('#panel tr.stoprow').first).to_contain_text('<img src=x')
        self.page.click('[data-tab=api]')  # the raw response
        expect(self.page.locator('#panel')).to_contain_text('<img src=x')
        self.page.locator('.pin[data-station]').first.click()  # its pop-up on the map
        expect(self.page.locator('.leaflet-popup')).to_contain_text('<img src=x')
        self.assert_nothing_ran()

    def test_what_the_visitor_types_is_shown_as_text(self):
        self.plan_from_the_dialog('<img src=x onerror="window.hacked = 1">, KS', 'Omega, OH')
        expect(self.page.locator('#onboarding .error')).to_contain_text('<img src=x')
        self.assert_nothing_ran()
