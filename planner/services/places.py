"""Offline place lookup: "City, ST" text to coordinates without a network call."""

import re
import unicodedata
from dataclasses import dataclass

from ..exceptions import LocationError
from ..models import Place

STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California',
    'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware', 'DC': 'District of Columbia',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois',
    'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana',
    'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
    'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon',
    'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota',
    'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia',
    'WA': 'Washington', 'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}
STATE_BY_NAME = {name.lower(): code for code, name in STATES.items()}

# Rough box around the 50 states; catches swapped or foreign coordinates early.
US_LAT = (18.0, 72.0)
US_LON = (-180.0, -66.0)

ABBREVIATIONS = {'st': 'saint', 'ste': 'sainte', 'ft': 'fort', 'mt': 'mount'}
COORDINATES = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*$')
COUNTRY_SUFFIX = re.compile(r',?\s*(usa|u\.s\.a\.|united states(?: of america)?)\s*$', re.IGNORECASE)


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
        if not (US_LAT[0] <= lat <= US_LAT[1] and US_LON[0] <= lon <= US_LON[1]):
            raise LocationError(f'"{query}" is not a latitude,longitude inside the USA.')
        return Location(query, f'{lat:.5f}, {lon:.5f}', lat, lon)

    city, state = _split_state(COUNTRY_SUFFIX.sub('', query))
    if state:
        place = find_place(city, state)
        if place is None:
            raise LocationError(
                f'Could not find "{city}" in {STATES[state]}. Check the spelling or pass "lat,lon".'
            )
        return Location(query, str(place), place.lat, place.lon)

    matches = []
    for key in key_variants(city):
        matches = list(Place.objects.filter(key=key, is_alias=False).order_by('-land_sqmi')[:6])
        if matches:
            break
    if len(matches) == 1:
        return Location(query, str(matches[0]), matches[0].lat, matches[0].lon)
    if matches:
        options = '; '.join(str(place) for place in matches[:5])
        raise LocationError(f'"{query}" matches several places ({options}). Add the state, e.g. "City, ST".')
    raise LocationError(f'Could not find "{query}". Use "City, ST" or "lat,lon".')
