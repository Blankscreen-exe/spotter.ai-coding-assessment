from django.templatetags.static import static
from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # The site's front door is the map page.
    path('', RedirectView.as_view(pattern_name='route-map', query_string=True), name='home'),
    path('api/v1/route/', views.RoutePlanView.as_view(), name='route-plan'),
    # The same endpoint without the last slash. Django would redirect there, and a
    # client following the redirect sends a GET, so a POST would lose its body.
    path('api/v1/route', views.RoutePlanView.as_view()),
    path('api/v1/places/', views.PlaceSearchView.as_view(), name='places'),
    path('api/v1/settings/', views.SettingsView.as_view(), name='settings'),
    path('api/v1/session/', views.SessionView.as_view(), name='session'),
    path('map/', views.route_map, name='route-map'),
    path('healthz/', views.health, name='health'),
    # Browsers ask for this on pages that name no icon of their own, such as the admin.
    path('favicon.ico', RedirectView.as_view(url=static('planner/favicon.svg'))),
]
