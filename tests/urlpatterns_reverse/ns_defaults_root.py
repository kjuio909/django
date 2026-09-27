from django.urls import include, path

# Root conf for reversing names nested behind includes whose own routes
# capture parameters that the include() fills from default kwargs.
urlpatterns = [
    # App-namespaced include directly under the root.
    path(
        "def/<int:outer>/",
        include("urlpatterns_reverse.ns_defaults_mid"),
        {"outer": 5},
    ),
    # A non-app include in the middle: its defaults must survive being
    # flattened away during reverse-dictionary population. The unhashable
    # list value must also be carried without tripping resolver caching.
    path(
        "flat/<int:a>/",
        include("urlpatterns_reverse.ns_defaults_flatten_mid"),
        {"a": 4, "extra": ["x", "y"]},
    ),
]
