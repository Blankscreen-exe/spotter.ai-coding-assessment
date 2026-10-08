import tempfile
import zipfile
from io import StringIO
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from planner import conf
from planner.models import FuelStation, Place, ProviderCredential, Setting
from planner.services import server_settings

GAZETTEER = """USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG
AL|0100124|x|1|Abbeville city|25|A|1|1|15.543|0.042|31.565164|-85.259165
CO|0811810|x|2|Cañon City city|25|A|1|1|12.5|0.1|38.443|-105.220
TN|4752006|x|3|Nashville-Davidson metropolitan government (balance)|00|A|1|1|475.5|22.0|36.171|-86.784
TX|4873493|x|4|Town of Pecos city|25|A|1|1|32.175|0.|31.389425|-103.522209
VA|5144984|x|5|Lake of the Woods CDP|57|S|1|1|8.9|0.9|38.334|-77.759
PR|7200100|x|6|Adjuntas zona urbana|62|S|1|1|1.0|0.|18.163|-66.723
"""


class ImportPlacesTests(TestCase):
    def run_import(self, **options):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with zipfile.ZipFile(folder / 'gazetteer.zip', 'w') as archive:
            archive.writestr('places.txt', GAZETTEER.encode('utf-8'))
        output = StringIO()
        call_command('import_places', zip=str(folder / 'gazetteer.zip'), stdout=output, **options)
        return output.getvalue()

    def test_import(self):
        output = self.run_import()
        self.assertIn('Loaded 5 places and 2 aliases', output)

        places = {(p.name, p.state): p for p in Place.objects.filter(is_alias=False)}
        self.assertEqual(
            set(places),
            {
                ('Abbeville', 'AL'),
                ('Cañon City', 'CO'),
                ('Nashville-Davidson', 'TN'),
                ('Town of Pecos', 'TX'),
                ('Lake of the Woods', 'VA'),
            },  # descriptors stripped, Puerto Rico skipped
        )
        self.assertEqual(places[('Cañon City', 'CO')].key, 'canon city')
        self.assertEqual((places[('Abbeville', 'AL')].lat, places[('Abbeville', 'AL')].lon), (31.565164, -85.259165))
        self.assertEqual(
            set(Place.objects.filter(is_alias=True).values_list('name', 'state')),
            {('Nashville', 'TN'), ('Pecos', 'TX')},
        )

    def test_rerun_replaces_census_places_and_keeps_geocoded_ones(self):
        Place.objects.create(
            name='Breezewood', state='PA', key='breezewood', lat=39.99, lon=-78.24, source=Place.SOURCE_NOMINATIM
        )
        self.run_import()
        self.run_import()
        self.assertEqual(Place.objects.filter(source=Place.SOURCE_CENSUS).count(), 7)
        self.assertEqual(Place.objects.filter(source=Place.SOURCE_NOMINATIM).count(), 1)

    def test_rerun_warns_that_stations_lost_their_place(self):
        self.run_import()
        FuelStation.objects.create(
            opis_id=1,
            name='A',
            address='x',
            city='Abbeville',
            state='AL',
            price='3.00',
            place=Place.objects.get(name='Abbeville'),
        )
        output = self.run_import()
        self.assertIn('1 stations pointed at the replaced places', output)
        self.assertIsNone(FuelStation.objects.get().place)

    def test_if_empty_skips_when_places_exist(self):
        self.run_import()
        self.assertIn('already loaded', self.run_import(if_empty=True))

    def test_unreadable_file(self):
        with self.assertRaisesMessage(CommandError, 'Cannot read'):
            call_command('import_places', zip='does-not-exist.zip')


@override_settings(CREDENTIALS_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class SetProviderKeyTests(TestCase):
    def setUp(self):
        # Storing a key is written to the server's log. Held here, so it can be read and stays out of the test output.
        self.audit = self.enterContext(mock.patch.object(server_settings.logger, 'info'))

    def run_command(self, *args):
        output = StringIO()
        call_command('set_provider_key', conf.PROVIDER_ORS, *args, stdout=output)
        return output.getvalue()

    def test_key_from_environment_is_stored_encrypted(self):
        with mock.patch.dict('os.environ', {'ORS_TEST_KEY': ' secret-key '}):
            self.run_command('--from-env', 'ORS_TEST_KEY')
        self.assertEqual(ProviderCredential.objects.get(provider=conf.PROVIDER_ORS).api_key, 'secret-key')
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_key FROM planner_providercredential')
            self.assertNotIn('secret-key', cursor.fetchone()[0])
        self.assertEqual(Setting.objects.get(key=conf.ROUTING_PROVIDER).value, conf.PROVIDER_OSRM)

    def test_key_from_prompt(self):
        with mock.patch('getpass.getpass', return_value='typed-key'):
            self.run_command()
        self.assertEqual(ProviderCredential.objects.get().api_key, 'typed-key')

    def test_storing_again_replaces_the_key(self):
        with mock.patch('getpass.getpass', side_effect=['first', 'second']):
            self.run_command()
            self.run_command()
        self.assertEqual(ProviderCredential.objects.get().api_key, 'second')

    def test_activate_makes_the_provider_the_default(self):
        with mock.patch('getpass.getpass', return_value='typed-key'):
            self.run_command('--activate')
        self.assertEqual(server_settings.load()[conf.ROUTING_PROVIDER], conf.PROVIDER_ORS)

    def test_it_is_logged_as_the_command_and_never_with_the_key(self):
        with mock.patch('getpass.getpass', return_value='typed-key'):
            self.run_command('--activate')
        message = self.audit.call_args.args[0] % self.audit.call_args.args[1:]
        self.assertEqual(
            message,
            'Settings changed by the set_provider_key command: '
            'routing.provider=openrouteservice, openrouteservice API key',
        )

    def test_empty_key_is_refused(self):
        with mock.patch.dict('os.environ', {}, clear=False):
            with self.assertRaisesMessage(CommandError, 'No API key given'):
                self.run_command('--from-env', 'ORS_KEY_THAT_IS_NOT_SET')
        self.assertFalse(ProviderCredential.objects.exists())


# The real password hasher is deliberately slow (over a second per password); tests swap in a fast one.
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SeedAdminTests(TestCase):
    def run_command(self, *args, **environment):
        output = StringIO()
        cleared = {'DJANGO_SUPERUSER_USERNAME': '', 'DJANGO_SUPERUSER_PASSWORD': '', 'DJANGO_SUPERUSER_EMAIL': ''}
        with mock.patch.dict('os.environ', {**cleared, **environment}):
            call_command('seed_admin', *args, stdout=output)
        return output.getvalue()

    @override_settings(DEBUG=True)
    def test_demo_login_when_debug_is_on(self):
        self.assertIn('Created admin user "admin"', self.run_command())
        self.assertTrue(self.client.login(username='admin', password='fuelroute-demo'))
        self.assertEqual(self.client.get('/admin/planner/setting/').status_code, 200)

    def test_demo_password_is_refused_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, 'Set DJANGO_SUPERUSER_PASSWORD'):
            self.run_command()
        self.assertFalse(get_user_model().objects.exists())

    def test_login_from_the_environment(self):
        self.run_command(DJANGO_SUPERUSER_USERNAME='ops', DJANGO_SUPERUSER_PASSWORD='s3cret-pass')
        user = get_user_model().objects.get()
        self.assertEqual((user.username, user.is_superuser, user.is_staff), ('ops', True, True))
        self.assertTrue(user.check_password('s3cret-pass'))

    def test_existing_user_is_left_alone(self):
        self.run_command(DJANGO_SUPERUSER_PASSWORD='first-pass')
        output = self.run_command(DJANGO_SUPERUSER_PASSWORD='second-pass')
        self.assertIn('already exists', output)
        self.assertTrue(get_user_model().objects.get().check_password('first-pass'))

    def test_reset_password(self):
        self.run_command(DJANGO_SUPERUSER_PASSWORD='first-pass')
        self.run_command('--reset-password', DJANGO_SUPERUSER_PASSWORD='second-pass')
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertTrue(get_user_model().objects.get().check_password('second-pass'))


class GenerateEncryptionKeyTests(SimpleTestCase):
    def test_prints_a_usable_fernet_key(self):
        output = StringIO()
        call_command('generate_encryption_key', stdout=output)
        Fernet(output.getvalue().strip().encode())  # raises if the key is not valid
