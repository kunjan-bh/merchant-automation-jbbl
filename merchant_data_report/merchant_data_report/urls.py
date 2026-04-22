"""URL configuration for merchant_data_report project.

`/admin/` is our custom log viewer (see merchant/auth.py). Django's built-in
admin is mounted at `/django-admin/` in case it's needed for DB inspection.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('django-admin/', admin.site.urls),
    path('', include('merchant.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
