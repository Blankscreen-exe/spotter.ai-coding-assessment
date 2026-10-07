import csv
from collections import Counter, defaultdict
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import FuelStation, Place
from planner.services.nominatim import NominatimCache
from planner.services.places import STATES, key_variants, normalize
from planner.services.stations import reset_index


class Command(BaseCommand):
    help = (
        'Load fuel stations from the price CSV and link each one to a Place: '
        'a Census town first, a cached Nominatim result second.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=str(settings.FUEL_PRICES_CSV))
        parser.add_argument(
            '--geocode-missing', action='store_true',
            help='Query Nominatim (1 request/second) for towns that are in neither the '
                 'Census file nor the cache, and add the answers to the cache.',
        )
        parser.add_argument('--if-empty', action='store_true', help='Do nothing if stations are already loaded.')

    def handle(self, *args, **options):
        if options['if_empty'] and FuelStation.objects.exists():
            self.stdout.write('Stations already loaded, skipping.')
            return
        if not Place.objects.filter(source=Place.SOURCE_CENSUS).exists():
            raise CommandError('No places loaded. Run "python manage.py import_places" first.')
        try:
            with open(options['csv'], newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
        except OSError as exc:
            raise CommandError(f'Cannot read {options["csv"]}: {exc}')

        # One station per OPIS ID. Rows sharing an ID are the same truck stop
        # listed under several names, sometimes with different prices.
        grouped, outside_us = defaultdict(list), 0
        for row in rows:
            if row['State'].strip() not in STATES:
                outside_us += 1
                continue
            grouped[int(row['OPIS Truckstop ID'])].append(row)

        stations = []
        for opis_id, group in grouped.items():
            first = group[0]
            prices = [Decimal(row['Retail Price']) for row in group]
            stations.append(FuelStation(
                opis_id=opis_id,
                name=first['Truckstop Name'].strip(),
                address=first['Address'].strip(),
                city=first['City'].strip(),
                state=first['State'].strip(),
                price=sum(prices) / len(prices),
            ))

        census = self._census_places()
        lookups = NominatimCache(settings.NOMINATIM_CACHE)
        town_place = {}  # (city, state) as the CSV spells it -> Place id
        geocoded, never_asked = {}, set()  # towns the Census file does not list
        for city, state in {(station.city, station.state) for station in stations}:
            match = next((census[(key, state)] for key in key_variants(city) if (key, state) in census), None)
            if match:
                town_place[(city, state)] = match
                continue
            try:
                geocoded[(city, state)] = lookups.get(city, state)
            except KeyError:
                never_asked.add((city, state))
        if options['geocode_missing'] and never_asked:
            geocoded.update(self._ask_nominatim(lookups, never_asked))
            never_asked = set()

        with transaction.atomic():
            FuelStation.objects.all().delete()
            Place.objects.filter(source=Place.SOURCE_NOMINATIM).delete()
            extra = Place.objects.bulk_create([
                Place(
                    name=city, state=state, key=normalize(city),
                    lat=position[0], lon=position[1], source=Place.SOURCE_NOMINATIM,
                )
                for (city, state), position in sorted(geocoded.items()) if position
            ])
            by_nominatim = {(place.name, place.state) for place in extra}
            town_place.update({(place.name, place.state): place.pk for place in extra})
            for station in stations:
                station.place_id = town_place.get((station.city, station.state))
            FuelStation.objects.bulk_create(stations, batch_size=2000)
        reset_index()
        cache.clear()  # cached plans were built from the old data

        sources = Counter(
            Place.SOURCE_NOMINATIM if (station.city, station.state) in by_nominatim else Place.SOURCE_CENSUS
            for station in stations if station.place_id
        )
        located = sum(sources.values())
        self.stdout.write(f'CSV rows read:              {len(rows)}')
        self.stdout.write(f'  outside the USA, dropped: {outside_us}')
        self.stdout.write(f'  duplicate rows merged:    {len(rows) - outside_us - len(stations)}')
        self.stdout.write(f'Stations stored:            {len(stations)}')
        self.stdout.write(f'  located by Census:        {sources[Place.SOURCE_CENSUS]}')
        self.stdout.write(f'  located by Nominatim:     {sources[Place.SOURCE_NOMINATIM]}')
        self.stdout.write(f'  not located (unused):     {len(stations) - located}')
        if never_asked:
            self.stdout.write(self.style.WARNING(
                f'{len(never_asked)} towns are in neither the Census file nor the cache. '
                'Re-run with --geocode-missing to look them up.'
            ))

    def _census_places(self):
        """Map (key, state) to a Place id, preferring real names and larger places."""
        places = {}
        rows = Place.objects.filter(source=Place.SOURCE_CENSUS).order_by('is_alias', '-land_sqmi')
        for key, state, pk in rows.values_list('key', 'state', 'pk'):
            places.setdefault((key, state), pk)
        return places

    def _ask_nominatim(self, lookups, towns):
        self.stdout.write(f'Asking Nominatim about {len(towns)} towns, about 1 per second...')
        answers = {}
        try:
            for done, town in enumerate(sorted(towns), start=1):
                answers[town] = lookups.fetch(*town)
                if done % 25 == 0:
                    lookups.save()
                    self.stdout.write(f'  {done}/{len(towns)}')
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f'Nominatim lookup stopped early: {exc}'))
        finally:
            lookups.save()
        return answers
