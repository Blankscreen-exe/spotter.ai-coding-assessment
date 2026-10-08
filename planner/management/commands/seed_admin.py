import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

DEMO_USERNAME = 'admin'
DEMO_PASSWORD = 'fuelroute-demo'


class Command(BaseCommand):
    help = (
        'Create the admin user for /admin/ if it does not exist. Reads '
        'DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_PASSWORD and DJANGO_SUPERUSER_EMAIL; '
        'with DEBUG on it falls back to a demo login.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='If the user already exists, set its password again instead of leaving it alone.',
        )

    def handle(self, *args, **options):
        username = os.environ.get('DJANGO_SUPERUSER_USERNAME') or DEMO_USERNAME
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
        if not password:
            # A password that is published in the repository must never guard a real deployment.
            if not settings.DEBUG:
                raise CommandError('Set DJANGO_SUPERUSER_PASSWORD; the demo password is only used when DEBUG is on.')
            password = DEMO_PASSWORD

        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is None:
            User.objects.create_superuser(username, os.environ.get('DJANGO_SUPERUSER_EMAIL', ''), password)
            self.stdout.write(self.style.SUCCESS(f'Created admin user "{username}".'))
        elif options['reset_password']:
            user.set_password(password)
            user.is_staff = user.is_superuser = user.is_active = True
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Reset the password of admin user "{username}".'))
        else:
            self.stdout.write(f'Admin user "{username}" already exists, left unchanged.')
