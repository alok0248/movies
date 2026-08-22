import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'movie_portal.settings')
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

# Find the user with username "admin"
admin_user = User.objects.filter(username='admin').first()

if admin_user:
    admin_user.set_password('Admin@12345')
    admin_user.save()
    print(f"Successfully reset password for user: {admin_user.username}")
else:
    # If no admin exists, create one
    admin_user = User.objects.create_superuser(
        username='admin',
        email='admin@example.com',
        password='Admin@12345'
    )
    print("Created new admin user with username: admin")
