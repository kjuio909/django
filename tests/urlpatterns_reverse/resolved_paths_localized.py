from django.urls import include, path, register_converter

from .converters import BoundedIntConverter, ExplodingConverter

# Registered before any pattern below resolves (the innermost URLconf is
# imported lazily on first resolution), so <bounded:…>/<exploding:…> are
# known when its routes are compiled.
register_converter(BoundedIntConverter, "bounded")
register_converter(ExplodingConverter, "exploding")

# Two include levels behind a language prefix. The outer include captures
# "org" and supplies a same-named default; the middle include does the same
# for "section".
localized_patterns = [
    path(
        "org/<slug:org>/",
        include("urlpatterns_reverse.resolved_paths_mid"),
        {"org": "default-org"},
    ),
]
