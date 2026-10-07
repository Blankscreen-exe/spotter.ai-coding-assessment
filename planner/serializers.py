"""What the API accepts and what it returns. Views and services hold no field names or formats."""

from urllib.parse import urlencode

from django.urls import reverse
from rest_framework import serializers

from . import conf
from .exceptions import InvalidRequest, NotAllowed
from .permissions import CHANGE_SETTINGS, SET_PROVIDER_KEYS
from .services import server_settings


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text='"City, ST" or "lat,lon"')
    finish = serializers.CharField(max_length=200, help_text='"City, ST" or "lat,lon"')
    provider = serializers.ChoiceField(choices=conf.PROVIDER_CHOICES, required=False)
    initial_range_miles = serializers.FloatField(min_value=0, required=False)
    stop_cost = serializers.FloatField(min_value=0, required=False)
    include_geometry = serializers.BooleanField(required=False, default=True)
    include_candidates = serializers.BooleanField(required=False, default=False)


# ---------- a planned trip ----------


class Rounded(serializers.FloatField):
    """A number given to a fixed count of decimal places."""

    def __init__(self, places, **kwargs):
        self.places = places
        super().__init__(**kwargs)

    def to_representation(self, value):
        return round(float(value), self.places)


class LocationSerializer(serializers.Serializer):
    query = serializers.CharField()
    name = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()


class RouteStationSerializer(serializers.BaseSerializer):
    """A station and where it sits on the route: one the planner considered, or (extended below) a fuel stop.

    Read-only, and written out rather than declared field by field: one response
    can hold several hundred of these, and this is several times quicker.
    """

    def to_representation(self, on_route):
        station = on_route.station
        return {
            'station_id': station.opis_id,
            'name': station.name,
            'address': station.address,
            'city': station.city,
            'state': station.state,
            'lat': station.lat,
            'lon': station.lon,
            'mile_marker': round(on_route.mile, 1),
            'miles_off_route': round(on_route.off_route_miles, 1),
            'price_per_gallon': round(station.price, 3),
        }


class FuelStopSerializer(RouteStationSerializer):
    def to_representation(self, stop):
        return {
            'order': stop.order,
            **super().to_representation(stop),
            'gallons_on_arrival': round(stop.gallons_on_arrival, 2),
            'gallons_purchased': round(stop.gallons_purchased, 2),
            'cost': float(stop.cost),
        }


class SummarySerializer(serializers.Serializer):
    distance_miles = Rounded(1)
    duration_hours = Rounded(2)
    fuel_stops = serializers.SerializerMethodField()
    total_fuel_cost = serializers.FloatField(source='total_cost')
    gallons_purchased = Rounded(2)
    gallons_used = Rounded(2)
    currency = serializers.SerializerMethodField()

    def get_fuel_stops(self, plan):
        return len(plan.stops)

    def get_currency(self, plan):
        return 'USD'


class VehicleSerializer(serializers.Serializer):
    max_range_miles = serializers.FloatField(source='range_miles')
    miles_per_gallon = serializers.FloatField(source='mpg')
    initial_range_miles = serializers.FloatField()


class PlanningSerializer(serializers.Serializer):
    stop_cost = serializers.FloatField()
    corridor_miles = serializers.FloatField()


class MetaSerializer(serializers.Serializer):
    routing_provider = serializers.CharField(source='plan.provider')
    routing_api_calls = serializers.IntegerField(source='routing_calls')
    served_from = serializers.CharField()
    stations_considered = serializers.SerializerMethodField()
    elapsed_ms = serializers.FloatField()

    def get_stations_considered(self, trip):
        return len(trip.plan.candidates)


class TripSerializer(serializers.Serializer):
    """A planned trip (services.trip.Trip) as the API returns it.

    The route line and the list of stations considered are the two heavy parts,
    so each can be left out. The context carries the request, and under "asked"
    what the client asked for, which is what the link to the map repeats.
    """

    start = LocationSerializer()
    finish = LocationSerializer()
    summary = SummarySerializer(source='plan')
    vehicle = VehicleSerializer(source='plan')
    planning = PlanningSerializer(source='plan')
    fuel_stops = FuelStopSerializer(source='plan.stops', many=True)
    candidate_stations = RouteStationSerializer(source='plan.candidates', many=True)
    route = serializers.SerializerMethodField()
    meta = MetaSerializer(source='*')
    map_url = serializers.SerializerMethodField()

    def __init__(self, *args, include_geometry=True, include_candidates=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not include_geometry:
            self.fields.pop('route')
        if not include_candidates:
            self.fields.pop('candidate_stations')

    def get_route(self, trip):
        return {
            'type': 'Feature',
            'properties': {'provider': trip.plan.provider},
            'geometry': {'type': 'LineString', 'coordinates': trip.plan.geometry},
        }

    def get_map_url(self, trip):
        return self.context['request'].build_absolute_uri(f'{reverse("route-map")}?{urlencode(self.context["asked"])}')


# ---------- signing in ----------


class SignInSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class EditorSerializer(serializers.Serializer):
    """Who is asking, and what they may change. Takes a user, signed in or not."""

    signed_in = serializers.BooleanField(source='is_authenticated')
    username = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_set_keys = serializers.SerializerMethodField()

    def get_username(self, user):
        return user.get_username() if user.is_authenticated else None

    def get_can_edit(self, user):
        return user.has_perm(CHANGE_SETTINGS)

    def get_can_set_keys(self, user):
        return user.has_perm(SET_PROVIDER_KEYS)


# ---------- server settings ----------


class SettingStateSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    value = serializers.ReadOnlyField()
    default = serializers.ReadOnlyField()
    unit = serializers.CharField()
    description = serializers.CharField()


class ProviderStateSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()
    active = serializers.BooleanField()
    needs_key = serializers.BooleanField()
    has_key = serializers.BooleanField()
    key_page = serializers.CharField()


class ServerSettingsSerializer(serializers.Serializer):
    """The settings as they stand, with what the reader may do about them. Needs the request in its context."""

    settings = SettingStateSerializer(many=True)
    providers = ProviderStateSerializer(many=True)
    editor = serializers.SerializerMethodField()

    def get_editor(self, _):
        return EditorSerializer(self.context['request'].user).data


class SettingsChangeSerializer(serializers.Serializer):
    """New values for settings, API keys for providers, or both. Needs the request in its context.

    Problems are reported in one flat object under the names the client sent
    ("vehicle.mpg", "provider_keys.openrouteservice"), so each message can be
    shown beside its own field. Nothing is saved unless all of it is valid.
    """

    settings = serializers.DictField(required=False)
    provider_keys = serializers.DictField(required=False)

    def validate(self, data):
        changes, keys = data.get('settings') or {}, data.get('provider_keys') or {}
        if not (changes or keys):
            raise InvalidRequest('Send {"settings": {...}} and/or {"provider_keys": {...}} with at least one change.')
        # The permission to change settings does not cover the providers' API keys.
        if keys and not self.context['request'].user.has_perm(SET_PROVIDER_KEYS):
            raise NotAllowed('This account may not store provider API keys.')

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
        # Switching to a provider that has no key would break every request after it.
        wanted = parsed.get(conf.ROUTING_PROVIDER)
        if (
            wanted in conf.PROVIDERS_NEEDING_A_KEY
            and wanted not in keys
            and wanted not in server_settings.providers_with_a_key()
        ):
            problems[conf.ROUTING_PROVIDER] = ['Store an API key for this provider before switching to it.']
        if problems:
            raise serializers.ValidationError(problems)
        return {'settings': parsed, 'provider_keys': {provider: secret.strip() for provider, secret in keys.items()}}

    def save(self):
        server_settings.change(**self.validated_data, changed_by=self.context['request'].user.get_username())
