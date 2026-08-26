"""
Sample data script for NewMovies.
Run: python manage.py shell < sample_data.py
Creates sample users, watchlist, play history, and sessions on the external DB.
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'movie_portal.settings')

# Ensure pymysql is installed as MySQLdb
try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass

django.setup()

from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
import random

# Determine which database to use
# Local: default (SQLite). Server with .env: external (MySQL)
try:
    from django.db import connections
    connections['external'].ensure_connection()
    use_external = True
except Exception:
    use_external = False
db = 'external' if use_external else 'default'

print(f"\n{'='*60}")
print(f"  Creating sample data on: {db.upper()} database")
print(f"{'='*60}\n")

# --- Sample Users ---
users_data = [
    {'username': 'alok', 'email': 'alok.AK639@gmail.com', 'first_name': 'Alok', 'last_name': 'Kumar'},
    {'username': 'priya', 'email': 'priya.sharma@gmail.com', 'first_name': 'Priya', 'last_name': 'Sharma'},
    {'username': 'rahul', 'email': 'rahul.verma@yahoo.com', 'first_name': 'Rahul', 'last_name': 'Verma'},
    {'username': 'nisha', 'email': 'nisha.singh@gmail.com', 'first_name': 'Nisha', 'last_name': 'Singh'},
    {'username': 'admin', 'email': 'admin@newmovies.com', 'first_name': 'Admin', 'last_name': 'User', 'is_staff': True, 'is_superuser': True},
]

users = []
for data in users_data:
    user, created = User.objects.using(db).get_or_create(
        username=data['username'],
        defaults={
            'email': data['email'],
            'first_name': data['first_name'],
            'last_name': data['last_name'],
            'is_staff': data.get('is_staff', False),
            'is_superuser': data.get('is_superuser', False),
        }
    )
    if created:
        user.set_password('testpass123')
        user.save(using=db)
        print(f"  Created user: {user.username} ({user.email})")
    else:
        print(f"  User exists: {user.username}")
    users.append(user)

# --- Sample Watchlist ---
from core.models import WatchList

watchlist_items = [
    {'tmdb_id': 550, 'media_type': 'movie', 'title': 'Fight Club', 'poster_path': '/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg'},
    {'tmdb_id': 299534, 'media_type': 'movie', 'title': 'Avengers: Endgame', 'poster_path': '/or06FN3Dka5tukK1e9sl16pB3iy.jpg'},
    {'tmdb_id': 155, 'media_type': 'movie', 'title': 'The Dark Knight', 'poster_path': '/qJ2tW6WMUDux911Z6D1FqyT6Cjs.jpg'},
    {'tmdb_id': 1399, 'media_type': 'tv', 'title': 'Game of Thrones', 'poster_path': '/1XS1oqL89opfnbLl8WnZY1O1uJx.jpg'},
    {'tmdb_id': 82856, 'media_type': 'tv', 'title': 'The Mandalorian', 'poster_path': '/sWgBv7LV2PRoQgkxwlibdGXKz1S.jpg'},
    {'tmdb_id': 27205, 'media_type': 'movie', 'title': 'Inception', 'poster_path': '/oYuLEt3zVCKq57qu2F8dT7NIa6f.jpg'},
    {'tmdb_id': 680, 'media_type': 'movie', 'title': 'Pulp Fiction', 'poster_path': '/d5iIlFn5s0ImsuYxUO13j9v8T1C.jpg'},
    {'tmdb_id': 238, 'media_type': 'movie', 'title': 'The Godfather', 'poster_path': '/3bhkrj58Vtu7enYsRolD1fZdja1.jpg'},
    {'tmdb_id': 11216, 'media_type': 'movie', 'title': 'Cinema Paradiso', 'poster_path': '/8SRUPfE6cpXbbrnS4w1FrT3PcCk.jpg'},
    {'tmdb_id': 87827, 'media_type': 'tv', 'title': 'Stranger Things', 'poster_path': '/49WJfeN0moxb9IPfGn8AIqMGskD.jpg'},
]

for user in users[:3]:  # First 3 users get watchlist
    for item in random.sample(watchlist_items, random.randint(3, 7)):
        wl, created = WatchList.objects.using(db).get_or_create(
            user=user,
            tmdb_id=item['tmdb_id'],
            media_type=item['media_type'],
            defaults={
                'title': item['title'],
                'poster_path': item['poster_path'],
            }
        )
        if created:
            print(f"  Added to {user.username}'s watchlist: {item['title']}")

# --- Sample Play History ---
from core.models import PlayHistory

play_items = [
    {'tmdb_id': 550, 'media_type': 'movie', 'title': 'Fight Club', 'poster_path': '/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg', 'duration': 7920, 'pos': 3540},
    {'tmdb_id': 299534, 'media_type': 'movie', 'title': 'Avengers: Endgame', 'poster_path': '/or06FN3Dka5tukK1e9sl16pB3iy.jpg', 'duration': 10140, 'pos': 8500},
    {'tmdb_id': 155, 'media_type': 'movie', 'title': 'The Dark Knight', 'poster_path': '/qJ2tW6WMUDux911R6D1FqyT6Cjs.jpg', 'duration': 9300, 'pos': 9300},
    {'tmdb_id': 27205, 'media_type': 'movie', 'title': 'Inception', 'poster_path': '/oYuLEt3zVCKq57qu2F8dT7NIa6f.jpg', 'duration': 8880, 'pos': 4440},
    {'tmdb_id': 680, 'media_type': 'movie', 'title': 'Pulp Fiction', 'poster_path': '/d5iIlFn5s0ImsuYxUO13j9v8T1C.jpg', 'duration': 9240, 'pos': 6160},
]

for user in users[:3]:
    for item in random.sample(play_items, random.randint(2, 4)):
        completed = item['pos'] / item['duration'] > 0.95
        ph, created = PlayHistory.objects.using(db).get_or_create(
            user=user,
            tmdb_id=item['tmdb_id'],
            media_type=item['media_type'],
            season_number=None,
            episode_number=None,
            defaults={
                'title': item['title'],
                'poster_path': item['poster_path'],
                'duration_seconds': item['pos'],
                'total_duration_seconds': item['duration'],
                'completed': completed,
            }
        )
        if created:
            pct = round(item['pos'] / item['duration'] * 100)
            status = 'completed' if completed else f'{pct}% watched'
            print(f"  Added to {user.username}'s history: {item['title']} ({status})")

# --- Sample Sessions ---
from core.models import UserSession

devices = [
    ('web', '', 'Chrome 128', 'Windows 11'),
    ('android', 'Pixel 8 Pro', 'Android 14', 'CineStream 1.9.8'),
    ('web', '', 'Safari 17', 'macOS Sonoma'),
]

for user in users[:3]:
    for source, model, ua, os_ver in random.sample(devices, random.randint(1, 2)):
        session = UserSession.objects.using(db).create(
            user=user,
            source=source,
            ip_address=f'192.168.1.{random.randint(1, 254)}',
            user_agent=ua,
            device_model=model,
            os_version=os_ver,
            app_version='1.9.8' if source == 'android' else '',
            is_active=random.choice([True, True, False]),
        )
        status = 'Active' if session.is_active else 'Offline'
        print(f"  Session for {user.username}: {source} ({status})")

# --- Synced User Profiles (Android) ---
from core.models import SyncedUser

for user in users[:3]:
    su, created = SyncedUser.objects.using(db).get_or_create(
        email=user.email,
        defaults={
            'user': user,
            'display_name': f'{user.first_name} {user.last_name}',
            'photo_url': f'https://ui-avatars.com/api/?name={user.first_name}+{user.last_name}&background=4F46E5&color=fff&size=128',
            'google_id': str(random.randint(100000000, 999999999)),
            'app_version': '1.9.8',
            'build_number': 198,
            'device_id': f'android_{random.randint(100000, 999999)}',
            'device_model': 'Pixel 8 Pro',
            'os_version': '14',
            'is_subscribed': user.username == 'alok',
            'plan': 'VIP Premium' if user.username == 'alok' else 'Standard Free',
        }
    )
    if created:
        print(f"  Synced profile: {user.first_name} {user.last_name} ({su.plan})")

# --- Cloud Data ---
from core.models import UserCloudData

for user in users[:3]:
    cloud, created = UserCloudData.objects.using(db).get_or_create(user=user)
    if created:
        # Add some playback progress
        cloud.playback_progress = {
            'movie_550_-1_-1': {
                'mediaId': 550, 'isTv': False, 'season': -1, 'episode': -1,
                'positionMs': 3540000, 'durationMs': 7920000,
                'lastUpdated': int(timezone.now().timestamp() * 1000),
            }
        }
        cloud.watchlist_ids = [
            {'mediaId': 299534, 'isTv': False, 'title': 'Avengers: Endgame', 'posterPath': '/or06FN3Dka5tukK1e9sl16pB3iy.jpg'},
            {'mediaId': 1399, 'isTv': True, 'title': 'Game of Thrones', 'posterPath': '/1XS1oqL89opfnbLl8WnZY1O1uJx.jpg'},
        ]
        cloud.save()
        print(f"  Cloud data for {user.username}: 1 playback + 2 watchlist items")

print(f"\n{'='*60}")
print(f"  Sample data created successfully!")
print(f"{'='*60}")
print(f"\n  Users: {len(users)}")
print(f"  Password for all: testpass123")
print(f"  DB: {db}")
print()
