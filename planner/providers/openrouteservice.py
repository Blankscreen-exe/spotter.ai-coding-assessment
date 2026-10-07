from .. import conf
from ..exceptions import ProviderNotConfigured, RouteNotFound, RoutingProviderError
from ..models import ProviderCredential
from ..services.places import Location
from .base import METERS_PER_MILE, Route, RoutingProvider
from .polyline import decode_polyline

# ORS error codes meaning the request was fine but no route exists.
NO_ROUTE_CODES = {2009, 2010}


class OpenRouteServiceProvider(RoutingProvider):
    name = conf.PROVIDER_ORS
    label = 'OpenRouteService'
    base_url_setting = 'ORS_BASE_URL'

    def route(self, start: Location, finish: Location) -> Route:
        credential = ProviderCredential.objects.filter(provider=self.name).first()
        if credential is None or not credential.api_key:
            raise ProviderNotConfigured(
                'OpenRouteService is selected but has no API key. Add one in the admin under '
                'Provider credentials, or run "python manage.py set_provider_key openrouteservice".'
            )
        response = self._send(
            'POST',
            f'{self.base_url}/v2/directions/driving-car',
            headers={'Authorization': credential.api_key},
            json={
                'coordinates': [[start.lon, start.lat], [finish.lon, finish.lat]],
                'instructions': False,
            },
        )
        body = self._json(response)
        if response.status_code in (401, 403):
            raise ProviderNotConfigured('OpenRouteService rejected the stored API key.')
        if response.status_code != 200:
            error = body.get('error')
            if isinstance(error, dict):
                if error.get('code') in NO_ROUTE_CODES:
                    raise RouteNotFound(f'No driving route between {start.name} and {finish.name}.')
                error = error.get('message')
            raise RoutingProviderError(
                f'OpenRouteService returned HTTP {response.status_code}: {error or "no detail"}.'
            )
        try:
            best = body['routes'][0]
            summary = best['summary']
            return Route(
                provider=self.name,
                coordinates=decode_polyline(best['geometry']),
                distance_miles=summary['distance'] / METERS_PER_MILE,
                duration_seconds=summary['duration'],
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise RoutingProviderError('OpenRouteService returned a response without a route.') from exc
