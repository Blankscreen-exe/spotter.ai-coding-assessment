"""The admin, plus everything the planner app serves (the API, the map page, the health check)."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('planner.urls')),
]

if settings.DEBUG_TOOLBAR:
    # Included directly: the toolbar's own debug_toolbar_urls() helper returns
    # nothing unless DEBUG is on, and the compose stack runs with DEBUG off.
    urlpatterns.append(path('__debug__/', include('debug_toolbar.urls')))
