from django.db import migrations


UNITS = [
    # (ad_type, name, unit_id, order)
    ("Popunder", "Popunder_1", "30866764", 0),
    ("Smartlink", "Smartlink_1", "30866772", 1),
    ("Social Bar", "SocialBar_1", "30866773", 2),
    ("Native Banner", "NativeBanner_1", "30866765", 3),
    ("Banner 320x50", "320x50_1", "30866766", 4),
]


def seed_units(apps, schema_editor):
    AdsterraLink = apps.get_model("core", "AdsterraLink")
    for ad_type, name, unit_id, order in UNITS:
        AdsterraLink.objects.get_or_create(
            unit_id=unit_id,
            defaults={
                "ad_type": ad_type,
                "name": name,
                "order": order,
                "is_active": True,
                "click_count": 0,
            },
        )


def unseed_units(apps, schema_editor):
    AdsterraLink = apps.get_model("core", "AdsterraLink")
    AdsterraLink.objects.filter(unit_id__in=[u[2] for u in UNITS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0098_adsterralink_ad_type_adsterralink_name_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_units, unseed_units),
    ]
