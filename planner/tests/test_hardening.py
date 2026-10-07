import json
import os
import subprocess
import sys
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from config.toolbar import show_toolbar
from planner import views
from planner.models import FuelStation, Place
from planner.services.places import Location
from planner.services.stations import reset_index
from planner.services.trip import Trip, TripPlan

TRIP = {'start': 'Alpha, KS', 'finish': 'Omega, OH'}
# A planned trip for the tests that are about something other than planning.
A_TRIP = Trip(
    start=Location('Alpha, KS', 'Alpha, KS', 40.0, -100.0),
    finish=Location('Omega, OH', 'Omega, OH', 40.0, -80.0),
    plan=TripPlan(
        provider='osrm',
        distance_miles=1059.0,
        duration_seconds=57600.0,
        geometry=[[-100.0, 40.0], [-80.0, 40.0]],
        range_miles=500.0,
        mpg=10.0,
        initial_range_miles=500.0,
        stop_cost=5.0,
        corridor_miles=5.0,
        stops=(),
        candidates=(),
    ),
    routing_calls=0,
    served_from='plan cache',
    elapsed_ms=0.1,
)


class ErrorShapeTests(TestCase):
    """Every failure on the API comes back as {"error": {"code", "message"}}."""

    url = reverse('route-plan')

    def setUp(self):
        cache.clear()

    def test_malformed_json(self):
        response = self.client.post(self.url, '{"start": ', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'parse_error')

    def test_wrong_method(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()['error']['code'], 'method_not_allowed')

    def test_wrong_content_type(self):
        response = self.client.post(self.url, 'start=a', content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()['error']['code'], 'unsupported_media_type')

    def test_unexpected_failure_is_json_and_does_not_leak_details(self):
        with mock.patch.object(views, 'plan_trip', side_effect=RuntimeError('secret internals')):
            with self.assertLogs('planner.handlers', level='ERROR'):
                response = self.client.post(self.url, TRIP, content_type='application/json')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {'error': {'code': 'internal_error', 'message': 'Unexpected server error.'}})


@override_settings(API_RATE_LIMIT='2/min')
class RateLimitTests(TestCase):
    url = reverse('route-plan')

    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(views, 'plan_trip', return_value=A_TRIP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, **extra):
        return self.client.post(self.url, TRIP, content_type='application/json', **extra)

    def test_requests_over_the_limit_are_refused(self):
        self.assertEqual([self.request().status_code for _ in range(3)], [200, 200, 429])
        refused = self.request()
        self.assertEqual(refused.json()['error']['code'], 'throttled')
        self.assertIn('Retry-After', refused.headers)

    def test_limit_is_per_client(self):
        for _ in range(2):
            self.request(REMOTE_ADDR='10.0.0.1')
        self.assertEqual(self.request(REMOTE_ADDR='10.0.0.1').status_code, 429)
        self.assertEqual(self.request(REMOTE_ADDR='10.0.0.2').status_code, 200)

    def test_a_client_cannot_name_its_own_address(self):
        # X-Forwarded-For is whatever the sender types. A new value each time must not be a new allowance.
        statuses = [self.request(HTTP_X_FORWARDED_FOR=f'10.9.9.{number}').status_code for number in range(4)]
        self.assertEqual(statuses, [200, 200, 429, 429])

    def test_behind_a_proxy_the_address_it_reports_is_the_client(self):
        behind_one_proxy = {**settings.REST_FRAMEWORK, 'NUM_PROXIES': 1}
        with override_settings(REST_FRAMEWORK=behind_one_proxy):
            for _ in range(2):
                self.request(HTTP_X_FORWARDED_FOR='203.0.113.5')
            self.assertEqual(self.request(HTTP_X_FORWARDED_FOR='203.0.113.5').status_code, 429)
            self.assertEqual(self.request(HTTP_X_FORWARDED_FOR='203.0.113.6').status_code, 200)
            # Only the proxy's own entry, the last one, is believed: what the client put before it is not.
            self.assertEqual(self.request(HTTP_X_FORWARDED_FOR='1.2.3.4, 203.0.113.5').status_code, 429)

    @override_settings(API_RATE_LIMIT='')
    def test_empty_setting_disables_the_limit(self):
        self.assertEqual({self.request().status_code for _ in range(5)}, {200})

    def test_cache_outage_lets_requests_through(self):
        with mock.patch('rest_framework.throttling.SimpleRateThrottle.allow_request', side_effect=ConnectionError):
            with self.assertLogs('planner.throttling', level='WARNING'):
                self.assertEqual(self.request().status_code, 200)


class HealthTests(TestCase):
    url = reverse('health')

    def setUp(self):
        reset_index()
        self.addCleanup(reset_index)

    def test_ok_when_stations_are_loaded(self):
        place = Place.objects.create(name='Alpha', state='KS', key='alpha', lat=40.0, lon=-100.0)
        FuelStation.objects.create(
            opis_id=1, name='A', address='x', city='Alpha', state='KS', price='3.00', place=place
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok', 'stations': 1})

    def test_unavailable_before_the_data_is_loaded(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['reason'], 'no station data loaded')

    def test_unavailable_when_the_database_is_down(self):
        with mock.patch.object(views, 'get_index', side_effect=DatabaseError):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['reason'], 'database unreachable')

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r'^healthz/$'])
    def test_not_redirected_to_https(self):
        self.assertEqual(self.client.get(self.url).status_code, 503)
        self.assertEqual(self.client.get(reverse('route-map')).status_code, 301)


class DebugToolbarTests(SimpleTestCase):
    """The toolbar shows the inside of the server, so the switch for it has to be dependable."""

    TOOLBAR = 'debug_toolbar.middleware.DebugToolbarMiddleware'

    def settings_under(self, **environment):
        """The settings as a server started with this environment would load them."""
        program = (
            'import json, django; from django.conf import settings; django.setup(); '
            'print(json.dumps({"apps": settings.INSTALLED_APPS, "middleware": settings.MIDDLEWARE, '
            '"config": getattr(settings, "DEBUG_TOOLBAR_CONFIG", {}), "caches": sorted(settings.CACHES)}))'
        )
        return subprocess.run(
            [sys.executable, '-c', program],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env={
                **os.environ,
                'DJANGO_SETTINGS_MODULE': 'config.settings',
                'DJANGO_DEBUG_TOOLBAR': '',
                'DJANGO_SECURE': '',
                'REDIS_URL': '',
                **environment,
            },
        )

    def test_off_unless_asked_for(self):
        loaded = json.loads(self.settings_under().stdout)
        self.assertNotIn('debug_toolbar', loaded['apps'])
        self.assertNotIn(self.TOOLBAR, loaded['middleware'])

    def test_never_loaded_for_the_test_suite(self):
        # True even when the environment asks for it, as the docker compose stack does.
        self.assertFalse(settings.DEBUG_TOOLBAR)
        self.assertNotIn('debug_toolbar', settings.INSTALLED_APPS)
        self.assertEqual(self.client.get('/__debug__/render_panel/').status_code, 404)

    def test_switched_on_it_sits_straight_after_gzip(self):
        loaded = json.loads(self.settings_under(DJANGO_DEBUG_TOOLBAR='true').stdout)
        self.assertIn('debug_toolbar', loaded['apps'])
        # It has to add itself to a page before the page is compressed.
        gzip = loaded['middleware'].index('django.middleware.gzip.GZipMiddleware')
        self.assertEqual(loaded['middleware'].index(self.TOOLBAR), gzip + 1)
        self.assertEqual(loaded['config']['SHOW_TOOLBAR_CALLBACK'], 'config.toolbar.show_toolbar')
        self.assertNotIn('TOOLBAR_STORE_CLASS', loaded['config'])  # one process: its memory will do

    def test_with_redis_its_records_go_to_a_cache_of_their_own(self):
        # Shared, so either worker can show a request the other served; separate from the
        # application's cache, or the Cache panel records none of the application's calls.
        loaded = json.loads(self.settings_under(DJANGO_DEBUG_TOOLBAR='true', REDIS_URL='redis://cache:6379/0').stdout)
        self.assertEqual(loaded['caches'], ['default', 'toolbar'])
        self.assertEqual(loaded['config']['TOOLBAR_STORE_CLASS'], 'debug_toolbar.store.CacheStore')
        self.assertEqual(loaded['config']['CACHE_BACKEND'], 'toolbar')

    def test_refused_on_a_secured_deployment(self):
        refused = self.settings_under(DJANGO_DEBUG_TOOLBAR='true', DJANGO_SECURE='true')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('DJANGO_DEBUG_TOOLBAR must not be set', refused.stderr)

    def test_health_checks_are_kept_out_of_its_history(self):
        # The container checks itself every ten seconds; those would push out the requests worth reading.
        requests = RequestFactory()
        self.assertFalse(show_toolbar(requests.get(reverse('health'))))
        self.assertTrue(show_toolbar(requests.get(reverse('route-map'))))
        self.assertTrue(show_toolbar(requests.post(reverse('route-plan'))))


class FaviconTests(TestCase):
    def test_pages_without_an_icon_of_their_own_are_sent_to_the_site_icon(self):
        response = self.client.get('/favicon.ico')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('/static/planner/favicon.svg'))

    def test_the_map_page_names_its_icon(self):
        self.assertContains(self.client.get(reverse('route-map')), 'rel="icon" type="image/svg+xml"')
