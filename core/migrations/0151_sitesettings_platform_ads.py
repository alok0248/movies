from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0150_emailsendlog_audience_emailsendlog_batch_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitesettings',
            name='ads_mobile_enabled',
            field=models.BooleanField(default=True, help_text='Master switch for advertising inside the Android app. Delivered to the app with its ad config.'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='ads_website_enabled',
            field=models.BooleanField(default=True, help_text='Master switch for advertising on this website (sponsor block and direct-link units).'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='ads_mobile_show_home',
            field=models.BooleanField(default=True, help_text='App: show banners on the home screen and the other browsing rows.'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='ads_mobile_show_detail',
            field=models.BooleanField(default=True, help_text='App: show banners on movie and series detail pages.'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='ads_mobile_show_player',
            field=models.BooleanField(default=False, help_text='App: show banners inside the player screens. Off by default: ads in a player cost watch time and invite complaints.'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='ads_website_show_home',
            field=models.BooleanField(default=True, help_text='Website: show ad units on the home page and general pages.'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='ads_website_show_detail',
            field=models.BooleanField(default=True, help_text='Website: show ad units on movie and series detail pages.'),
        ),
    ]
