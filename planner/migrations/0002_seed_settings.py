from django.db import migrations

from planner import conf


def seed(apps, schema_editor):
    Setting = apps.get_model('planner', 'Setting')
    for key, definition in conf.DEFINITIONS.items():
        Setting.objects.get_or_create(key=key, defaults={'value': definition.default})


class Migration(migrations.Migration):
    dependencies = [('planner', '0001_initial')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
