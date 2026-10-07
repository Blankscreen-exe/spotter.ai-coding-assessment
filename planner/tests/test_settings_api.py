from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from planner import conf
from planner.models import ProviderCredential, Setting
from planner.services import server_settings

FAST_HASHER = ['django.contrib.auth.hashers.MD5PasswordHasher']
SETTINGS_URL, SESSION_URL = reverse('settings'), reverse('session')


def patch(client, body, **extra):
    return client.patch(SETTINGS_URL, body, content_type='application/json', **extra)


@override_settings(PASSWORD_HASHERS=FAST_HASHER, CREDENTIALS_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class SettingsEditingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin', '', 'admin-pass')
        cls.visitor = User.objects.create_user('visitor', '', 'visitor-pass')
        cls.clerk = User.objects.create_user('clerk', '', 'clerk-pass', is_staff=True)
        cls.clerk.user_permissions.add(Permission.objects.get(codename='change_setting'))

    def setUp(self):
        cache.clear()
        self.audit = self.enterContext(mock.patch.object(server_settings.logger, 'info'))

    def value(self, key):
        return Setting.objects.get(key=key).value

    def test_changes_are_logged_with_who_made_them_and_never_the_key(self):
        self.client.force_login(self.admin)
        patch(self.client, {'settings': {conf.MPG: 12}, 'provider_keys': {conf.PROVIDER_ORS: 'the-secret-key'}})
        message = self.audit.call_args.args[0] % self.audit.call_args.args[1:]
        self.assertEqual(message, 'Settings changed by admin: vehicle.mpg=12, openrouteservice API key')

    # ----- who may change settings -----

    def test_reading_says_whether_the_reader_may_edit(self):
        self.assertEqual(
            self.client.get(SETTINGS_URL).json()['editor'],
            {'signed_in': False, 'username': None, 'can_edit': False, 'can_set_keys': False},
        )
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(SETTINGS_URL).json()['editor'],
            {'signed_in': True, 'username': 'admin', 'can_edit': True, 'can_set_keys': True},
        )

    def test_anonymous_change_is_refused(self):
        response = patch(self.client, {'settings': {conf.STOP_COST: 0}})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'not_signed_in')
        self.assertEqual(self.value(conf.STOP_COST), '5')

    def test_account_without_permission_is_refused(self):
        self.client.force_login(self.visitor)
        response = patch(self.client, {'settings': {conf.STOP_COST: 0}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'not_allowed')
        self.assertEqual(self.value(conf.STOP_COST), '5')

    def test_settings_permission_does_not_cover_api_keys(self):
        self.client.force_login(self.clerk)
        self.assertEqual(patch(self.client, {'settings': {conf.MPG: 12}}).status_code, 200)
        response = patch(self.client, {'provider_keys': {conf.PROVIDER_ORS: 'a-key'}})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProviderCredential.objects.exists())

    # ----- changing them -----

    def test_admin_changes_several_settings_at_once(self):
        self.client.force_login(self.admin)
        response = patch(self.client, {'settings': {conf.STOP_COST: 8, conf.MPG: '12.5', conf.RANGE_MILES: 400}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.value(conf.STOP_COST), self.value(conf.MPG), self.value(conf.RANGE_MILES)), ('8', '12.5', '400'))
        returned = {setting['key']: setting['value'] for setting in response.json()['settings']}
        self.assertEqual((returned[conf.STOP_COST], returned[conf.MPG]), (8.0, 12.5))
        self.assertEqual(server_settings.load()[conf.RANGE_MILES], 400.0)

    def test_a_setting_whose_row_is_missing_is_created(self):
        Setting.objects.filter(key=conf.MPG).delete()
        self.client.force_login(self.admin)
        self.assertEqual(patch(self.client, {'settings': {conf.MPG: 9}}).status_code, 200)
        self.assertEqual(self.value(conf.MPG), '9')

    def test_one_bad_value_saves_nothing(self):
        self.client.force_login(self.admin)
        response = patch(self.client, {'settings': {conf.STOP_COST: 8, conf.MPG: 0, conf.RANGE_MILES: 'far', 'made.up': 1}})
        self.assertEqual(response.status_code, 400)
        fields = response.json()['error']['fields']
        self.assertEqual(fields[conf.MPG], ['Fuel economy must be greater than zero.'])
        self.assertEqual(fields[conf.RANGE_MILES], ['Vehicle range must be a number.'])
        self.assertEqual(fields['made.up'], ['Unknown setting.'])
        self.assertEqual(self.value(conf.STOP_COST), '5')

    def test_values_that_would_break_planning_are_refused(self):
        self.client.force_login(self.admin)
        for key, bad in [(conf.STOP_COST, -1), (conf.RANGE_MILES, 'inf'), (conf.MPG, 'nan'), (conf.ROUTING_PROVIDER, 'google')]:
            self.assertEqual(patch(self.client, {'settings': {key: bad}}).status_code, 400, f'{key}={bad}')

    def test_empty_change_is_refused(self):
        self.client.force_login(self.admin)
        for body in ({}, {'settings': {}}, {'settings': 'x'}, []):
            self.assertEqual(patch(self.client, body).status_code, 400, body)

    # ----- providers and their keys -----

    def test_cannot_switch_to_a_provider_that_has_no_key(self):
        self.client.force_login(self.admin)
        response = patch(self.client, {'settings': {conf.ROUTING_PROVIDER: conf.PROVIDER_ORS}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Store an API key', response.json()['error']['fields'][conf.ROUTING_PROVIDER][0])
        self.assertEqual(self.value(conf.ROUTING_PROVIDER), 'osrm')

    def test_key_and_switch_in_one_request(self):
        self.client.force_login(self.admin)
        response = patch(self.client, {
            'settings': {conf.ROUTING_PROVIDER: conf.PROVIDER_ORS}, 'provider_keys': {conf.PROVIDER_ORS: '  the-secret-key '},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProviderCredential.objects.get().api_key, 'the-secret-key')
        providers = {provider['name']: provider for provider in response.json()['providers']}
        self.assertEqual((providers['openrouteservice']['active'], providers['openrouteservice']['has_key']), (True, True))
        # Encrypted in the table, and never sent back.
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_key FROM planner_providercredential')
            self.assertNotIn('the-secret-key', cursor.fetchone()[0])
        self.assertNotIn('the-secret-key', response.content.decode())

    def test_a_stored_key_can_be_replaced(self):
        self.client.force_login(self.admin)
        patch(self.client, {'provider_keys': {conf.PROVIDER_ORS: 'first'}})
        patch(self.client, {'provider_keys': {conf.PROVIDER_ORS: 'second'}})
        self.assertEqual(ProviderCredential.objects.get().api_key, 'second')

    def test_bad_keys_are_refused(self):
        self.client.force_login(self.admin)
        for keys in ({conf.PROVIDER_ORS: '   '}, {conf.PROVIDER_ORS: 5}, {conf.PROVIDER_OSRM: 'not-needed'}):
            self.assertEqual(patch(self.client, {'provider_keys': keys}).status_code, 400, keys)
        self.assertFalse(ProviderCredential.objects.exists())

    def test_storing_a_key_without_an_encryption_key_fails_cleanly(self):
        self.client.force_login(self.admin)
        with override_settings(CREDENTIALS_ENCRYPTION_KEYS=[]):
            response = patch(self.client, {'settings': {conf.MPG: 12}, 'provider_keys': {conf.PROVIDER_ORS: 'a-key'}})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'encryption_not_configured')
        self.assertEqual(self.value(conf.MPG), '10')  # the setting in the same request was not saved either

    # ----- cross-site request forgery -----

    def test_a_signed_in_browser_cannot_be_made_to_change_settings_from_another_site(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(patch(browser, {'settings': {conf.STOP_COST: 0}}).status_code, 403)
        self.assertEqual(self.value(conf.STOP_COST), '5')
        browser.get(reverse('route-map'))  # the page hands out the token
        token = browser.cookies['csrftoken'].value
        self.assertEqual(patch(browser, {'settings': {conf.STOP_COST: 0}}, headers={'X-CSRFToken': token}).status_code, 200)
        self.assertEqual(self.value(conf.STOP_COST), '0')


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class SessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_superuser('admin', '', 'admin-pass')

    def setUp(self):
        cache.clear()

    def sign_in(self, password='admin-pass', client=None, **extra):
        return (client or self.client).post(
            SESSION_URL, {'username': 'admin', 'password': password}, content_type='application/json', **extra
        )

    def test_sign_in_then_out(self):
        self.assertFalse(self.client.get(SESSION_URL).json()['signed_in'])
        response = self.sign_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'signed_in': True, 'username': 'admin', 'can_edit': True, 'can_set_keys': True})
        self.assertTrue(self.client.get(SESSION_URL).json()['signed_in'])
        self.assertEqual(patch(self.client, {'settings': {conf.MPG: 11}}).status_code, 200)

        self.assertFalse(self.client.delete(SESSION_URL).json()['signed_in'])
        self.assertEqual(patch(self.client, {'settings': {conf.MPG: 12}}).status_code, 401)

    def test_it_is_the_same_session_as_the_admin(self):
        self.sign_in()
        self.assertEqual(self.client.get(reverse('admin:planner_setting_changelist')).status_code, 200)

    def test_wrong_password(self):
        response = self.sign_in(password='guess')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_login')
        self.assertFalse(self.client.get(SESSION_URL).json()['signed_in'])

    def test_missing_fields(self):
        response = self.client.post(SESSION_URL, {}, content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_sign_in_needs_the_csrf_token(self):
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(self.sign_in(client=browser).status_code, 403)
        browser.get(reverse('route-map'))
        token = browser.cookies['csrftoken'].value
        self.assertEqual(self.sign_in(client=browser, headers={'X-CSRFToken': token}).status_code, 200)

    @override_settings(LOGIN_RATE_LIMIT='3/min')
    def test_password_guessing_is_slowed_down(self):
        statuses = [self.sign_in(password='guess').status_code for _ in range(5)]
        self.assertEqual(statuses, [400, 400, 400, 429, 429])
        self.assertEqual(self.sign_in().status_code, 429)  # even the right password has to wait

    def test_the_route_endpoint_still_needs_no_token(self):
        browser = Client(enforce_csrf_checks=True)
        response = browser.post(reverse('route-plan'), {'start': 'x'}, content_type='application/json')
        self.assertEqual(response.status_code, 400)  # a validation error, not a CSRF refusal
