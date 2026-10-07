from django.urls import path

from . import views

urlpatterns = [
    path('api/v1/route/', views.RoutePlanView.as_view(), name='route-plan'),
    path('map/', views.route_map, name='route-map'),
    path('healthz/', views.health, name='health'),
]
