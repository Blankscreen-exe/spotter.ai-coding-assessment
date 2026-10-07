from urllib.parse import urlencode

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import PlannerError
from .serializers import RouteRequestSerializer
from .services.stations import get_index
from .services.trip import plan_trip
from .throttling import RouteRateThrottle


def _plan(params):
    """Validate request parameters and plan the trip. Returns (data, plan)."""
    # A query string arrives as a QueryDict, which DRF reads like an HTML form
    # (a missing boolean means False). A plain dict makes GET behave like POST.
    if hasattr(params, 'dict'):
        params = params.dict()
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
    return data, plan


class RoutePlanView(APIView):
    """Plan the cheapest fuel stops for a drive between two US locations.

    Accepts the same fields as a JSON body (POST) or query string (GET).
    Failures are turned into responses by handlers.api_exception_handler.
    """

    throttle_classes = [RouteRateThrottle]

    def get(self, request):
        return self._respond(request, request.query_params)

    def post(self, request):
        return self._respond(request, request.data)

    def _respond(self, request, params):
        data, plan = _plan(params)
        query = urlencode({key: value for key, value in data.items() if key != 'include_geometry'})
        plan['map_url'] = request.build_absolute_uri(f'{reverse("route-map")}?{query}')
        return Response(plan)


def route_map(request):
    """The same plan drawn on an interactive map, for people rather than clients."""
    context = {'params': request.GET, 'plan': None, 'error': None}
    if request.GET.get('start') and request.GET.get('finish'):
        if not RouteRateThrottle().allow_request(request, None):
            context['error'] = 'Too many requests. Please wait a minute and try again.'
            return render(request, 'planner/map.html', context, status=429)
        try:
            _, context['plan'] = _plan({**request.GET.dict(), 'include_geometry': True})
        except PlannerError as exc:
            context['error'] = exc.message
        except ValidationError as exc:
            context['error'] = '; '.join(
                f'{field}: {" ".join(map(str, errors))}' for field, errors in exc.detail.items()
            )
    return render(request, 'planner/map.html', context)


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
