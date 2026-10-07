from .. import conf
from ..exceptions import RouteNotFound, RoutingProviderError
from .base import METERS_PER_MILE, Route, RoutingProvider
from .polyline import decode_polyline


class OSRMProvider(RoutingProvider):
    name = conf.PROVIDER_OSRM
    label = 'OSRM'
    base_url_setting = 'OSRM_BASE_URL'

    def route(self, start, finish):
        url = (
            f'{self.base_url}/route/v1/driving/'
            f'{start.lon:.6f},{start.lat:.6f};{finish.lon:.6f},{finish.lat:.6f}'
        )
        response = self._send('GET', url, params={
            'overview': 'full', 'geometries': 'polyline', 'steps': 'false', 'alternatives': 'false',
        })
        body = self._json(response)
        code = body.get('code')
        if code in ('NoRoute', 'NoSegment'):
            raise RouteNotFound(f'No driving route between {start.name} and {finish.name}.')
        if response.status_code != 200 or code != 'Ok' or not body.get('routes'):
            raise RoutingProviderError(
                f'OSRM returned HTTP {response.status_code}: {body.get("message") or code or "no route"}.'
            )
        best = body['routes'][0]
        return Route(
            provider=self.name,
            coordinates=decode_polyline(best['geometry']),
            distance_miles=best['distance'] / METERS_PER_MILE,
            duration_seconds=best['duration'],
        )
