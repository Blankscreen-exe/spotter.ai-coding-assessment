from urllib.parse import urlencode

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from rest_framework.response import Response
from rest_framework.views import APIView

from . import conf
from .models import ProviderCredential
from .serializers import RouteRequestSerializer
from .services.stations import get_index
from .services.trip import plan_trip
from .throttling import RouteRateThrottle


class RoutePlanView(APIView):
    """Plan the cheapest fuel stops for a drive between two US locations.

    Accepts the same fields as a JSON body (POST) or query string (GET).
    Failures are turned into responses by handlers.api_exception_handler.
    """

    throttle_classes = [RouteRateThrottle]

    def get(self, request):
        # A query string arrives as a QueryDict, which DRF reads like an HTML form
        # (a missing boolean means False). A plain dict makes GET behave like POST.
        return self._respond(request, request.query_params.dict())

    def post(self, request):
        return self._respond(request, request.data)

    def _respond(self, request, params):
        serializer = RouteRequestSerializer(data=params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = plan_trip(
            data['start'],
            data['finish'],
            provider_name=data.get('provider'),
            initial_range_miles=data.get('initial_range_miles'),
            stop_cost=data.get('stop_cost'),
            include_geometry=data['include_geometry'],
        )
        query = urlencode({key: value for key, value in data.items() if key != 'include_geometry'})
        plan['map_url'] = request.build_absolute_uri(f'{reverse("route-map")}?{query}')
        return Response(plan)


class SettingsView(APIView):
    """What the server uses when a request does not say otherwise. Read-only.

    Changing a setting is done in the admin, behind a login. Nothing secret is
    returned: for a provider's API key, only whether one is stored.
    """

    def get(self, request):
        current = conf.load_settings()
        with_a_key = set(ProviderCredential.objects.values_list('provider', flat=True))
        return Response({
            'settings': [
                {
                    'key': key,
                    'label': conf.DEFINITIONS[key].label,
                    'value': current[key],
                    'default': conf.DEFINITIONS[key].parse(conf.DEFINITIONS[key].default),
                    'unit': conf.DEFINITIONS[key].unit,
                    'description': conf.DEFINITIONS[key].help_text,
                }
                for key in conf.DISPLAY_ORDER
            ],
            'providers': [
                {
                    'name': name,
                    'label': label,
                    'active': name == current[conf.ROUTING_PROVIDER],
                    'needs_key': name in conf.PROVIDERS_NEEDING_A_KEY,
                    'has_key': name in with_a_key,
                }
                for name, label in conf.PROVIDER_CHOICES
            ],
        })


def route_map(request):
    """The page people use. It plans nothing itself: its script calls the API above."""
    settings_now = conf.load_settings()
    return render(request, 'planner/map.html', {
        'config': {
            'apiUrl': reverse('route-plan'),
            'healthUrl': reverse('health'),
            'settingsUrl': reverse('settings'),
            'adminUrl': reverse('admin:planner_setting_changelist'),
            # The sliders start from what the server would use anyway.
            'defaults': {
                'stopCost': settings_now[conf.STOP_COST],
                'rangeMiles': settings_now[conf.RANGE_MILES],
                'mpg': settings_now[conf.MPG],
                'provider': settings_now[conf.ROUTING_PROVIDER],
            },
        },
    })


def health(request):
    """For load balancers and container health checks: 200 only when requests can be served."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        stations = len(get_index().stations)
    except DatabaseError:
        return JsonResponse({'status': 'unavailable', 'reason': 'database unreachable'}, status=503)
    if not stations:
        return JsonResponse({'status': 'unavailable', 'reason': 'no station data loaded'}, status=503)
    return JsonResponse({'status': 'ok', 'stations': stations})
