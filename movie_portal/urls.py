from django.contrib import admin
from django.shortcuts import redirect
from django.urls import path, include

def redirect_to_custom_login(request):
    return redirect(f'/login/?next={request.GET.get("next", "/")}')

urlpatterns = [
    path('accounts/login/', redirect_to_custom_login, name='login_redirect'),
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
]
