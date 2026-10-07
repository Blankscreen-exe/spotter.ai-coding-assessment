import logging
from urllib.parse import urlencode

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import conf
from .exceptions import EncryptionNotConfigured, InvalidLogin, InvalidRequest, NotAllowed, NotSignedIn
from .models import ProviderCredential, Setting
from .serializers import RouteRequestSerializer
from .services.stations import get_index
from .services.trip import plan_trip
from .throttling import LoginRateThrottle, RouteRateThrottle

logger = logging.getLogger(__name__)


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


def _editor(user):
    """Who is asking, and what they may change."""
    signed_in = bool(user and user.is_authenticated)
    return {
        'signed_in': signed_in,
        'username': user.get_username() if signed_in else None,
        'can_edit': signed_in and user.has_perm('planner.change_setting'),
        'can_set_keys': signed_in and user.has_perm('planner.change_providercredential'),
    }


class SettingsView(APIView):
    """What the server uses when a request does not say otherwise.

    Anyone may read it. Changing it (PATCH) needs a signed-in account with
    permission to change settings, because the settings apply to every client.
    Nothing secret is returned: for a provider's API key, only whether one is stored.
    """

    # The route endpoint takes no credentials at all. This one accepts the same
    # session cookie as the admin, and with it Django's CSRF check on every write.
    authentication_classes = [SessionAuthentication]

    def get(self, request):
        return Response(self._describe(request))

    def patch(self, request):
        editor = _editor(request.user)
        if not editor['signed_in']:
            raise NotSignedIn('Sign in to change the server settings.')
        if not editor['can_edit']:
            raise NotAllowed('This account may not change the server settings.')
        body = request.data if isinstance(request.data, dict) else {}
        changes, keys = body.get('settings') or {}, body.get('provider_keys') or {}
        if not isinstance(changes, dict) or not isinstance(keys, dict) or not (changes or keys):
            raise InvalidRequest('Send {"settings": {...}} and/or {"provider_keys": {...}} with at least one change.')

        problems, parsed = {}, {}
        for key, raw in changes.items():
            definition = conf.DEFINITIONS.get(key)
            if definition is None:
                problems[key] = ['Unknown setting.']
                continue
            try:
                parsed[key] = definition.parse(raw)
            except ValueError as exc:
                problems[key] = [f'{definition.label} {exc}.']
        for provider, secret in keys.items():
            if provider not in conf.PROVIDERS_NEEDING_A_KEY:
                problems[f'provider_keys.{provider}'] = ['This provider does not take an API key.']
            elif not isinstance(secret, str) or not secret.strip():
                problems[f'provider_keys.{provider}'] = ['The API key is empty.']
            elif not editor['can_set_keys']:
                raise NotAllowed('This account may not store provider API keys.')
        # Switching to a provider that has no key would break every request after it.
        wanted = parsed.get(conf.ROUTING_PROVIDER)
        if (wanted in conf.PROVIDERS_NEEDING_A_KEY and wanted not in keys
                and not ProviderCredential.objects.filter(provider=wanted).exists()):
            problems[conf.ROUTING_PROVIDER] = ['Store an API key for this provider before switching to it.']
        if problems:
            raise ValidationError(problems)

        try:
            with transaction.atomic():
                for provider, secret in keys.items():
                    # Written without reading the old key back, so a key that can no longer be
                    # decrypted (the encryption key was replaced) can still be overwritten.
                    replaced = ProviderCredential.objects.filter(provider=provider).update(
                        api_key=secret.strip(), updated_at=timezone.now()
                    )
                    if not replaced:
                        ProviderCredential.objects.create(provider=provider, api_key=secret.strip())
                for key, value in parsed.items():
                    Setting.objects.update_or_create(key=key, defaults={'value': conf.to_stored(value)})
        except ImproperlyConfigured:
            raise EncryptionNotConfigured(
                'An API key cannot be stored until the server has an encryption key. '
                'Set CREDENTIALS_ENCRYPTION_KEYS (see the README) and restart.'
            )
        logger.info('Settings changed by %s: %s', editor['username'], ', '.join(
            [f'{key}={conf.to_stored(value)}' for key, value in sorted(parsed.items())]
            + [f'{provider} API key' for provider in sorted(keys)]
        ) or 'nothing')
        return Response(self._describe(request))

    def _describe(self, request):
        current = conf.load_settings()
        with_a_key = set(ProviderCredential.objects.values_list('provider', flat=True))
        return {
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
                    'key_page': conf.PROVIDER_KEY_PAGES.get(name),
                }
                for name, label in conf.PROVIDER_CHOICES
            ],
            'editor': _editor(request.user),
        }


class SessionView(APIView):
    """Sign in and out from the page, with the same accounts and session as the admin."""

    authentication_classes = [SessionAuthentication]

    def get_throttles(self):
        return [LoginRateThrottle()] if self.request.method == 'POST' else []

    def get(self, request):
        return Response(_editor(request.user))

    def post(self, request):
        # DRF checks the CSRF token only once someone is signed in; signing in needs it too.
        SessionAuthentication().enforce_csrf(request)
        user = authenticate(
            request._request, username=str(request.data.get('username', '')), password=str(request.data.get('password', ''))
        )
        if user is None:
            raise InvalidLogin('That username and password do not match an account.')
        login(request._request, user)
        return Response(_editor(user))

    def delete(self, request):
        logout(request._request)
        return Response(_editor(None))


@ensure_csrf_cookie  # the page's script needs the token to sign in and to save settings
def route_map(request):
    """The page people use. It plans nothing itself: its script calls the API above."""
    settings_now = conf.load_settings()
    return render(request, 'planner/map.html', {
        'config': {
            'apiUrl': reverse('route-plan'),
            'healthUrl': reverse('health'),
            'settingsUrl': reverse('settings'),
            'sessionUrl': reverse('session'),
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
