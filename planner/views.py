from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import PlannerError
from .serializers import RouteRequestSerializer
from .services.trip import plan_trip


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
    """

    def get(self, request):
        return self._respond(request, request.query_params)

    def post(self, request):
        return self._respond(request, request.data)

    def _respond(self, request, params):
        try:
            data, plan = _plan(params)
        except ValidationError as exc:
            return Response(
                {'error': {'code': 'invalid_request', 'message': 'Invalid request.', 'fields': exc.detail}},
                status=400,
            )
        except PlannerError as exc:
            return Response({'error': {'code': exc.code, 'message': exc.message}}, status=exc.status_code)
        query = urlencode({key: value for key, value in data.items() if key != 'include_geometry'})
        plan['map_url'] = request.build_absolute_uri(f'{reverse("route-map")}?{query}')
        return Response(plan)


def route_map(request):
    """The same plan drawn on an interactive map, for people rather than clients."""
    context = {'params': request.GET, 'plan': None, 'error': None}
    if request.GET.get('start') and request.GET.get('finish'):
        try:
            _, context['plan'] = _plan({**request.GET.dict(), 'include_geometry': True})
        except PlannerError as exc:
            context['error'] = exc.message
        except ValidationError as exc:
            context['error'] = '; '.join(
                f'{field}: {" ".join(map(str, errors))}' for field, errors in exc.detail.items()
            )
    return render(request, 'planner/map.html', context)
