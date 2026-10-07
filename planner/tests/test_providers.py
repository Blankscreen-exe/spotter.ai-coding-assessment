from unittest import mock

import requests
from cryptography.fernet import Fernet
from django.test import SimpleTestCase, TestCase, override_settings

from planner import conf
from planner.exceptions import ProviderNotConfigured, RouteNotFound, RoutingProviderError
from planner.models import ProviderCredential
from planner.providers import get_provider
from planner.services.places import Location

CHICAGO = Location('Chicago, IL', 'Chicago, IL', 41.84, -87.68)
HOUSTON = Location('Houston, TX', 'Houston, TX', 29.79, -95.39)
LINE = [[-87.68, 41.84], [-90.0, 36.0], [-95.39, 29.79]]
ENCODED_LINE = '_{j~F~~cvO~rsb@~bdMn{{d@nv{_@'  # LINE in encoded polyline form, as both providers send it


def response(status, body):
    fake = mock.Mock(status_code=status)
    if body is None:
        fake.json.side_effect = ValueError('not json')
    else:
        fake.json.return_value = body
    return fake


class OSRMTests(SimpleTestCase):
    def setUp(self):
        self.provider = get_provider(conf.PROVIDER_OSRM)

    def test_builds_one_request_and_parses_the_route(self):
        body = {
            'code': 'Ok',
            'routes': [
                {
                    'geometry': ENCODED_LINE,
                    'distance': 1609344.0,
                    'duration': 7200.0,
                }
            ],
        }
        with mock.patch.object(requests.Session, 'request', return_value=response(200, body)) as send:
            route = self.provider.route(CHICAGO, HOUSTON)
        send.assert_called_once()
        method, url = send.call_args.args
        self.assertEqual(method, 'GET')
        self.assertTrue(url.endswith('/route/v1/driving/-87.680000,41.840000;-95.390000,29.790000'))
        self.assertEqual(send.call_args.kwargs['params']['overview'], 'full')
        self.assertEqual(route.coordinates.tolist(), LINE)
        self.assertAlmostEqual(route.distance_miles, 1000.0)
        self.assertEqual(route.duration_seconds, 7200.0)

    def test_no_route(self):
        body = {'code': 'NoRoute', 'message': 'Impossible route between points'}
        with mock.patch.object(requests.Session, 'request', return_value=response(400, body)):
            with self.assertRaises(RouteNotFound):
                self.provider.route(CHICAGO, HOUSTON)

    def test_server_error(self):
        with mock.patch.object(requests.Session, 'request', return_value=response(502, None)):
            with self.assertRaisesMessage(RoutingProviderError, 'HTTP 502'):
                self.provider.route(CHICAGO, HOUSTON)

    def test_timeout(self):
        with mock.patch.object(requests.Session, 'request', side_effect=requests.Timeout):
            with self.assertRaisesMessage(RoutingProviderError, 'did not respond in time'):
                self.provider.route(CHICAGO, HOUSTON)


@override_settings(CREDENTIALS_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class OpenRouteServiceTests(TestCase):
    def setUp(self):
        self.provider = get_provider(conf.PROVIDER_ORS)

    def test_without_a_key_no_request_is_made(self):
        with mock.patch.object(requests.Session, 'request') as send:
            with self.assertRaisesMessage(ProviderNotConfigured, 'has no API key'):
                self.provider.route(CHICAGO, HOUSTON)
        send.assert_not_called()

    def test_sends_the_stored_key_and_parses_the_route(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key='secret-key')
        body = {'routes': [{'geometry': ENCODED_LINE, 'summary': {'distance': 1609344.0, 'duration': 7200.0}}]}
        with mock.patch.object(requests.Session, 'request', return_value=response(200, body)) as send:
            route = self.provider.route(CHICAGO, HOUSTON)
        send.assert_called_once()
        method, url = send.call_args.args
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/v2/directions/driving-car'))
        self.assertEqual(send.call_args.kwargs['headers'], {'Authorization': 'secret-key'})
        self.assertEqual(send.call_args.kwargs['json']['coordinates'], [[-87.68, 41.84], [-95.39, 29.79]])
        self.assertEqual(route.coordinates.tolist(), LINE)
        self.assertAlmostEqual(route.distance_miles, 1000.0)

    def test_rejected_key(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key='bad')
        with mock.patch.object(requests.Session, 'request', return_value=response(403, {'error': 'Access denied'})):
            with self.assertRaisesMessage(ProviderNotConfigured, 'rejected the stored API key'):
                self.provider.route(CHICAGO, HOUSTON)

    def test_no_route(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key='secret-key')
        body = {'error': {'code': 2010, 'message': 'Could not find routable point'}}
        with mock.patch.object(requests.Session, 'request', return_value=response(404, body)):
            with self.assertRaises(RouteNotFound):
                self.provider.route(CHICAGO, HOUSTON)

    def test_quota_exceeded_is_reported(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key='secret-key')
        with mock.patch.object(
            requests.Session, 'request', return_value=response(429, {'error': 'Rate limit exceeded'})
        ):
            with self.assertRaisesMessage(RoutingProviderError, 'HTTP 429: Rate limit exceeded'):
                self.provider.route(CHICAGO, HOUSTON)
