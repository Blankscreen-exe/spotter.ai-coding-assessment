from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from planner import views
from planner.models import FuelStation, Place
from planner.services.stations import reset_index

TRIP = {'start': 'Alpha, KS', 'finish': 'Omega, OH'}


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
        patcher = mock.patch.object(views, 'plan_trip', return_value={})
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
        FuelStation.objects.create(opis_id=1, name='A', address='x', city='Alpha', state='KS', price='3.00', place=place)
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
