from django.conf.urls.i18n import i18n_patterns
from django.urls import path, re_path

from .resolved_paths_localized import localized_patterns
from .views import empty_view

# Same routes as resolved_paths_urls, but the default language is served
# without a prefix while non-default public languages still carry one.
legacy_patterns = [
    path("plain/<int:id>/", empty_view, name="plain"),
    re_path(r"^old-style/(?P<code>[0-9]+)/$", empty_view, name="old-style"),
]

urlpatterns = legacy_patterns + i18n_patterns(
    *localized_patterns, prefix_default_language=False
)
