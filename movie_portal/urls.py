from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.urls import path, include

def redirect_to_custom_login(request):
    return redirect(f'/login/?next={request.GET.get("next", "/")}')

urlpatterns = [
    path('accounts/login/', redirect_to_custom_login, name='login_redirect'),
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
