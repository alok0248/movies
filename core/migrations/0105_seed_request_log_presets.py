from django.db import migrations


# Ready-made saved views so the Request Log is useful out of the box.
PRESETS = [
    ('5xx errors', 'view=table&status=5xx&sort=created_at&dir=desc'),
    ('5xx API calls', 'view=table&category=api&status=5xx&sort=created_at&dir=desc'),
    ('Slow pages (>1s)', 'view=table&category=page&min_duration=1000&sort=duration_ms&dir=desc'),
    ('Most clicked pages', 'view=table&category=page&sort=click_count&dir=desc'),
]


def seed(apps, schema_editor):
    RequestLogPreset = apps.get_model('core', 'RequestLogPreset')
    for name, query in PRESETS:
        RequestLogPreset.objects.get_or_create(
            name=name,
            defaults={'query': query, 'is_system': True},
        )


def unseed(apps, schema_editor):
    RequestLogPreset = apps.get_model('core', 'RequestLogPreset')
    RequestLogPreset.objects.filter(
        name__in=[name for name, _ in PRESETS], is_system=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0104_requestlogpreset'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
