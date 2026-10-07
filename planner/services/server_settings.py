"""The server's runtime settings and provider API keys: reading them and changing them.

conf.py says which settings exist. This module is the only code that reads or
writes the two tables behind them, so the planner, the API, the page and the
set_provider_key command all go through one place.
"""

import logging
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from .. import conf
from ..exceptions import EncryptionNotConfigured
from ..models import ProviderCredential, Setting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingState:
    key: str
    label: str
    value: Any
    default: Any
    unit: str
    description: str


@dataclass(frozen=True)
class ProviderState:
    name: str
    label: str
    active: bool
    needs_key: bool
    has_key: bool
    key_page: str | None


@dataclass(frozen=True)
class ServerSettings:
    """What the server would use for a request that names nothing itself."""

    settings: list[SettingState]
    providers: list[ProviderState]


def load() -> dict[str, Any]:
    """Every setting as a parsed value, read with a single query."""
    stored = dict(Setting.objects.values_list('key', 'value'))
    return {
        key: definition.parse(stored.get(key, definition.default))
        for key, definition in conf.DEFINITIONS.items()
    }


def providers_with_a_key() -> set[str]:
    return set(ProviderCredential.objects.values_list('provider', flat=True))


def describe() -> ServerSettings:
    """The settings in the order people read them, and each provider's state. Never a key itself."""
    current, with_a_key = load(), providers_with_a_key()
    return ServerSettings(
        settings=[
            SettingState(
                key=key,
                label=conf.DEFINITIONS[key].label,
                value=current[key],
                default=conf.DEFINITIONS[key].parse(conf.DEFINITIONS[key].default),
                unit=conf.DEFINITIONS[key].unit,
                description=conf.DEFINITIONS[key].help_text,
            )
            for key in conf.DISPLAY_ORDER
        ],
        providers=[
            ProviderState(
                name=name,
                label=label,
                active=name == current[conf.ROUTING_PROVIDER],
                needs_key=name in conf.PROVIDERS_NEEDING_A_KEY,
                has_key=name in with_a_key,
                key_page=conf.PROVIDER_KEY_PAGES.get(name),
            )
            for name, label in conf.PROVIDER_CHOICES
        ],
    )


def change(settings: dict[str, Any], provider_keys: dict[str, str], changed_by: str) -> None:
    """Save new setting values and provider keys together, or nothing at all.

    settings holds parsed values (see conf.DEFINITIONS). The change is logged
    with who made it; a key is logged as having changed, never its value.
    """
    try:
        with transaction.atomic():
            for provider, api_key in provider_keys.items():
                _store_key(provider, api_key)
            for key, value in settings.items():
                Setting.objects.update_or_create(key=key, defaults={'value': conf.to_stored(value)})
    except ImproperlyConfigured:
        raise EncryptionNotConfigured(
            'An API key cannot be stored until the server has an encryption key. '
            'Set CREDENTIALS_ENCRYPTION_KEYS (see the README) and restart.'
        )
    logger.info('Settings changed by %s: %s', changed_by, ', '.join(
        [f'{key}={conf.to_stored(value)}' for key, value in sorted(settings.items())]
        + [f'{provider} API key' for provider in sorted(provider_keys)]
    ) or 'nothing')


def _store_key(provider: str, api_key: str) -> None:
    # Written without reading the old key back, so a key that can no longer be
    # decrypted (the encryption key was replaced) can still be overwritten.
    replaced = ProviderCredential.objects.filter(provider=provider).update(
        api_key=api_key, updated_at=timezone.now()
    )
    if not replaced:
        ProviderCredential.objects.create(provider=provider, api_key=api_key)
