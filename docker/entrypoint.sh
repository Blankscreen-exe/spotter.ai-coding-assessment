#!/bin/sh
# Bring the database up to date and load the reference data on first start,
# then hand over to the server command.
set -e

python manage.py migrate --noinput
python manage.py import_places --if-empty
python manage.py import_stations --if-empty
if [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    python manage.py seed_admin
fi

exec "$@"
