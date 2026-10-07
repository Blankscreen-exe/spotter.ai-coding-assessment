"""Offline place lookup: "City, ST" text to coordinates without a network call."""

import re
import unicodedata
from dataclasses import dataclass

from ..exceptions import LocationError
from ..models import Place

STATES = {
    'AL': 'Alabama',
    'AK': 'Alaska',
    'AZ': 'Arizona',
    'AR': 'Arkansas',
    'CA': 'California',
    'CO': 'Colorado',
    'CT': 'Connecticut',
    'DE': 'Delaware',
    'DC': 'District of Columbia',
    'FL': 'Florida',
    'GA': 'Georgia',
    'HI': 'Hawaii',
    'ID': 'Idaho',
    'IL': 'Illinois',
    'IN': 'Indiana',
    'IA': 'Iowa',
    'KS': 'Kansas',
    'KY': 'Kentucky',
    'LA': 'Louisiana',
    'ME': 'Maine',
    'MD': 'Maryland',
    'MA': 'Massachusetts',
    'MI': 'Michigan',
    'MN': 'Minnesota',
    'MS': 'Mississippi',
    'MO': 'Missouri',
    'MT': 'Montana',
    'NE': 'Nebraska',
    'NV': 'Nevada',
    'NH': 'New Hampshire',
    'NJ': 'New Jersey',
    'NM': 'New Mexico',
    'NY': 'New York',
    'NC': 'North Carolina',
    'ND': 'North Dakota',
    'OH': 'Ohio',
    'OK': 'Oklahoma',
    'OR': 'Oregon',
    'PA': 'Pennsylvania',
    'RI': 'Rhode Island',
    'SC': 'South Carolina',
    'SD': 'South Dakota',
    'TN': 'Tennessee',
    'TX': 'Texas',
    'UT': 'Utah',
    'VT': 'Vermont',
    'VA': 'Virginia',
    'WA': 'Washington',
    'WV': 'West Virginia',
    'WI': 'Wisconsin',
    'WY': 'Wyoming',
}
STATE_BY_NAME = {name.lower(): code for code, name in STATES.items()}

# Rough boxes around the lower 48 states, Alaska and Hawaii: (south, north, west, east).
# They catch swapped, mistyped and far-off coordinates before a routing call is spent
# on them. They are not the border: a point just across it (Toronto, Tijuana) is
# inside a box and is routed like any other.
US_BOXES = (
    (24.4, 49.4, -125.0, -66.9),
    (51.0, 71.5, -180.0, -129.9),
    (18.9, 22.3, -160.3, -154.7),
)

ABBREVIATIONS = {'st': 'saint', 'ste': 'sainte', 'ft': 'fort', 'mt': 'mount'}
COORDINATES = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*$')
# A country after the place, set off by a comma or a space, so that "Azusa" is left whole.
COUNTRY_SUFFIX = re.compile(r'(?:\s*,\s*|\s+)(usa|u\.s\.a\.|united states(?: of america)?)\s*$', re.IGNORECASE)


@dataclass(frozen=True)
class Location:
    query: str
    name: str
    lat: float
    lon: float


def normalize(name: str) -> str:
    """Fold a place name to a matching key: "St. Louis" -> "saint louis"."""
    text = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    text = text.replace('&', ' and ')
    text = re.sub(r"[.'`]", '', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    text = ' '.join(ABBREVIATIONS.get(token, token) for token in text.split())
    # The fuel file writes "Mc Lean" where the Census writes "McLean".
    return re.sub(r'\bmc (?=[a-z])', 'mc', text)


def key_variants(name: str) -> list[str]:
    """Keys to try in order: as given, then with/without a trailing "city"."""
    key = normalize(name)
    variants = [key]
    if key.endswith(' city'):
        variants.append(key.removesuffix(' city'))
    else:
        variants.append(key + ' city')
    return variants


def find_place(name: str, state: str) -> Place | None:
    for key in key_variants(name):
        place = Place.objects.filter(key=key, state=state).order_by('is_alias', '-land_sqmi').first()
        if place:
            return place
    return None


def _places_named(name: str) -> list[Place]:
    """The places with this name in any state, largest first. A handful at most."""
    for key in key_variants(name):
        matches = list(Place.objects.filter(key=key, is_alias=False).order_by('-land_sqmi')[:6])
        if matches:
            return matches
    return []


def _split_state(text: str) -> tuple[str, str | None]:
    """Split "Chicago, IL" / "Chicago IL" / "Chicago, Illinois" into (city, state code)."""
    if ',' in text:
        city, _, tail = text.rpartition(',')
        tail = tail.strip()
        code = tail.upper() if tail.upper() in STATES else STATE_BY_NAME.get(tail.lower())
        if code:
            return city.strip(), code
        return text, None
    words = text.split()
    for size in (3, 2, 1):
        if len(words) > size:
            tail = ' '.join(words[-size:])
            code = STATE_BY_NAME.get(tail.lower()) or (tail.upper() if tail.upper() in STATES else None)
            if code:
                return ' '.join(words[:-size]), code
    return text, None


def resolve_location(text: str) -> Location:
    """Turn request text into a Location, or raise LocationError."""
    query = text.strip()
    match = COORDINATES.match(query)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not any(south <= lat <= north and west <= lon <= east for south, north, west, east in US_BOXES):
            raise LocationError(f'"{query}" is not a latitude,longitude inside the USA.')
        return Location(query, f'{lat:.5f}, {lon:.5f}', lat, lon)

    text = COUNTRY_SUFFIX.sub('', query)
    city, state = _split_state(text)
    matches = []
    if state is None or ',' not in text:
        # Without a comma, a name that ends in a state's name may be a town in its own right:
        # "West New York" is in New Jersey, and is not "West" in New York.
        matches = _places_named(text)
    if state and not matches:
        place = find_place(city, state)
        if place is None:
            raise LocationError(f'Could not find "{city}" in {STATES[state]}. Check the spelling or pass "lat,lon".')
        return Location(query, str(place), place.lat, place.lon)

    if len(matches) == 1:
        return Location(query, str(matches[0]), matches[0].lat, matches[0].lon)
    if matches:
        options = '; '.join(str(place) for place in matches[:5])
        raise LocationError(f'"{query}" matches several places ({options}). Add the state, e.g. "City, ST".')
    raise LocationError(f'Could not find "{query}". Use "City, ST" or "lat,lon".')
