"""What the API accepts and what it returns. Views and services hold no field names or formats."""

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
        if (wanted in conf.PROVIDERS_NEEDING_A_KEY and wanted not in keys
                and wanted not in server_settings.providers_with_a_key()):
            problems[conf.ROUTING_PROVIDER] = ['Store an API key for this provider before switching to it.']
        if problems:
            raise serializers.ValidationError(problems)
        return {'settings': parsed, 'provider_keys': {provider: secret.strip() for provider, secret in keys.items()}}

    def save(self):
        server_settings.change(
            **self.validated_data, changed_by=self.context['request'].user.get_username()
        )
