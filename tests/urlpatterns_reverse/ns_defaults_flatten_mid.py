from django.urls import include, path

# Deliberately no app_name: this include is flattened away while populating
# the reverse dictionaries. Its default kwargs must still reach a reverse of
# a name nested behind an app-namespaced include further out.
urlpatterns = [
    path(
        "b/<int:b>/",
        include("urlpatterns_reverse.ns_defaults_inner"),
        {"b": 6},
    ),
]
