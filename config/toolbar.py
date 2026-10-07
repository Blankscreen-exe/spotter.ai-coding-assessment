"""Django Debug Toolbar: the SQL, cache calls and timing behind each request.

It is off unless DJANGO_DEBUG_TOOLBAR is set (see settings). When it is on it is
on for every visitor, because the local stack it is meant for runs with DEBUG
off and reaches Django through Docker's network, so the toolbar's usual test
(DEBUG on, request from 127.0.0.1) can never pass there.
"""

from django.urls import reverse


def show_toolbar(request):
    """Every request except the health check.

    The container checks its own health every ten seconds, which would push the
    requests worth looking at out of the toolbar's history within minutes.
    """
    return request.path != reverse('health')
