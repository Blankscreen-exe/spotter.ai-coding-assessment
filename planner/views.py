from django.contrib.auth import authenticate, login, logout
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.cache import patch_cache_control
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
    PlaceSuggestionSerializer,
    RouteRequestSerializer,
    ServerSettingsSerializer,
    SettingsChangeSerializer,
    SignInSerializer,
    TripSerializer,
)
from .services import server_settings
from .services.places import suggest_places
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
        asked = RouteRequestSerializer(data=params)
        asked.is_valid(raise_exception=True)
        trip_wanted = dict(asked.validated_data)
        # The two "include" switches say how much of the answer to send, not which trip to plan.
        shape = {name: trip_wanted.pop(name) for name in ('include_geometry', 'include_candidates')}
        trip = plan_trip(
            trip_wanted['start'],
            trip_wanted['finish'],
            provider_name=trip_wanted.get('provider'),
            initial_range_miles=trip_wanted.get('initial_range_miles'),
            stop_cost=trip_wanted.get('stop_cost'),
        )
        return Response(TripSerializer(trip, context={'request': request, 'asked': trip_wanted}, **shape).data)


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


class PlaceSearchView(APIView):
    """Places whose name starts with ?q=, for a search box: GET /api/v1/places/?q=chi.

    Open and read-only, like the route endpoint. One indexed query and no outside call, so
    it carries no rate limit; a browser may keep an answer for an hour, since the list
    only changes when the reference data is imported again.
    """

    def get(self, request):
        places = suggest_places(request.query_params.get('q', '')[:120])
        response = Response({'places': PlaceSuggestionSerializer(places, many=True).data})
        patch_cache_control(response, public=True, max_age=3600)
        return response


@ensure_csrf_cookie  # the page's script needs the token to sign in and to save settings
def route_map(request):
    """The page people use. It plans nothing itself: its script calls the API above."""
    settings_now = server_settings.load()
    return render(
        request,
        'planner/map.html',
        {
            'author': author_for_page(),
            'config': {
                'apiUrl': reverse('route-plan'),
                'placesUrl': reverse('places'),
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
        },
    )


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
