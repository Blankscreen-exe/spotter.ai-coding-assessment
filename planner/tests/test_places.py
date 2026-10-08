from django.test import SimpleTestCase, TestCase

from planner.exceptions import LocationError
from planner.management.commands.import_places import split_descriptor
from planner.models import Place
from planner.services.places import normalize, resolve_location, suggest_places


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
                place('Azusa', 'CA', 34.14, -117.91),
                place('West New York', 'NJ', 40.79, -74.01),
                place('Olympia', 'WA', 47.04, -122.89),
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

    def test_coordinates_far_from_the_usa_are_rejected(self):
        # Paris, Mexico City, the Pacific half way to Hawaii, and a latitude and longitude the wrong way round.
        for text in ('48.85, 2.35', '19.43, -99.13', '30.0, -140.0', '-87.63, 41.88'):
            with self.assertRaisesMessage(LocationError, 'inside the USA', msg=text):
                resolve_location(text)

    def test_coordinates_in_alaska_and_hawaii_are_accepted(self):
        self.assertEqual(resolve_location('61.22, -149.90').lat, 61.22)
        self.assertEqual(resolve_location('21.31, -157.86').lat, 21.31)

    def test_a_name_that_ends_like_a_country_is_left_whole(self):
        self.assertEqual(resolve_location('Azusa').name, 'Azusa, CA')
        self.assertEqual(resolve_location('Azusa, CA, USA').name, 'Azusa, CA')
        self.assertEqual(resolve_location('Azusa USA').name, 'Azusa, CA')

    def test_a_town_whose_name_ends_in_a_state_is_not_split(self):
        self.assertEqual(resolve_location('West New York').name, 'West New York, NJ')
        self.assertEqual(resolve_location('west new york nj').name, 'West New York, NJ')
        # A town followed by its state, without a comma, still reads as that.
        self.assertEqual(resolve_location('Olympia Washington').name, 'Olympia, WA')
        self.assertEqual(resolve_location('New York New York').name, 'New York, NY')

    def test_unique_name_needs_no_state(self):
        self.assertEqual(resolve_location('Chicago').name, 'Chicago, IL')

    def test_ambiguous_name_asks_for_a_state(self):
        with self.assertRaisesMessage(LocationError, 'Springfield, MO; Springfield, IL'):
            resolve_location('Springfield')

    def test_unknown_place(self):
        with self.assertRaisesMessage(LocationError, 'Could not find "Atlantis" in Texas'):
            resolve_location('Atlantis, TX')


class SuggestPlacesTests(TestCase):
    """What a search box is offered while a name is being typed."""

    @classmethod
    def setUpTestData(cls):
        def place(name, state, land, lat=40.0, lon=-90.0, alias=False):
            return Place(name=name, state=state, key=normalize(name), lat=lat, lon=lon, land_sqmi=land, is_alias=alias)

        Place.objects.bulk_create(
            [
                place('Chicago', 'IL', 228, lat=41.84),
                place('Chico', 'CA', 33, lat=39.76),
                place('Chistochina', 'AK', 351, lat=62.57),
                place('Chicago Heights', 'IL', 10, lat=41.51),
                place('Springfield', 'IL', 60, lat=39.79),
                place('Springfield', 'MO', 82, lat=37.19),
                place('Springfield', 'MA', 32, lat=42.11),
                place('Springfield', 'NJ', 5, lat=40.70),
                place('Springfield', 'NJ', 3, lat=40.04),  # two places of one name in one state
                place('St. Louis', 'MO', 62, lat=38.64),
                place('Nashville-Davidson', 'TN', 475, lat=36.17, lon=-86.78),
                place('Nashville', 'TN', 475, lat=36.17, lon=-86.78, alias=True),
            ]
        )

    def names(self, typed):
        return [str(place) for place in suggest_places(typed)]

    def test_names_that_start_with_what_was_typed_largest_first(self):
        self.assertEqual(self.names('chic'), ['Chicago, IL', 'Chico, CA', 'Chicago Heights, IL'])

    def test_it_is_one_query(self):
        with self.assertNumQueries(1):
            suggest_places('chi')

    def test_alaska_is_listed_after_the_rest_however_large(self):
        self.assertEqual(self.names('chi'), ['Chicago, IL', 'Chico, CA', 'Chicago Heights, IL', 'Chistochina, AK'])

    def test_case_spacing_and_abbreviations_do_not_matter(self):
        for typed in ('st l', 'ST. LOU', '  saint louis '):
            self.assertEqual(self.names(typed), ['St. Louis, MO'], typed)

    def test_a_state_narrows_it_with_or_without_a_comma(self):
        self.assertEqual(self.names('springfield, m'), ['Springfield, MO', 'Springfield, MA'])
        self.assertEqual(self.names('springfield, mass'), ['Springfield, MA'])
        self.assertEqual(self.names('springfield il'), ['Springfield, IL'])
        self.assertEqual(self.names('Springfield, IL, USA'), ['Springfield, IL'])
        self.assertEqual(self.names('springfield, zz'), [])

    def test_a_name_is_offered_once(self):
        self.assertEqual(self.names('springfield, nj'), ['Springfield, NJ'])
        # A consolidated city and its short form are one place: the short form is the one offered.
        self.assertEqual(self.names('nash'), ['Nashville, TN'])
        self.assertEqual(self.names('nashville-d'), ['Nashville-Davidson, TN'])

    def test_fewer_than_two_letters_find_nothing_and_ask_nothing(self):
        with self.assertNumQueries(0):
            for typed in ('', ' ', 'c', '4', ',', ', il'):
                self.assertEqual(self.names(typed), [], typed)

    def test_no_more_than_eight(self):
        Place.objects.bulk_create(
            Place(name=f'Dover {number}', state='OH', key=f'dover {number}', lat=40.0 + number / 100, lon=-81.0)
            for number in range(12)
        )
        self.assertEqual(len(self.names('dover')), 8)
