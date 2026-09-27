from django.urls import include, path

from .views import empty_view

app_name = "ns-defaults-mid"

# An app-namespaced include whose own route captures a parameter that the
# include() supplies a default for.
urlpatterns = [
    path(
        "mid/<int:mid>/",
        include("urlpatterns_reverse.ns_defaults_inner"),
        {"mid": 6},
    ),
    path("passthrough/<int:pk>/", empty_view, name="passthrough"),
]
