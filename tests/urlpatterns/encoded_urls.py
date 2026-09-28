from django.conf.urls.i18n import i18n_patterns
from django.urls import include, path, re_path, register_converter

from . import views
from .converters import DecodedFourDigitYearConverter, DecodedIntConverter

register_converter(DecodedIntConverter, "decint")
register_converter(DecodedFourDigitYearConverter, "decyear")

# Innermost URLconf, reused below both a plain two-level include and an
# i18n-prefixed include.
inner_urlpatterns = [
    path("article/<int:pk>/", views.empty_view, name="article"),
    path("slug/<slug:slug>/", views.empty_view, name="slug"),
    path("file/<path:rest>/", views.empty_view, name="file"),
    path("word/<str:word>/", views.empty_view, name="word"),
    path("count/<decint:number>/", views.empty_view, name="count"),
    path("year/<decyear:year>/", views.empty_view, name="year"),
    path(
        "defaulted/<int:pk>/",
        views.empty_view,
        {"pk": 3, "verified": True},
        name="defaulted",
    ),
]

middle_urlpatterns = [
    path(
        "sec/<slug:section>/",
        include((inner_urlpatterns, "inner"), namespace="inner"),
    ),
]

urlpatterns = [
    path(
        "org/<int:org>/",
        include((middle_urlpatterns, "mid"), namespace="mid"),
    ),
    path("plain/<int:pk>/", views.empty_view, name="plain"),
    path("word/<str:word>/", views.empty_view, name="plain-word"),
    # The typed, bounded route is declared before the permissive <path>
    # sibling: a value that belongs to the year converter (even with its digits
    # partly percent-encoded) must never fall through to the catch-all.
    path("blog/<decyear:year>/", views.empty_view, name="blog-year"),
    path("blog/<path:rest>/", views.empty_view, name="blog-rest"),
    # A legacy, non-namespaced regex route: captures keep their raw,
    # percent-encoded text (resolution of re_path() is unchanged).
    re_path(r"^legacy/(?P<rest>.+)/$", views.empty_view, name="legacy"),
    *i18n_patterns(
        path(
            "loc/<slug:book>/",
            include((inner_urlpatterns, "inner"), namespace="inner"),
        ),
    ),
]
