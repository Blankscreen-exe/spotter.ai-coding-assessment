"""Encryption at rest for secrets stored in the database."""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

PREFIX = 'fernet:'


def _fernet():
    keys = settings.CREDENTIALS_ENCRYPTION_KEYS
    if not keys:
        raise ImproperlyConfigured(
            'CREDENTIALS_ENCRYPTION_KEYS is not set. Generate a key with '
            '"python manage.py generate_encryption_key" and put it in .env.'
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError as exc:
        raise ImproperlyConfigured(f'CREDENTIALS_ENCRYPTION_KEYS holds an invalid Fernet key: {exc}')


def encrypt(plaintext):
    return PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.removeprefix(PREFIX).encode()).decode()
    except InvalidToken:
        raise ImproperlyConfigured(
            'A stored credential could not be decrypted with the configured '
            'CREDENTIALS_ENCRYPTION_KEYS. Re-enter the credential.'
        )


class EncryptedTextField(models.TextField):
    """Text that is plaintext in Python and Fernet ciphertext in the database."""

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return encrypt(value)
