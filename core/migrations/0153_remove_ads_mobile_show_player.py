from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0152_remove_adsterra'),
    ]

    operations = [
        migrations.RemoveField(model_name='sitesettings', name='ads_mobile_show_player'),
    ]
