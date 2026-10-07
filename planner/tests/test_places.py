from django.test import SimpleTestCase, TestCase

from planner.exceptions import LocationError
from planner.management.commands.import_places import split_descriptor
from planner.models import Place
from planner.services.places import normalize, resolve_location


class NormalizeTests(SimpleTestCase):
    def test_folds_case_punctuation_and_abbreviations(self):
        self.assertEqual(normalize('St. Louis'), 'saint louis')
        self.assertEqual(normalize('FT WORTH'), 'fort worth')
        self.assertEqual(normalize("Coeur d'Alene"), 'coeur dalene')
        self.assertEqual(normalize('Winston-Salem'), 'winston salem')

    def test_joins_a_detached_mc_prefix(self):
        self.assertEqual(normalize('Mc Lean'), normalize('McLean'))

    def test_strips_accents(self):
        self.assertEqual(normalize('Cañon City'), 'canon city')


class SplitDescriptorTests(SimpleTestCase):
    def test_removes_the_census_legal_descriptor(self):
        self.assertEqual(split_descriptor('Abbeville city'), ('Abbeville', 'city'))
        self.assertEqual(split_descriptor('Lake of the Woods CDP'), ('Lake of the Woods', 'CDP'))
        self.assertEqual(
            split_descriptor('Nashville-Davidson metropolitan government (balance)'),
            ('Nashville-Davidson', 'metropolitan government (balance)'),
        )


class ResolveLocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        def place(name, state, lat, lon, land=10.0, alias=False):
            return Place(name=name, state=state, key=normalize(name), lat=lat, lon=lon, land_sqmi=land, is_alias=alias)

        Place.objects.bulk_create(
            [
                place('Chicago', 'IL', 41.84, -87.68),
                place('St. Louis', 'MO', 38.64, -90.24),
                place('New York', 'NY', 40.66, -73.94),
                place('Boise City', 'ID', 43.60, -116.23),
                place('Springfield', 'IL', 39.79, -89.64, land=60),
                place('Springfield', 'MO', 37.19, -93.29, land=82),
                place('Nashville', 'TN', 36.17, -86.78, alias=True),
            ]
        )

    def test_city_and_state_code(self):
        location = resolve_location('Chicago, IL')
        self.assertEqual((location.name, location.lat, location.lon), ('Chicago, IL', 41.84, -87.68))

    def test_accepts_loose_formatting(self):
        for text in ('chicago il', 'Chicago, Illinois', ' CHICAGO ,il ', 'Chicago, IL, USA'):
            self.assertEqual(resolve_location(text).name, 'Chicago, IL', text)

    def test_abbreviation_and_city_suffix_variants(self):
        self.assertEqual(resolve_location('Saint Louis, MO').name, 'St. Louis, MO')
        self.assertEqual(resolve_location('New York City, NY').name, 'New York, NY')
        self.assertEqual(resolve_location('Boise, ID').name, 'Boise City, ID')

    def test_alias_of_a_consolidated_city(self):
        self.assertEqual(resolve_location('Nashville, TN').lat, 36.17)

    def test_coordinates_are_passed_through(self):
        location = resolve_location('41.88, -87.63')
        self.assertEqual((location.lat, location.lon), (41.88, -87.63))

    def test_coordinates_outside_the_usa_are_rejected(self):
        with self.assertRaisesMessage(LocationError, 'inside the USA'):
            resolve_location('48.85, 2.35')

    def test_unique_name_needs_no_state(self):
        self.assertEqual(resolve_location('Chicago').name, 'Chicago, IL')

    def test_ambiguous_name_asks_for_a_state(self):
        with self.assertRaisesMessage(LocationError, 'Springfield, MO; Springfield, IL'):
            resolve_location('Springfield')

    def test_unknown_place(self):
        with self.assertRaisesMessage(LocationError, 'Could not find "Atlantis" in Texas'):
            resolve_location('Atlantis, TX')
