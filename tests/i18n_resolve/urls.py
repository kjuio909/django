"""
URLconf builders for the i18n URL resolution tests.

The patterns are constructed here (rather than as a single static module) so a
test can mount the same routes twice: once with i18n_patterns()' default
language prefix enabled and once with it disabled.
"""

from django.conf.urls.i18n import i18n_patterns
from django.urls import include, path, re_path, register_converter

from . import views
from .converters import BoundedConverter, ExplodingConverter

register_converter(BoundedConverter, "bounded")
register_converter(ExplodingConverter, "exploding")

# A namespaced, two-level include: shop/ -> market/ -> endpoints.
market_patterns = [
    path("item/<int:pk>/", views.item_view, name="item"),
    path("bounded/<bounded:name>/", views.bounded_view, name="market-bounded"),
]

shop_patterns = [
    path("market/", include((market_patterns, "market"), namespace="market")),
]


def i18n_urlconf(prefix_default_language):
    return i18n_patterns(
        path("simple/", views.simple_view, name="simple"),
        path("bounded/<bounded:name>/", views.bounded_view, name="bounded"),
        path("exploding/<exploding:word>/", views.simple_view, name="exploding"),
        # An old-style regex URL without a namespace.
        re_path(
            r"^legacy/(?P<legacy_id>[0-9]+)/$",
            views.legacy_view,
            name="legacy",
        ),
        path("shop/", include(shop_patterns)),
        prefix_default_language=prefix_default_language,
    )


# A plain, non-internationalized URLconf.
plain_urlconf = [
    path("plain/<int:pk>/", views.plain_view, name="plain"),
]

