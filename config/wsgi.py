"""What gunicorn and runserver load: the application, and the one-off work done as a server process starts."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()

# Imported after Django is set up. Runs once per server process.
from planner.warmup import warm_up  # noqa: E402

warm_up()
