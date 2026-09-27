from django.urls import include, path

from .views import empty_view

app_name = "resolved-paths-mid"

# Second include level. Its own route captures "section"; the include fills
# a default of the same name, so resolving a concrete path must restore the
# captured value while a missing one uses the default.
urlpatterns = [
    path(
        "sec/<slug:section>/",
        include("urlpatterns_reverse.resolved_paths_inner"),
        {"section": "default-section"},
    ),
    # Plain view inside the app namespace, used to prove that include
    # prefixes are not accidentally exposed as captured parameters.
    path("index/", empty_view, name="index"),
]
