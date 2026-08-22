from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0041_playerconfiguration_custom_iframe_html_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='AndroidApp',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, unique=True)),
                ('slug', models.SlugField(blank=True, max_length=255, unique=True)),
                ('access_username', models.CharField(max_length=255)),
                ('access_password', models.CharField(max_length=255)),
                ('allowed_endpoint', models.CharField(blank=True, default='', max_length=500)),
                ('json_payload', models.JSONField(blank=True, default=dict)),
                ('is_active', models.BooleanField(default=True)),
                ('total_connections', models.PositiveIntegerField(default=0)),
                ('last_accessed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Android App',
                'verbose_name_plural': 'Android Apps',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='AndroidAppAccessLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('access_date', models.DateField()),
                ('connection_count', models.PositiveIntegerField(default=0)),
                ('last_accessed_at', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('android_app', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='access_logs', to='core.androidapp')),
            ],
            options={
                'verbose_name': 'Android App Access Log',
                'verbose_name_plural': 'Android App Access Logs',
                'ordering': ['-access_date'],
                'unique_together': {('android_app', 'access_date')},
            },
        ),
        migrations.CreateModel(
            name='AndroidAppBuildLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('build_identifier', models.CharField(max_length=255)),
                ('access_date', models.DateField()),
                ('connection_count', models.PositiveIntegerField(default=0)),
                ('last_accessed_at', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('android_app', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='build_logs', to='core.androidapp')),
            ],
            options={
                'verbose_name': 'Android App Build Log',
                'verbose_name_plural': 'Android App Build Logs',
                'ordering': ['-access_date', 'build_identifier'],
                'unique_together': {('android_app', 'build_identifier', 'access_date')},
            },
        ),
    ]
