from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # The site's front door is the map page.
    path('', RedirectView.as_view(pattern_name='route-map', query_string=True), name='home'),
    path('api/v1/route/', views.RoutePlanView.as_view(), name='route-plan'),
    path('api/v1/settings/', views.SettingsView.as_view(), name='settings'),
    path('api/v1/session/', views.SessionView.as_view(), name='session'),
    path('map/', views.route_map, name='route-map'),
    path('healthz/', views.health, name='health'),
]
