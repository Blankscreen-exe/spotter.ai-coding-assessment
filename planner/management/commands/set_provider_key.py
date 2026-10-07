import getpass
import os

from django.core.management.base import BaseCommand, CommandError

from planner import conf
from planner.services import server_settings


class Command(BaseCommand):
    help = 'Store a routing provider API key, encrypted, and optionally make that provider the default.'

    def add_arguments(self, parser):
        parser.add_argument('provider', choices=[conf.PROVIDER_ORS])
        parser.add_argument(
            '--from-env',
            metavar='VAR',
            help='Read the key from this environment variable instead of prompting.',
        )
        parser.add_argument('--activate', action='store_true', help='Also set routing.provider to this provider.')

    def handle(self, *args, **options):
        # The key is never accepted as an argument: it would land in shell history.
        api_key = os.environ.get(options['from_env'], '') if options['from_env'] else getpass.getpass('API key: ')
        api_key = api_key.strip()
        if not api_key:
            raise CommandError('No API key given.')

        # The same call the API makes, so there is one way to store a key and the change is logged.
        activate = {conf.ROUTING_PROVIDER: options['provider']} if options['activate'] else {}
        server_settings.change(activate, {options['provider']: api_key}, changed_by='the set_provider_key command')
        self.stdout.write(self.style.SUCCESS(f'Stored encrypted key for {options["provider"]}.'))
        if options['activate']:
            self.stdout.write(self.style.SUCCESS(f'routing.provider is now {options["provider"]}.'))
