from django.urls import path, re_path

from .views import empty_view

app_name = "resolved-paths-inner"

# The innermost URLconf exercises every built-in converter kind, a custom
# converter with a domain rejection rule, a converter whose internal parser
# rejects one value with ValueError, an endpoint default, and a legacy
# re_path() route that must keep matching the raw path without
# percent-decoding.
urlpatterns = [
    path("num/<int:pk>/", empty_view, name="num"),
    path("tag/<slug:tag>/", empty_view, name="tag"),
    path("file/<path:rest>/", empty_view, name="file"),
    path("word/<str:word>/", empty_view, name="word"),
    # Two-segment sibling of "word": a naive whole-path percent-decode would
    # turn /word/a%2Fb/ into /word/a/b/ and wrongly match this branch.
    path("word/<str:word>/<int:ordinal>/", empty_view, name="word-two"),
    path("small/<bounded:value>/", empty_view, name="small"),
    # Reachable only with its own suffix; an out-of-range value must not be
    # coerced into this side branch.
    path("small/<int:value>/overflow/", empty_view, name="small-overflow"),
    path("guarded/<exploding:value>/", empty_view, name="guarded"),
    path("themed/<int:pk>/", empty_view, {"theme": "light"}, name="themed"),
    # Legacy re_path() routes keep matching the raw path: captures are not
    # percent-decoded and malformed escapes are not an error at this layer.
    re_path(r"^legacy/(?P<token>[^/]+)/$", empty_view, name="legacy"),
]
