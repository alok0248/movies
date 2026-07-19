

import os
import django
from django.conf import settings
from django.urls import reverse

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'movie_portal.settings')
django.setup()

try:
    url = reverse('system_resource_dashboard')
    print("Success! URL is:", url)
    url2 = reverse('ajax_system_resource_dashboard')
    print("Success! AJAX URL is:", url2)
except Exception as e:
    print("Error:", type(e).__name__, str(e))
    import traceback
    traceback.print_exc()

