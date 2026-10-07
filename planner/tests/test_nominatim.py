import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from planner.models import FuelStation, Place
from planner.services import nominatim
from planner.services.nominatim import NominatimCache

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
3,PIKE PLAZA,"I-76, EXIT 161",Breezewood,PA,2,3.20
4,LOST PUMP,"Nowhere Rd",Atlantis,TX,3,2.00
"""


def reply(hits):
    fake = mock.Mock()
    fake.json.return_value = hits
    return fake


class NominatimCacheTests(SimpleTestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'cache.json'
        self.sleep = self.enterContext(mock.patch.object(nominatim.time, 'sleep'))

    def test_structured_search_hit_is_remembered(self):
        lookups = NominatimCache(self.path)
        with mock.patch.object(
            nominatim.requests, 'get', return_value=reply([{'lat': '39.99870', 'lon': '-78.23890'}])
        ) as get:
            self.assertEqual(lookups.fetch('Breezewood', 'PA'), [39.9987, -78.2389])
        get.assert_called_once()
        params = get.call_args.kwargs['params']
        self.assertEqual(
            (params['city'], params['state'], params['countrycodes']), ('Breezewood', 'Pennsylvania', 'us')
        )
        self.assertIn('User-Agent', get.call_args.kwargs['headers'])
        self.assertEqual(lookups.get('breezewood', 'PA'), [39.9987, -78.2389])

    def test_falls_back_to_free_text_search(self):
        lookups = NominatimCache(self.path)
        with mock.patch.object(
            nominatim.requests, 'get', side_effect=[reply([]), reply([{'lat': '1.5', 'lon': '2.5'}])]
        ) as get:
            self.assertEqual(lookups.fetch('Round O', 'SC'), [1.5, 2.5])
        self.assertEqual(get.call_args.kwargs['params']['q'], 'Round O, South Carolina, USA')

    def test_a_miss_is_remembered_too(self):
        lookups = NominatimCache(self.path)
        with mock.patch.object(nominatim.requests, 'get', return_value=reply([])):
            self.assertIsNone(lookups.fetch('Atlantis', 'TX'))
        self.assertIsNone(lookups.get('Atlantis', 'TX'))

    def test_never_asked_is_different_from_a_miss(self):
        with self.assertRaises(KeyError):
            NominatimCache(self.path).get('Atlantis', 'TX')

    def test_waits_between_requests(self):
        lookups = NominatimCache(self.path)
        with mock.patch.object(nominatim.requests, 'get', return_value=reply([])):
            lookups.fetch('Atlantis', 'TX')  # two searches, back to back
        self.sleep.assert_called()
        self.assertAlmostEqual(self.sleep.call_args.args[0], nominatim.SECONDS_BETWEEN_REQUESTS, delta=0.1)

    def test_save_and_reload(self):
        lookups = NominatimCache(self.path)
        with mock.patch.object(nominatim.requests, 'get', return_value=reply([{'lat': '1.5', 'lon': '2.5'}])):
            lookups.fetch('Breezewood', 'PA')
        lookups.save()
        self.assertEqual(NominatimCache(self.path).get('Breezewood', 'PA'), [1.5, 2.5])


class GeocodeMissingTests(TestCase):
    """import_stations --geocode-missing asks Nominatim only for towns it has no answer for."""

    def setUp(self):
        Place.objects.create(name='Somewhere', state='PA', key='somewhere', lat=40.0, lon=-78.0)
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.folder / 'prices.csv').write_text(CSV, encoding='utf-8')
        (self.folder / 'cache.json').write_text(json.dumps({'ATLANTIS|TX': None}), encoding='utf-8')
        self.enterContext(override_settings(NOMINATIM_CACHE=self.folder / 'cache.json'))
        self.enterContext(mock.patch.object(nominatim.time, 'sleep'))

    def run_import(self):
        output, errors = StringIO(), StringIO()
        call_command(
            'import_stations', csv=str(self.folder / 'prices.csv'), geocode_missing=True, stdout=output, stderr=errors
        )
        return output.getvalue(), errors.getvalue()

    def test_looks_up_only_the_unknown_town_and_saves_the_answer(self):
        with mock.patch.object(
            nominatim.requests, 'get', return_value=reply([{'lat': '39.9987', 'lon': '-78.2389'}])
        ) as get:
            self.run_import()
        get.assert_called_once()  # Breezewood; Atlantis is already a known miss
        self.assertEqual(FuelStation.objects.get(opis_id=3).place.source, Place.SOURCE_NOMINATIM)
        self.assertIsNone(FuelStation.objects.get(opis_id=4).place)
        saved = json.loads((self.folder / 'cache.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['BREEZEWOOD|PA'], [39.9987, -78.2389])

    def test_network_failure_keeps_the_import_going(self):
        with mock.patch.object(nominatim.requests, 'get', side_effect=requests.ConnectionError('offline')):
            _, errors = self.run_import()
        self.assertIn('Nominatim lookup stopped early', errors)
        self.assertEqual(FuelStation.objects.count(), 2)
        self.assertIsNone(FuelStation.objects.get(opis_id=3).place)
