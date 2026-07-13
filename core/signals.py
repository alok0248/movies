from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import PlayerConfiguration, AndroidApp


def update_android_apps_json_payload():
    """Helper function to build movie_servers/series_servers and update all AndroidApp json_payload in DB"""
    # Get all use_for_android=True and is_active=True PlayerConfigs
    android_players = PlayerConfiguration.objects.filter(
        use_for_android=True, is_active=True
    ).order_by('order', 'name')

    movie_servers = []
    series_servers = []

    for idx, player in enumerate(android_players, start=1):
        # Process movie server
        if player.media_type in ['movie', 'both']:
            movie_url = player.custom_movie_iframe_url or player.custom_iframe_url
            if not movie_url:
                # Default Vidking URL
                movie_url = "https://www.vidking.net/embed/movie/{id}"
                if player.player_color:
                    movie_url += f"?color={player.player_color}"
            # Replace placeholders with {id}
            movie_url = movie_url.replace("{tmdb_id}", "{id}")\
                                   .replace("{content_id}", "{id}")\
                                   .replace("{imdb_id}", "{id}")
            movie_servers.append({
                "name": f"Player {idx}",
                "url_template": movie_url
            })
        # Process series server
        if player.media_type in ['tv', 'both']:
            tv_url = player.custom_tv_iframe_url or player.custom_iframe_url
            if not tv_url:
                # Default Vidking URL
                tv_url = "https://www.vidking.net/embed/tv/{id}/{season}/{episode}"
                if player.player_color:
                    tv_url += f"?color={player.player_color}"
            # Replace placeholders with {id}
            tv_url = tv_url.replace("{tmdb_id}", "{id}")\
                           .replace("{content_id}", "{id}")\
                           .replace("{imdb_id}", "{id}")
            series_servers.append({
                "name": f"Player {len(series_servers) + 1}",
                "url_template": tv_url
            })

    # Now update all AndroidApp instances!
    for app in AndroidApp.objects.all():
        if isinstance(app.json_payload, dict):
            updated_payload = app.json_payload.copy()
            updated_payload['movie_servers'] = movie_servers
            updated_payload['series_servers'] = series_servers
            app.json_payload = updated_payload
            app.save()


@receiver(post_save, sender=PlayerConfiguration)
def player_config_saved(sender, instance, **kwargs):
    """Trigger update whenever PlayerConfiguration is created or modified"""
    update_android_apps_json_payload()


@receiver(post_delete, sender=PlayerConfiguration)
def player_config_deleted(sender, instance, **kwargs):
    """Trigger update whenever PlayerConfiguration is deleted"""
    update_android_apps_json_payload()
