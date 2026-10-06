import csv
import io
import re
import zipfile

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import FuelStation, Place
from planner.services.places import STATES, normalize
from planner.services.stations import reset_index

# Consolidated city-counties are filed under a joint name. People (and the fuel
# file) use the city alone, so that gets an alias row.
CONSOLIDATED = re.compile(r'government|\(balance\)|urban county|metro', re.IGNORECASE)
# A few places are filed as "Town of Pecos"; they get an alias under the bare name.
LEGAL_PREFIX = re.compile(r'^(?:Town|City|Village|Borough) of (?=\S)')


def split_descriptor(census_name):
    """Split "Abbeville city" into ("Abbeville", "city").

    The Census appends a legal descriptor (city, town, CDP, "metropolitan
    government (balance)") in lower case, so trailing lower-case words go.
    """
    words = census_name.split()
    end = len(words)
    while end > 1 and (words[end - 1][0].islower() or words[end - 1] in ('CDP', '(balance)')):
        end -= 1
    return ' '.join(words[:end]), ' '.join(words[end:])


class Command(BaseCommand):
    help = 'Load US city/town centroids from the Census Gazetteer places file.'

    def add_arguments(self, parser):
        parser.add_argument('--zip', default=str(settings.CENSUS_PLACES_ZIP), help='Gazetteer zip file.')
        parser.add_argument('--if-empty', action='store_true', help='Do nothing if places are already loaded.')

    def handle(self, *args, **options):
        census = Place.objects.filter(source=Place.SOURCE_CENSUS)
        if options['if_empty'] and census.exists():
            self.stdout.write('Places already loaded, skipping.')
            return
        try:
            archive = zipfile.ZipFile(options['zip'])
        except (OSError, zipfile.BadZipFile) as exc:
            raise CommandError(f'Cannot read {options["zip"]}: {exc}')
        with archive, archive.open(archive.namelist()[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8'), delimiter='|')
            reader.fieldnames = [name.strip() for name in reader.fieldnames]
            places = []
            for row in reader:
                state = row['USPS'].strip()
                if state not in STATES:
                    continue
                name, descriptor = split_descriptor(row['NAME'].strip())
                common = dict(
                    state=state,
                    lat=float(row['INTPTLAT']),
                    lon=float(row['INTPTLONG']),
                    land_sqmi=float(row['ALAND_SQMI']),
                )
                places.append(Place(name=name, key=normalize(name), **common))
                short = LEGAL_PREFIX.sub('', name)
                if short == name and CONSOLIDATED.search(descriptor):
                    short = re.split(r'[-/]', name)[0].strip()
                if short != name:
                    places.append(Place(name=short, key=normalize(short), is_alias=True, **common))

        unlinked = FuelStation.objects.filter(place__source=Place.SOURCE_CENSUS).count()
        with transaction.atomic():
            census.delete()
            Place.objects.bulk_create(places, batch_size=2000)
        reset_index()
        cache.clear()  # cached plans were built from the old data
        aliases = sum(place.is_alias for place in places)
        self.stdout.write(self.style.SUCCESS(f'Loaded {len(places) - aliases} places and {aliases} aliases.'))
        if unlinked:
            self.stdout.write(self.style.WARNING(
                f'{unlinked} stations pointed at the replaced places and are now unlocated. '
                'Run "python manage.py import_stations" to link them again.'
            ))
