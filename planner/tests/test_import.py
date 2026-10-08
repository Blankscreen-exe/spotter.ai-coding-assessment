import json
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from planner.models import FuelStation, Place
from planner.services.places import normalize

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
1,BIG STOP,"I-44, EXIT 283",Big Cabin   ,OK,307,3.00
1,BIG STOP #1,"I-44, EXIT 283",Big Cabin,OK,307,3.50
2,MAPLE FUEL,"Hwy 401",Toronto,ON,1,4.00
3,PIKE PLAZA,"I-76, EXIT 161",Breezewood,PA,2,3.20
4,LOST PUMP,"Nowhere Rd",Atlantis,TX,3,2.00
5,MC STOP,"I-55, EXIT 145",Mc Lean,IL,4,3.10
"""


class ImportStationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Place.objects.create(name='Big Cabin', state='OK', key=normalize('Big Cabin'), lat=36.54, lon=-95.22)
        Place.objects.create(name='McLean', state='IL', key=normalize('McLean'), lat=40.31, lon=-89.17)

    def run_import(self, lookups, **options):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / 'prices.csv').write_text(CSV, encoding='utf-8')
        (folder / 'nominatim.json').write_text(json.dumps(lookups), encoding='utf-8')
        output = StringIO()
        with override_settings(NOMINATIM_CACHE=folder / 'nominatim.json'):
            call_command('import_stations', csv=str(folder / 'prices.csv'), stdout=output, **options)
        return output.getvalue()

    def test_import(self):
        cache.set('plan:stale', 'built from the old data')
        output = self.run_import({'BREEZEWOOD|PA': [39.9987, -78.2389], 'ATLANTIS|TX': None})

        self.assertEqual(set(FuelStation.objects.values_list('opis_id', flat=True)), {1, 3, 4, 5})  # no Toronto

        merged = FuelStation.objects.get(opis_id=1)
        self.assertEqual(merged.price, Decimal('3.25'))  # mean of the two rows
        self.assertEqual((merged.name, merged.city), ('BIG STOP', 'Big Cabin'))
        self.assertEqual(merged.place.source, Place.SOURCE_CENSUS)

        geocoded = FuelStation.objects.get(opis_id=3).place
        self.assertEqual(
            (geocoded.source, geocoded.name, geocoded.lat), (Place.SOURCE_NOMINATIM, 'Breezewood', 39.9987)
        )

        self.assertIsNone(FuelStation.objects.get(opis_id=4).place)  # Nominatim had no answer
        self.assertEqual(FuelStation.objects.get(opis_id=5).place.name, 'McLean')  # "Mc Lean" in the file

        self.assertIn('outside the USA, dropped: 1', output)
        self.assertIn('duplicate rows merged:    1', output)
        self.assertIsNone(cache.get('plan:stale'))

    def test_town_missing_from_the_cache_is_reported_not_fetched(self):
        output = self.run_import({})
        self.assertIsNone(FuelStation.objects.get(opis_id=3).place)
        self.assertIn('2 towns are in neither the Census file nor the cache', output)

    def test_rerun_replaces_rather_than_duplicates(self):
        lookups = {'BREEZEWOOD|PA': [39.9987, -78.2389], 'ATLANTIS|TX': None}
        self.run_import(lookups)
        self.run_import(lookups)
        self.assertEqual(FuelStation.objects.count(), 4)
        self.assertEqual(Place.objects.filter(source=Place.SOURCE_NOMINATIM).count(), 1)

    def test_if_empty_leaves_existing_stations_alone(self):
        self.run_import({})
        FuelStation.objects.filter(opis_id=1).update(name='EDITED')
        output = self.run_import({}, if_empty=True)
        self.assertIn('already loaded', output)
        self.assertEqual(FuelStation.objects.get(opis_id=1).name, 'EDITED')
