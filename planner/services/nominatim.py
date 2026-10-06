"""One-off geocoding of towns the Census file does not list.

Only the import command calls this, never a request. Nominatim's usage policy
allows one request per second and asks for results to be cached, so answers
(including "not found") are kept in a JSON file that is committed to the repo.
"""

import json
import time

import requests

from .places import STATES

URL = 'https://nominatim.openstreetmap.org/search'
USER_AGENT = 'fuel-route-planner-assessment/1.0 (one-off import of US town centroids)'
SECONDS_BETWEEN_REQUESTS = 1.1


class NominatimCache:
    def __init__(self, path):
        self.path = path
        self.entries = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self._last_request = 0.0

    @staticmethod
    def key(city, state):
        return f'{city.upper()}|{state}'

    def get(self, city, state):
        """Cached [lat, lon], None for a cached miss, or KeyError if never asked."""
        return self.entries[self.key(city, state)]

    def save(self):
        self.path.write_text(json.dumps(self.entries, indent=0, sort_keys=True) + '\n', encoding='utf-8')

    def fetch(self, city, state):
        """Ask Nominatim, remember the answer, and return [lat, lon] or None."""
        params = {'format': 'jsonv2', 'limit': 1, 'countrycodes': 'us'}
        result = self._search({**params, 'city': city, 'state': STATES[state], 'country': 'USA'})
        if result is None:
            result = self._search({**params, 'q': f'{city}, {STATES[state]}, USA'})
        self.entries[self.key(city, state)] = result
        return result

    def _search(self, params):
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = requests.get(URL, params=params, headers={'User-Agent': USER_AGENT}, timeout=30)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        hits = response.json()
        if not hits:
            return None
        return [round(float(hits[0]['lat']), 6), round(float(hits[0]['lon']), 6)]
