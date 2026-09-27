from django.urls import path

from .views import empty_view

app_name = "ns-defaults-inner"

urlpatterns = [
    path("leaf/<int:pk>/", empty_view, name="leaf"),
    # A default for a non-pattern parameter that every reversal must match.
    path("themed/<int:pk>/", empty_view, {"theme": "light"}, name="themed"),
]
