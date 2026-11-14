from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0135_scheduledjob"),
    ]

    operations = [
        migrations.CreateModel(
            name="CannedReply",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=120)),
                ("body", models.TextField()),
                ("sort_order", models.IntegerField(default=100)),
                ("is_active", models.BooleanField(default=True)),
                ("uses", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Canned reply",
                "verbose_name_plural": "Canned replies",
                "ordering": ["sort_order", "title"],
            },
        ),
        migrations.CreateModel(
            name="SupportTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=300, unique=True)),
                ("email", models.EmailField(blank=True, default="", max_length=254,
                                            db_index=True)),
                ("device_id", models.CharField(blank=True, default="", max_length=100,
                                               db_index=True)),
                ("token", models.CharField(max_length=64, unique=True)),
                ("acked_at", models.DateTimeField(blank=True, null=True)),
                ("csat_asked_at", models.DateTimeField(blank=True, null=True)),
                ("rating", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("rated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("app", models.ForeignKey(blank=True, null=True,
                                          on_delete=django.db.models.deletion.SET_NULL,
                                          related_name="support_tickets",
                                          to="core.androidapp")),
            ],
            options={
                "verbose_name": "Support ticket",
                "verbose_name_plural": "Support tickets",
                "ordering": ["-created_at"],
            },
        ),
    ]
