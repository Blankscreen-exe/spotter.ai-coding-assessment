"""Which runtime settings exist: their names, defaults and how a value is checked.

Every setting has a code default here, so the app runs on an empty table; a row
in the Setting table overrides the default without a deploy. Reading and
writing that table is the job of services/server_settings.py.
"""

import math
from dataclasses import dataclass

ROUTING_PROVIDER = 'routing.provider'
CORRIDOR_MILES = 'stations.corridor_miles'
RANGE_MILES = 'vehicle.range_miles'
MPG = 'vehicle.mpg'
STOP_COST = 'stops.cost_per_stop'

PROVIDER_OSRM = 'osrm'
PROVIDER_ORS = 'openrouteservice'
PROVIDER_CHOICES = [
    (PROVIDER_OSRM, 'OSRM public server (no key)'),
    (PROVIDER_ORS, 'OpenRouteService (API key)'),
]


def _number(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError('must be a number')
    if not math.isfinite(value):
        raise ValueError('must be a finite number')
    return value


def _positive_float(raw):
    value = _number(raw)
    if not value > 0:
        raise ValueError('must be greater than zero')
    return value


def _non_negative_float(raw):
    value = _number(raw)
    if not value >= 0:
        raise ValueError('must be zero or more')
    return value


def _provider(raw):
    value = str(raw).strip().lower()
    if value not in dict(PROVIDER_CHOICES):
        raise ValueError('must be one of: ' + ', '.join(dict(PROVIDER_CHOICES)))
    return value


@dataclass(frozen=True)
class Definition:
    default: str
    parse: callable
    help_text: str
    label: str = ''
    unit: str = ''  # how a value is shown to people: 'USD', 'miles', 'mpg' or nothing


# Providers that cannot be used until an API key is stored for them, and where to get one.
PROVIDER_KEY_PAGES = {PROVIDER_ORS: 'https://openrouteservice.org/dev/#/signup'}
PROVIDERS_NEEDING_A_KEY = set(PROVIDER_KEY_PAGES)

DEFINITIONS = {
    ROUTING_PROVIDER: Definition(
        PROVIDER_OSRM, _provider,
        'Routing API used when a request does not name one: osrm or openrouteservice.',
        label='Routing provider',
    ),
    CORRIDOR_MILES: Definition(
        '5', _positive_float,
        'How far from the route line a station may be and still count as on the route.',
        label='Station corridor', unit='miles',
    ),
    RANGE_MILES: Definition(
        '500', _positive_float, 'Distance the vehicle covers on a full tank.', label='Vehicle range', unit='miles',
    ),
    MPG: Definition('10', _positive_float, 'Fuel economy in miles per gallon.', label='Fuel economy', unit='mpg'),
    STOP_COST: Definition(
        '5', _non_negative_float,
        'Dollars one extra fuel stop is worth avoiding. 0 gives the cheapest fuel bill regardless of stops.',
        label='Cost per stop', unit='USD',
    ),
}

# The order people read them in: what is tuned most often comes first.
DISPLAY_ORDER = (ROUTING_PROVIDER, STOP_COST, RANGE_MILES, MPG, CORRIDOR_MILES)


def to_stored(value):
    """A parsed value as the text kept in the Setting table: 8.0 is stored as "8"."""
    return f'{value:g}' if isinstance(value, float) else str(value)

