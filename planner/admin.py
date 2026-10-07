from django import forms
from django.contrib import admin

from . import conf
from .models import FuelStation, Place, ProviderCredential, Setting


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'description', 'updated_at')

    @admin.display(description='What it does')
    def description(self, obj):
        definition = conf.DEFINITIONS.get(obj.key)
        return definition.help_text if definition else ''


class ProviderCredentialForm(forms.ModelForm):
    # Write-only: the stored key is never rendered back into the page.
    api_key = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text='Leave blank to keep the stored key.',
    )

    class Meta:
        model = ProviderCredential
        fields = ('provider', 'api_key')

    def clean_api_key(self):
        value = self.cleaned_data['api_key'].strip()
        if value:
            return value
        if self.instance.pk:
            return self.instance.api_key
        raise forms.ValidationError('An API key is required.')


@admin.register(ProviderCredential)
class ProviderCredentialAdmin(admin.ModelAdmin):
    form = ProviderCredentialForm
    list_display = ('provider', 'masked', 'updated_at')


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('opis_id', 'name', 'city', 'state', 'price', 'place')
    list_filter = ('place__source', 'state')
    list_select_related = ('place',)
    search_fields = ('name', 'city', '=opis_id')
    raw_id_fields = ('place',)


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ('name', 'state', 'lat', 'lon', 'source', 'is_alias')
    list_filter = ('source', 'state')
    search_fields = ('name',)
