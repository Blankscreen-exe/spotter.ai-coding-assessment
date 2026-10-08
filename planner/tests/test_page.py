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
from django.core.servers.basehttp import WSGIServer
from django.test.testcases import LiveServerThread, QuietWSGIRequestHandler

from planner.models import Place
from planner.services.places import normalize
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
class OneAtATimeServerThread(LiveServerThread):
    """The test server, answering one request at a time.

    On SQLite the test database lives in memory behind a single connection, which Django shares
    with the test server. Its usual server gives every request a thread of its own, and the page
    sends several requests at once (the health check, the plan, the place search). Two threads
    reading through that one connection at the same moment get each other's rows: about one run
    in eight failed with an IndexError or a None where a price should be. Served in turn, they
    cannot. A real server is not affected, since there every thread has its own connection.
    """

    def _create_server(self, connections_override=None):
        return WSGIServer((self.host, self.port), QuietWSGIRequestHandler, allow_reuse_address=False)


class PageTestCase(StaticLiveServerTestCase):
    """The test trip in the database, the routing provider mocked, and a fresh browser page on the live server."""

    server_thread_class = OneAtATimeServerThread

    # The database is emptied after each of these tests, settings rows included. The server then
    # runs on the defaults in conf.py, which are the same values.
    station_name = '{town} Truck Stop'
    station_lat = 40.0  # on the road

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
        create_trip_data(station_name=self.station_name, station_lat=self.station_lat)
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

    def zoom_once_the_map_is_still(self):
        """The zoom level the map has settled on: that of its newest tiles, once it has stopped asking for more.

        The map first draws its opening view and then moves to frame the trip, so tiles from two
        zoom levels arrive in turn. Reading the level before that is over gives the wrong one.
        """
        self.eventually(lambda: self.tiles)
        asked = 0
        while asked != len(self.tiles):
            asked = len(self.tiles)
            self.page.wait_for_timeout(600)
        return self.tiles[-1][0]

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

    # ---------- the two location boxes ----------

    def test_typing_in_a_location_box_offers_places_to_click_or_to_pick_with_the_keys(self):
        page = self.page
        page.goto(f'{self.live_server_url}/map/')
        page.locator('#start').press_sequentially('al')
        expect(page.locator('#startPlaces li')).to_have_text(['Alpha, KS'])
        page.locator('#startPlaces li').first.click()
        expect(page.locator('#start')).to_have_value('Alpha, KS')
        expect(page.locator('#startPlaces')).to_be_hidden()
        expect(page.locator('#start')).to_be_focused()  # still in the box, so Enter carries straight on
        page.press('#start', 'Enter')

        page.locator('#finish').press_sequentially('om')
        expect(page.locator('#finishPlaces li')).to_have_text(['Omega, OH'])
        page.press('#finish', 'ArrowDown')
        expect(page.locator('#finishPlaces li.on')).to_have_text('Omega, OH')
        page.press('#finish', 'Enter')  # this Enter takes the row...
        expect(page.locator('#finish')).to_have_value('Omega, OH')
        expect(page.locator('#askFinish')).to_be_visible()  # ...and the question is still there
        page.press('#finish', 'Enter')  # the next one plans the trip
        expect(page.locator('#tripName')).to_contain_text('Alpha, KS')
        expect(page.locator('#tripName')).to_contain_text('Omega, OH')

    def test_the_list_never_picks_for_the_visitor_and_escape_puts_it_away(self):
        page = self.page
        page.goto(f'{self.live_server_url}/map/')
        page.locator('#start').press_sequentially('do')
        expect(page.locator('#startPlaces li')).to_have_text(['Dover, KS'])
        page.press('#start', 'Escape')
        expect(page.locator('#startPlaces')).to_be_hidden()
        expect(page.locator('#onboarding')).to_be_visible()
        expect(page.locator('#start')).to_have_value('do')

        page.locator('#start').press_sequentially('v')
        expect(page.locator('#startPlaces li')).to_have_text(['Dover, KS'])
        page.press('#start', 'Enter')  # no row was picked, so what was typed goes on as it stands
        expect(page.locator('#fromName')).to_have_text('dov')
        expect(page.locator('#startPlaces')).to_be_hidden()

    def test_the_band_gives_the_fuel_bought_and_the_cost_of_all_the_fuel_used(self):
        self.open_trip()
        bought = float(self.page.locator('#sumCost').inner_text().lstrip('$'))
        in_all = float(self.page.locator('#sumAll').inner_text().lstrip('$'))
        self.assertGreater(bought, 0)
        self.assertGreater(in_all, bought)  # the trip starts on a full tank, and that fuel is counted too

    def test_a_link_that_names_a_routing_provider_keeps_to_it(self):
        self.open_trip(TRIP + '&provider=osrm')
        with self.page.expect_response(lambda response: '/api/v1/route/' in response.url) as answered:
            self.page.click('#cost [data-step="1"]')
        self.assertEqual(answered.value.request.post_data_json['provider'], 'osrm')
        self.page.wait_for_function("location.search.includes('provider=osrm')")

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
        zoom = self.zoom_once_the_map_is_still()
        self.page.click('#zoomIn')
        self.eventually(lambda: self.tiles[-1][0] == zoom + 1)  # the newest tiles are one level closer in

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

    def test_the_curl_command_can_be_pasted_when_a_name_has_an_apostrophe(self):
        Place.objects.create(name="O'Fallon", state='KS', key=normalize("O'Fallon"), lat=40.0, lon=-100.0)
        self.open_trip("start=O'Fallon, KS&finish=Omega, OH", tab='#api')
        # A shell ends a single-quoted string at the next single quote, so the one in the name is written '\''.
        expect(self.page.locator('#panel pre').first).to_contain_text(r"""-d '{"start":"O'\''Fallon, KS",""")

    def test_a_place_name_in_the_list_of_places_is_shown_as_text(self):
        name = '<img src=x onerror="window.hacked = 1">ville'
        Place.objects.create(name=name, state='KS', key=normalize(name), lat=40.0, lon=-99.0)
        self.page.goto(f'{self.live_server_url}/map/')
        self.page.locator('#start').press_sequentially('img')
        expect(self.page.locator('#startPlaces li')).to_contain_text(['<img src=x'])
        self.assert_nothing_ran()

    def test_what_the_visitor_types_is_shown_as_text(self):
        self.plan_from_the_dialog('<img src=x onerror="window.hacked = 1">, KS', 'Omega, OH')
        expect(self.page.locator('#onboarding .error')).to_contain_text('<img src=x')
        self.assert_nothing_ran()


class StationsOffTheRoadTests(PageTestCase):
    """A station is known only by its town, and the town's centre can be miles from the road.

    Here every station's town is just under five miles north of it. The page draws each station
    where the route passes it, so that a stop sits on the line.
    """

    station_lat = 40.07

    def centre_height(self, selector):
        box = self.page.locator(selector).first.bounding_box()
        return box['y'] + box['height'] / 2

    def test_a_stop_is_drawn_on_the_route_line(self):
        self.open_trip()
        expect(self.page.locator('.pin[data-station]')).to_have_count(2)
        # The road runs due east, so on screen the route is a level line.
        line = self.centre_height('.leaflet-overlay-pane path')
        self.assertAlmostEqual(self.centre_height('.pin[data-station]'), line, delta=1)

    def test_its_popup_says_how_far_away_the_town_is(self):
        self.open_trip()
        self.page.locator('.pin[data-station]').first.click()
        expect(self.page.locator('.leaflet-popup')).to_contain_text("The town's centre is 4.8 mi away")

    def test_geojson_puts_it_on_the_line_and_keeps_where_the_town_is(self):
        self.open_trip(tab='#geojson')
        collection = json.loads(self.page.locator('#panel textarea').input_value())
        stop = next(feature for feature in collection['features'] if feature['properties'].get('role') == 'fuel stop')
        self.assertEqual(stop['geometry']['coordinates'][1], 40)  # on the road
        self.assertEqual(stop['properties']['town_centre'][1], 40.07)
        self.assertEqual(stop['properties']['miles_off_route'], 4.8)
        # The stations passed over are left at their towns, there as on the map.
        self.page.check('#panel input[type=checkbox]')
        self.eventually(lambda: len(json.loads(self.page.locator('#panel textarea').input_value())['features']) == 7)
        others = [
            feature
            for feature in json.loads(self.page.locator('#panel textarea').input_value())['features']
            if feature['properties'].get('role') == 'considered, not chosen'
        ]
        self.assertEqual([feature['geometry']['coordinates'][1] for feature in others], [40.07, 40.07])

    def test_stations_passed_over_stay_at_their_towns_beside_the_route(self):
        self.open_trip()
        line = self.centre_height('.leaflet-overlay-pane path')
        # The dots are drawn on a canvas, so find them by their colour: the average height of the orange.
        dots = self.page.evaluate("""() => {
            const canvas = document.querySelector('.leaflet-stations-pane canvas');
            const box = canvas.getBoundingClientRect(), scale = canvas.height / box.height;
            const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            let total = 0, count = 0;
            for (let y = 0; y < canvas.height; y++) {
                for (let x = 0; x < canvas.width; x++) {
                    const i = (y * canvas.width + x) * 4;
                    const orange = pixels[i + 3] > 200 && pixels[i] > 220 && pixels[i + 1] > 90
                        && pixels[i + 1] < 150 && pixels[i + 2] < 60;
                    if (orange) { total += box.y + y / scale; count += 1; }
                }
            }
            return count ? total / count : null;
        }""")
        self.assertIsNotNone(dots)
        self.assertLess(dots, line - 3)  # their towns are north of the road, so they sit above the line
