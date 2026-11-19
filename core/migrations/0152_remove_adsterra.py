from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0151_sitesettings_platform_ads'),
    ]

    operations = [
        migrations.DeleteModel(name='AdsterraLink'),
        migrations.DeleteModel(name='AdNetworkTag'),
        migrations.RemoveField(model_name='sitesettings', name='adsterra_clicks_required'),
        migrations.RemoveField(model_name='sitesettings', name='adsterra_show_banner'),
        migrations.RemoveField(model_name='sitesettings', name='enable_adsterra_links'),
        migrations.AlterField(
            model_name='ad',
            name='provider',
            field=models.CharField(
                choices=[('google_adsense', 'Google AdSense'),
                         ('amazon_affiliate', 'Amazon Affiliate')],
                default='google_adsense', max_length=50),
        ),
    ]
