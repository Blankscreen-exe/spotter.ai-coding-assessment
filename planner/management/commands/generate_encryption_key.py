from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Print a new key for CREDENTIALS_ENCRYPTION_KEYS.'

    def handle(self, *args, **options):
        self.stdout.write(Fernet.generate_key().decode())
