"""Runtime settings backed by the Setting table.

Every setting has a code default here, so the app runs on an empty table; a row
in the table overrides the default without a deploy.
"""

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


def _positive_float(raw):
    value = float(raw)
    if not value > 0:
        raise ValueError('must be greater than zero')
    return value


def _non_negative_float(raw):
    value = float(raw)
    if not value >= 0:
        raise ValueError('must be zero or more')
    return value


def _provider(raw):
    value = raw.strip().lower()
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


# Providers that cannot be used until an API key is stored for them.
PROVIDERS_NEEDING_A_KEY = {PROVIDER_ORS}

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


def load_settings():
    """All settings as parsed values, read with a single query."""
    from .models import Setting

    stored = dict(Setting.objects.values_list('key', 'value'))
    return {
        key: definition.parse(stored.get(key, definition.default))
        for key, definition in DEFINITIONS.items()
    }
