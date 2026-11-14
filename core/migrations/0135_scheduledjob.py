from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0134_androidapp_legacy_user_auth_until"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScheduledJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=64, unique=True)),
                ("label", models.CharField(blank=True, default="", max_length=120)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("last_status", models.IntegerField(default=0)),
                ("last_ok", models.BooleanField(default=True)),
                ("runs", models.PositiveIntegerField(default=0)),
                ("failures", models.PositiveIntegerField(default=0)),
                ("last_detail", models.CharField(blank=True, default="", max_length=255)),
            ],
            options={
                "verbose_name": "Scheduled job",
                "verbose_name_plural": "Scheduled jobs",
                "ordering": ["name"],
            },
        ),
    ]
