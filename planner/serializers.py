from rest_framework import serializers

from . import conf


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text='"City, ST" or "lat,lon"')
    finish = serializers.CharField(max_length=200, help_text='"City, ST" or "lat,lon"')
    provider = serializers.ChoiceField(choices=conf.PROVIDER_CHOICES, required=False)
    initial_range_miles = serializers.FloatField(min_value=0, required=False)
    stop_cost = serializers.FloatField(min_value=0, required=False)
    include_geometry = serializers.BooleanField(required=False, default=True)
    include_candidates = serializers.BooleanField(required=False, default=False)
