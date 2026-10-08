"""The application for an ASGI server. The compose stack runs gunicorn through wsgi.py; nothing here is async."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_asgi_application()
