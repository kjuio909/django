"""
URLconf mounting the global django.contrib.admin.site. Models are registered
and unregistered by DefaultAdminSiteTests so other test modules are unaffected.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("default-admin/", admin.site.urls),
]
