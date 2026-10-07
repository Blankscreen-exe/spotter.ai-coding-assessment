from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from . import conf
from .crypto import EncryptedTextField


class Place(models.Model):
    """A US city or town and the coordinates of its centre.

    Used offline for two jobs: turning a request's "City, ST" into coordinates,
    and giving fuel stations (which come without coordinates) a position.
    """

    SOURCE_CENSUS = 'census'
    SOURCE_NOMINATIM = 'nominatim'
    SOURCE_CHOICES = [
        (SOURCE_CENSUS, 'Census Gazetteer'),
        (SOURCE_NOMINATIM, 'Nominatim (OpenStreetMap)'),
    ]

    name = models.CharField(max_length=120)
    state = models.CharField(max_length=2)
    key = models.CharField(max_length=120, help_text='Normalised name used for matching.')
    lat = models.FloatField()
    lon = models.FloatField()
    land_sqmi = models.FloatField(default=0, help_text='Breaks ties between places sharing a name.')
    is_alias = models.BooleanField(
        default=False, help_text='Short form of a consolidated name, e.g. Nashville for Nashville-Davidson.'
    )
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default=SOURCE_CENSUS)

    class Meta:
        indexes = [models.Index(fields=['key', 'state'], name='place_key_state_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(lat__gte=-90) & Q(lat__lte=90), name='place_lat_in_range'),
            models.CheckConstraint(condition=Q(lon__gte=-180) & Q(lon__lte=180), name='place_lon_in_range'),
        ]

    def __str__(self):
        return f'{self.name}, {self.state}'


class FuelStation(models.Model):
    """One truck stop from the price file, keyed by its OPIS ID.

    city and state are kept as the file gives them. Coordinates are not stored
    here: a station is positioned by the Place it was matched to, and a station
    with no place is simply never offered as a stop.
    """

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2)
    price = models.DecimalField(
        max_digits=8, decimal_places=5, help_text='USD per gallon; mean of the rows sharing this OPIS ID.'
    )
    place = models.ForeignKey(Place, null=True, blank=True, on_delete=models.SET_NULL, related_name='stations')

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(price__gt=0), name='fuelstation_price_positive'),
        ]

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'


class Setting(models.Model):
    """One row per runtime setting. Allowed keys and defaults live in conf.py."""

    key = models.CharField(max_length=64, unique=True, choices=[(k, k) for k in conf.DEFINITIONS])
    value = models.CharField(max_length=200)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.key} = {self.value}'

    def clean(self):
        definition = conf.DEFINITIONS.get(self.key)
        if definition is None:
            raise ValidationError({'key': 'Unknown setting.'})
        try:
            definition.parse(self.value)
        except ValueError as exc:
            raise ValidationError({'value': f'Invalid value: {exc}'}) from exc


class ProviderCredential(models.Model):
    """API key for a routing provider, stored encrypted (see crypto.py)."""

    provider = models.CharField(max_length=32, unique=True, choices=conf.PROVIDER_CHOICES)
    api_key = EncryptedTextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.provider

    def masked(self):
        return '*' * 8 + self.api_key[-4:] if self.api_key else ''
