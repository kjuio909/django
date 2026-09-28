"""
Tests for replacing an explicitly supplied URLconf's ``urlpatterns`` at
runtime and resolving through ``django.urls.resolve(path, urlconf=...)``.

A URLconf object handed to resolve() is long lived and version aware: when
its ``urlpatterns`` is replaced (a *refresh*) the next resolve() must observe
the new table without any cache being cleared by the caller, a refresh onto
an invalid table must raise a configuration error without destroying the
previously working table, and different URLconf objects must never leak
state into one another.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import (
    NoReverseMatch,
    Resolver404,
    include,
    path,
    resolve,
    reverse,
)


def old_view(request, **kwargs):
    pass


def new_view(request, **kwargs):
    pass


def other_view(request, **kwargs):
    pass


class MutableURLConf:
    """A hashable URLconf object whose ``urlpatterns`` can be replaced.

    Hashability matters: like a module or an ordinary instance it is used as
    the resolver cache key, so resolve() keeps returning the same
    long-lived, version-aware resolver across calls.
    """

    def __init__(self, urlpatterns, app_name=None):
        self.urlpatterns = urlpatterns
        if app_name is not None:
            self.app_name = app_name


def _inner(table):
    if table == "old":
        return [path("item/<int:pk>/", old_view, name="item")]
    if table == "new":
        return [path("thing/<slug:slug>/", new_view, name="thing")]
    if table == "fixed":
        return [path("fixed/<path:p>/", other_view, name="fixed")]
    raise ValueError(table)


def _patterns(table, inner_conf=None):
    inner_conf = inner_conf or MutableURLConf(_inner(table), app_name="innerapp")
    middle = [
        path(
            "nested/",
            include((inner_conf, "innerapp"), namespace="innerapp"),
        )
    ]
    if table == "old":
        return inner_conf, [
            path(
                "old/",
                include((middle, "outer"), namespace="outer"),
            ),
            path("plain/<int:n>/", other_view, name="plain"),
        ]
    if table == "new":
        return inner_conf, [
            path(
                "new/",
                include((middle, "outer"), namespace="outer"),
            ),
            path("plain2/<slug:s>/", other_view, name="plain2"),
        ]
    if table == "fixed":
        return inner_conf, [
            path(
                "fixed/",
                include((middle, "outer"), namespace="outer"),
            ),
        ]
    raise ValueError(table)


def _conf(table):
    inner_conf, patterns = _patterns(table)
    return MutableURLConf(patterns), inner_conf


def _match_fields(match):
    return (
        match.func,
        match.args,
        dict(match.kwargs),
        match.url_name,
        list(match.app_names),
        list(match.namespaces),
        match.route,
        dict(match.captured_kwargs),
        dict(match.extra_kwargs),
    )


class URLConfRefreshTests(SimpleTestCase):
    def test_replace_valid_table_takes_effect_immediately(self):
        conf, _ = _conf("old")
        match = resolve("/old/nested/item/42/", urlconf=conf)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.namespace, "outer:innerapp")
        self.assertEqual(match.kwargs, {"pk": 42})
        self.assertIsInstance(match.kwargs["pk"], int)
        old_fields = _match_fields(match)

        # A valid replacement is observed on the very next resolve(); the old
        # routes are gone without the caller clearing any cache.
        conf.urlpatterns = _patterns("new")[1]
        match = resolve("/new/nested/thing/abc/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.namespace, "outer:innerapp")
        self.assertEqual(match.kwargs, {"slug": "abc"})
        self.assertIsInstance(match.kwargs["slug"], str)
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/42/", urlconf=conf)
        with self.assertRaises(Resolver404):
            resolve("/plain/7/", urlconf=conf)

        # An invalid configuration raises a configuration error -- never a
        # ResolverMatch, a Resolver404 or a partial result -- for any path.
        conf.urlpatterns = None
        with self.assertRaises(ImproperlyConfigured):
            resolve("/new/nested/thing/abc/", urlconf=conf)
        with self.assertRaises(ImproperlyConfigured):
            resolve("/anything/", urlconf=conf)

        # The next valid replacement takes effect immediately, leaving no
        # failed state behind.
        conf.urlpatterns = _patterns("fixed")[1]
        match = resolve("/fixed/nested/fixed/a/b/", urlconf=conf)
        self.assertIs(match.func, other_view)
        self.assertEqual(match.kwargs, {"p": "a/b"})
        with self.assertRaises(Resolver404):
            resolve("/new/nested/thing/abc/", urlconf=conf)

        # Restoring the old table brings its previous result back field by
        # field: the failed refresh could not destroy the earlier routes.
        conf.urlpatterns = _patterns("old")[1]
        match = resolve("/old/nested/item/42/", urlconf=conf)
        self.assertEqual(_match_fields(match), old_fields)

    def test_missing_path_before_refresh_does_not_poison_new_table(self):
        conf, _ = _conf("old")
        with self.assertRaises(Resolver404):
            resolve("/does/not/exist/", urlconf=conf)
        conf.urlpatterns = _patterns("new")[1]
        match = resolve("/new/nested/thing/xyz/", urlconf=conf)
        self.assertIs(match.func, new_view)
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/1/", urlconf=conf)

    def test_invalid_configuration_before_any_success_then_fixed(self):
        conf = MutableURLConf(None)
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/1/", urlconf=conf)
        conf.urlpatterns = _patterns("new")[1]
        match = resolve("/new/nested/thing/xyz/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"slug": "xyz"})

    def test_old_path_accessed_first_after_refresh_is_not_stale(self):
        conf, _ = _conf("old")
        resolve("/plain/7/", urlconf=conf)
        conf.urlpatterns = _patterns("new")[1]
        with self.assertRaises(Resolver404):
            resolve("/plain/7/", urlconf=conf)
        match = resolve("/new/nested/thing/zz/", urlconf=conf)
        self.assertEqual(match.kwargs, {"slug": "zz"})

    def test_two_urlconfs_never_leak_state(self):
        conf_a, _ = _conf("old")
        conf_b, _ = _conf("new")
        self.assertEqual(
            resolve("/old/nested/item/1/", urlconf=conf_a).kwargs, {"pk": 1}
        )
        self.assertEqual(
            resolve("/new/nested/thing/b/", urlconf=conf_b).kwargs,
            {"slug": "b"},
        )
        # Alternate between the two objects; each keeps its own table.
        match = resolve("/old/nested/item/2/", urlconf=conf_a)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.namespaces, ["outer", "innerapp"])
        match = resolve("/new/nested/thing/c/", urlconf=conf_b)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"slug": "c"})

        # Refreshing only A cannot change what B resolves to.
        conf_a.urlpatterns = _patterns("fixed")[1]
        self.assertEqual(
            resolve("/fixed/nested/fixed/x/", urlconf=conf_a).kwargs,
            {"p": "x"},
        )
        match = resolve("/new/nested/thing/d/", urlconf=conf_b)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"slug": "d"})

    def test_unrefreshed_urlconf_keeps_original_behavior(self):
        conf, _ = _conf("old")
        untouched, _ = _conf("old")
        resolve("/old/nested/item/1/", urlconf=conf)
        conf.urlpatterns = _patterns("new")[1]
        # A separate object that was never refreshed is unaffected.
        match = resolve("/old/nested/item/8/", urlconf=untouched)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.kwargs, {"pk": 8})

    def test_equivalent_replacement_is_order_independent(self):
        conf, _ = _conf("old")
        before = _match_fields(resolve("/old/nested/item/5/", urlconf=conf))
        # Replace the table with a freshly constructed, equivalent one.
        conf.urlpatterns = _patterns("old")[1]
        after = _match_fields(resolve("/old/nested/item/5/", urlconf=conf))
        self.assertEqual(after, before)

    def test_nested_include_table_refresh(self):
        conf, inner = _conf("old")
        match = resolve("/old/nested/item/42/", urlconf=conf)
        self.assertEqual(match.kwargs, {"pk": 42})

        # Swap the nested table while every outer table object stays the same.
        inner.urlpatterns = _inner("new")
        match = resolve("/old/nested/thing/ab/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.namespace, "outer:innerapp")
        self.assertEqual(match.kwargs, {"slug": "ab"})
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/42/", urlconf=conf)

        # An invalid nested table is a configuration error; a valid one then
        # resolves immediately.
        inner.urlpatterns = None
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/thing/ab/", urlconf=conf)
        inner.urlpatterns = _inner("fixed")
        match = resolve("/old/nested/fixed/a/b/", urlconf=conf)
        self.assertEqual(match.kwargs, {"p": "a/b"})

    def test_invalid_entry_in_replacement_preserves_previous_table(self):
        conf, inner = _conf("old")
        resolve("/old/nested/item/1/", urlconf=conf)
        old_table = conf.urlpatterns
        conf.urlpatterns = ["not-a-pattern"]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/1/", urlconf=conf)
        # While the bad table is exposed every resolution fails as a config
        # error; restoring the previous table serves again unchanged.
        conf.urlpatterns = old_table
        match = resolve("/old/nested/item/3/", urlconf=conf)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.kwargs, {"pk": 3})

        # The same protection applies to a bad nested table.
        inner.urlpatterns = [42]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/3/", urlconf=conf)
        inner.urlpatterns = _inner("old")
        match = resolve("/old/nested/item/4/", urlconf=conf)
        self.assertEqual(match.kwargs, {"pk": 4})

    def test_generator_table_is_snapshotted_on_refresh(self):
        def nested_generator():
            yield path("gen/<int:pk>/", new_view, name="genitem")

        conf = MutableURLConf([path("first/<int:n>/", old_view, name="first")])
        resolve("/first/3/", urlconf=conf)
        conf.urlpatterns = [
            path("sec/", include(MutableURLConf(nested_generator()))),
        ]
        match = resolve("/sec/gen/9/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"pk": 9})
        # The snapshot keeps serving; the generator is not walked again.
        self.assertEqual(
            resolve("/sec/gen/10/", urlconf=conf).kwargs, {"pk": 10}
        )

    def test_shared_nested_generator_survives_parent_rebuild(self):
        inner = MutableURLConf(_inner("old"), app_name="innerapp")

        def outer_patterns():
            return [
                path(
                    "old/",
                    include(
                        (
                            [
                                path(
                                    "nested/",
                                    include(
                                        (inner, "innerapp"),
                                        namespace="innerapp",
                                    ),
                                )
                            ],
                            "outer",
                        ),
                        namespace="outer",
                    ),
                )
            ]

        conf = MutableURLConf(outer_patterns())
        resolve("/old/nested/item/1/", urlconf=conf)

        def nested_generator():
            yield path("thing/<slug:s>/", new_view, name="thing")

        inner.urlpatterns = nested_generator()
        self.assertEqual(
            resolve("/old/nested/thing/xy/", urlconf=conf).kwargs,
            {"s": "xy"},
        )

        # A failed parent refresh must not touch the nested snapshot.
        conf.urlpatterns = [object()]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/thing/xy/", urlconf=conf)
        # Rebuilding the parent constructs fresh child resolvers; they share
        # the snapshot of the already materialized nested generator instead of
        # iterating it a second time into an empty table.
        conf.urlpatterns = outer_patterns()
        match = resolve("/old/nested/thing/qq/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"s": "qq"})

    def test_percent_encoding_around_refresh(self):
        conf, _ = _conf("old")
        resolve("/plain/7/", urlconf=conf)
        conf.urlpatterns = _patterns("fixed")[1]
        # An encoded slash is a single parameter and decoding happens once.
        self.assertEqual(
            resolve("/fixed/nested/fixed/a%2Fb/", urlconf=conf).kwargs,
            {"p": "a/b"},
        )
        self.assertEqual(
            resolve("/fixed/nested/fixed/a%252Fb/", urlconf=conf).kwargs,
            {"p": "a%2Fb"},
        )
        # A converter rejecting a decoded value, malformed encoding, and a
        # missing or extra path segment all surface as Resolver404 -- never an
        # internal exception.
        conf.urlpatterns = _patterns("new")[1]
        for path_value in [
            "/new/nested/thing/a%20b/",
            "/new/nested/thing/%ZZ/",
            "/new/nested/thing/",
            "/new/nested/thing/ab/extra/",
        ]:
            with self.subTest(path_value=path_value):
                with self.assertRaises(Resolver404):
                    resolve(path_value, urlconf=conf)

    def test_reverse_uses_refreshed_table(self):
        conf = MutableURLConf([path("old/<int:n>/", old_view, name="oldname")])
        self.assertEqual(
            reverse("oldname", kwargs={"n": 1}, urlconf=conf), "/old/1/"
        )
        conf.urlpatterns = [path("new/<slug:s>/", new_view, name="newname")]
        self.assertEqual(
            reverse("newname", kwargs={"s": "ab"}, urlconf=conf), "/new/ab/"
        )
        with self.assertRaises(NoReverseMatch):
            reverse("oldname", kwargs={"n": 1}, urlconf=conf)

        # A failed refresh must not poison reversing once the table is fixed.
        conf.urlpatterns = None
        with self.assertRaises(ImproperlyConfigured):
            reverse("newname", kwargs={"s": "ab"}, urlconf=conf)
        conf.urlpatterns = [path("new/<slug:s>/", new_view, name="newname")]
        self.assertEqual(
            reverse("newname", kwargs={"s": "cd"}, urlconf=conf), "/new/cd/"
        )
