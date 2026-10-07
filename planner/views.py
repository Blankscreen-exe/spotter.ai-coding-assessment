from urllib.parse import urlencode

from django.contrib.auth import authenticate, login, logout
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

from . import conf
from .authentication import CsrfCheckedSessionAuthentication
from .author import author_for_page
from .exceptions import InvalidLogin
from .permissions import CanChangeSettings
from .serializers import (
    EditorSerializer,
    RouteRequestSerializer,
    ServerSettingsSerializer,
    SettingsChangeSerializer,
    SignInSerializer,
)
from .services import server_settings
from .services.stations import get_index
from .services.trip import plan_trip
from .throttling import LoginRateThrottle, RouteRateThrottle


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
            include_candidates=data['include_candidates'],
        )
        query = urlencode({key: value for key, value in data.items() if not key.startswith('include_')})
        plan['map_url'] = request.build_absolute_uri(f'{reverse("route-map")}?{query}')
        return Response(plan)


class SettingsView(APIView):
    """What the server uses when a request does not say otherwise.

    Anyone may read it. Changing it (PATCH) needs a signed-in account with
    permission to change settings, because the settings apply to every client.
    Nothing secret is returned: for a provider's API key, only whether one is stored.
    """

    # The route endpoint takes no credentials at all. This one accepts the same
    # session cookie as the admin, and with it Django's CSRF check on every write.
    authentication_classes = [SessionAuthentication]
    permission_classes = [CanChangeSettings]

    def get(self, request):
        return Response(ServerSettingsSerializer(server_settings.describe(), context={'request': request}).data)

    def patch(self, request):
        change = SettingsChangeSerializer(data=request.data, context={'request': request})
        change.is_valid(raise_exception=True)
        change.save()
        return self.get(request)


class SessionView(APIView):
    """Sign in and out from the page, with the same accounts and session as the admin."""

    authentication_classes = [CsrfCheckedSessionAuthentication]

    def get_throttles(self):
        return [LoginRateThrottle()] if self.request.method == 'POST' else []

    def get(self, request):
        return Response(EditorSerializer(request.user).data)

    def post(self, request):
        credentials = SignInSerializer(data=request.data)
        credentials.is_valid(raise_exception=True)
        user = authenticate(request, **credentials.validated_data)
        if user is None:
            raise InvalidLogin('That username and password do not match an account.')
        login(request, user)
        return Response(EditorSerializer(user).data)

    def delete(self, request):
        logout(request)
        return Response(EditorSerializer(request.user).data)


@ensure_csrf_cookie  # the page's script needs the token to sign in and to save settings
def route_map(request):
    """The page people use. It plans nothing itself: its script calls the API above."""
    settings_now = server_settings.load()
    return render(request, 'planner/map.html', {
        'author': author_for_page(),
        'config': {
            'apiUrl': reverse('route-plan'),
            'healthUrl': reverse('health'),
            'settingsUrl': reverse('settings'),
            'sessionUrl': reverse('session'),
            # The page's two counters start from what the server would use anyway.
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
