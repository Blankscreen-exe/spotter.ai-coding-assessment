from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import connection
from django.test import TestCase, override_settings

from planner import conf
from planner.models import ProviderCredential, Setting
from planner.services import server_settings

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()
SECRET = 'ors-secret-key-1234'


def stored_value():
    with connection.cursor() as cursor:
        cursor.execute('SELECT api_key FROM planner_providercredential')
        return cursor.fetchone()[0]


@override_settings(CREDENTIALS_ENCRYPTION_KEYS=[KEY_A])
class ProviderCredentialTests(TestCase):
    def test_api_key_is_ciphertext_in_the_database(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key=SECRET)
        raw = stored_value()
        self.assertTrue(raw.startswith('fernet:'))
        self.assertNotIn(SECRET, raw)

    def test_api_key_reads_back_as_plaintext(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key=SECRET)
        self.assertEqual(ProviderCredential.objects.get().api_key, SECRET)

    def test_masked_shows_only_the_last_four_characters(self):
        credential = ProviderCredential(provider=conf.PROVIDER_ORS, api_key=SECRET)
        self.assertEqual(credential.masked(), '********1234')

    def test_old_key_still_decrypts_after_rotation(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key=SECRET)
        with override_settings(CREDENTIALS_ENCRYPTION_KEYS=[KEY_B, KEY_A]):
            self.assertEqual(ProviderCredential.objects.get().api_key, SECRET)

    def test_wrong_key_fails_loudly(self):
        ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key=SECRET)
        with override_settings(CREDENTIALS_ENCRYPTION_KEYS=[KEY_B]):
            with self.assertRaisesMessage(ImproperlyConfigured, 'could not be decrypted'):
                ProviderCredential.objects.get()

    def test_saving_without_an_encryption_key_is_refused(self):
        with override_settings(CREDENTIALS_ENCRYPTION_KEYS=[]):
            with self.assertRaisesMessage(ImproperlyConfigured, 'CREDENTIALS_ENCRYPTION_KEYS is not set'):
                ProviderCredential.objects.create(provider=conf.PROVIDER_ORS, api_key=SECRET)


class SettingTests(TestCase):
    def test_defaults_are_seeded_by_migration(self):
        self.assertEqual(set(Setting.objects.values_list('key', flat=True)), set(conf.DEFINITIONS))

    def test_load_settings_parses_values(self):
        Setting.objects.filter(key=conf.CORRIDOR_MILES).update(value='7.5')
        Setting.objects.filter(key=conf.ROUTING_PROVIDER).update(value='openrouteservice')
        loaded = server_settings.load()
        self.assertEqual(loaded[conf.CORRIDOR_MILES], 7.5)
        self.assertEqual(loaded[conf.ROUTING_PROVIDER], conf.PROVIDER_ORS)

    def test_missing_row_falls_back_to_the_default(self):
        Setting.objects.all().delete()
        self.assertEqual(server_settings.load()[conf.RANGE_MILES], 500.0)

    def test_a_stored_value_that_is_no_longer_allowed_gives_way_to_the_default(self):
        # A corridor of 60 miles could be saved before the 25-mile ceiling existed.
        Setting.objects.filter(key=conf.CORRIDOR_MILES).update(value='60')
        Setting.objects.filter(key=conf.MPG).update(value='12')
        with self.assertLogs('planner.services.server_settings', level='WARNING') as logs:
            loaded = server_settings.load()
        self.assertEqual((loaded[conf.CORRIDOR_MILES], loaded[conf.MPG]), (5.0, 12.0))
        self.assertIn('stations.corridor_miles', logs.output[0])

    def test_invalid_values_are_rejected(self):
        for key, value in [(conf.ROUTING_PROVIDER, 'google'), (conf.MPG, '0'), (conf.STOP_COST, '-1')]:
            with self.assertRaises(ValidationError, msg=f'{key}={value}'):
                Setting(key=key, value=value).clean()
