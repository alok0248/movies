from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0153_remove_ads_mobile_show_player'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sitesettings',
            name='ads_website_show_home',
            field=models.BooleanField(
                default=True,
                help_text='Website: show ad units on the home page. Other pages (search, lists, account) follow the master switch.'),
        ),
        migrations.AlterField(
            model_name='sitesettings',
            name='ads_website_show_detail',
            field=models.BooleanField(
                default=True,
                help_text='Website: show ad units on movie, series, collection and person detail pages.'),
        ),
    ]
