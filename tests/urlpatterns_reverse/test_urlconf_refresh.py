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
    get_resolver,
    include,
    path,
    re_path,
    register_converter,
    resolve,
    reverse,
)


def old_view(request, **kwargs):
    pass


def new_view(request, **kwargs):
    pass


def other_view(request, **kwargs):
    pass


def code_view(request, **kwargs):
    pass


class CodeValue:
    """A deliberately non-string value handed back by the converters below."""

    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        return isinstance(other, CodeValue) and other.text == self.text

    def __repr__(self):
        return "CodeValue(%r)" % (self.text,)


class LenientCodeConverter:
    # Admits '%' and '/' but not '*', and bounds the length: percent escapes
    # are matched on the raw path and decoded once, so an encoded slash stays
    # data inside one parameter and a double-encoded value reaches to_python()
    # as the literal text '%2F'. An over-long decoded value is out of range.
    regex = r"[^*\n]{1,12}"

    def to_python(self, value):
        return CodeValue(value)

    def to_url(self, value):
        return value.text


class StrictCodeConverter:
    # Admits neither '%', '/', nor whitespace: a malformed escape, an encoded
    # slash or an encoded space can never be smuggled through it, and it is
    # length bounded so an over-long decoded value is out of range.
    regex = r"[^%/*\s]{1,12}"

    def to_python(self, value):
        return CodeValue(value)

    def to_url(self, value):
        return value.text


register_converter(LenientCodeConverter, "inplace_code")
register_converter(StrictCodeConverter, "strict_code")


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


def _build_in_place_conf():
    """
    A two-level include whose every routing table is a distinct list object
    that is later edited *in place*. Returns ``(conf, inner, middle, root)``
    where ``conf.urlpatterns is root`` and the include points at middle ->
    inner.
    """
    inner = MutableURLConf(
        [path("item/<int:pk>/", old_view, name="item")], app_name="innerapp"
    )
    middle = [
        path("nested/", include((inner, "innerapp"), namespace="innerapp")),
    ]
    root = [
        path("old/", include((middle, "outer"), namespace="outer")),
        path("plain/<int:n>/", other_view, name="plain"),
    ]
    return MutableURLConf(root), inner, middle, root


class InPlaceURLConfMutationTests(SimpleTestCase):
    def test_in_place_replace_hits_new_route_on_next_resolve(self):
        conf, inner, middle, root = _build_in_place_conf()
        resolver = get_resolver(conf)

        match = resolve("/old/nested/item/42/", urlconf=conf)
        # Full identity before the edit: view, namespace chain and typed args.
        self.assertIs(match.func, old_view)
        self.assertEqual(match.view_name, "outer:innerapp:item")
        self.assertEqual(match.namespaces, ["outer", "innerapp"])
        self.assertEqual(match.app_names, ["outer", "innerapp"])
        self.assertEqual(match.route, "old/nested/item/<int:pk>/")
        self.assertEqual(match.kwargs, {"pk": 42})
        self.assertIsInstance(match.kwargs["pk"], int)
        old_fields = _match_fields(match)

        # Replace the contents of the very same lists (two levels deep); no
        # urlconf object, resolver or list is rebuilt, and no cache is cleared.
        inner.urlpatterns[:] = [
            path("thing/<slug:slug>/", new_view, name="thing"),
            path("code/<inplace_code:c>/", code_view, name="code"),
        ]
        root[:] = [
            path("new/", include((middle, "outer"), namespace="outer")),
            path("simple/<int:n>/", other_view, name="simple"),
        ]
        self.assertIs(conf.urlpatterns, root)
        self.assertIs(get_resolver(conf), resolver)

        # The next resolve() immediately serves the new table.
        match = resolve("/new/nested/thing/abc/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.view_name, "outer:innerapp:thing")
        self.assertEqual(match.namespaces, ["outer", "innerapp"])
        self.assertEqual(match.route, "new/nested/thing/<slug:slug>/")
        self.assertEqual(match.kwargs, {"slug": "abc"})
        self.assertIsInstance(match.kwargs["slug"], str)
        # The old routes are gone, and this did not come from the old cache.
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/42/", urlconf=conf)
        with self.assertRaises(Resolver404):
            resolve("/plain/7/", urlconf=conf)

        # Restoring the original contents in place brings the old result back
        # field by field, still without rebuilding the urlconf.
        inner.urlpatterns[:] = [path("item/<int:pk>/", old_view, name="item")]
        root[:] = [
            path("old/", include((middle, "outer"), namespace="outer")),
            path("plain/<int:n>/", other_view, name="plain"),
        ]
        self.assertEqual(
            _match_fields(resolve("/old/nested/item/42/", urlconf=conf)),
            old_fields,
        )

    def test_in_place_append_delete_and_reorder(self):
        conf = MutableURLConf(
            [
                path("a/<int:n>/", old_view, name="a"),
                path("b/<int:n>/", other_view, name="b"),
            ]
        )
        table = conf.urlpatterns
        self.assertEqual(resolve("/a/1/", urlconf=conf).kwargs, {"n": 1})
        self.assertEqual(resolve("/b/1/", urlconf=conf).kwargs, {"n": 1})

        # A pure append is visible immediately; the untouched siblings keep
        # matching, and the appended route did not match beforehand.
        with self.assertRaises(Resolver404):
            resolve("/c/1/", urlconf=conf)
        table.append(path("c/<int:n>/", new_view, name="c"))
        self.assertIs(conf.urlpatterns, table)
        self.assertIs(resolve("/c/2/", urlconf=conf).func, new_view)
        self.assertIs(resolve("/a/3/", urlconf=conf).func, old_view)
        self.assertIs(resolve("/b/3/", urlconf=conf).func, other_view)

        # A pure delete is visible immediately; surviving siblings still match.
        del table[0]
        with self.assertRaises(Resolver404):
            resolve("/a/4/", urlconf=conf)
        self.assertIs(resolve("/b/4/", urlconf=conf).func, other_view)
        self.assertIs(resolve("/c/4/", urlconf=conf).func, new_view)

        # Reordering the same entries in place changes precedence: two
        # identical routes resolve to their first entry, and reversing the
        # list flips which view wins.
        first = path("dup/<int:n>/", old_view, name="dup")
        second = path("dup/<int:n>/", new_view, name="dup")
        conf.urlpatterns[:] = [first, second]
        self.assertIs(resolve("/dup/1/", urlconf=conf).func, old_view)
        conf.urlpatterns.reverse()
        self.assertIs(resolve("/dup/1/", urlconf=conf).func, new_view)

    def test_in_place_invalid_content_is_configuration_error(self):
        conf, inner, middle, root = _build_in_place_conf()
        resolve("/old/nested/item/1/", urlconf=conf)

        good_table = list(root)
        good_inner = list(inner.urlpatterns)
        for bad in ([None], ["not-a-pattern"], [42], [re_path("[", old_view)]):
            with self.subTest(bad=bad):
                root[:] = bad
                # Every path fails as a configuration error -- never a
                # Resolver404, a partial result, or a leaked exception.
                with self.assertRaises(ImproperlyConfigured):
                    resolve("/old/nested/item/1/", urlconf=conf)
                with self.assertRaises(ImproperlyConfigured):
                    resolve("/anything/else/", urlconf=conf)
                root[:] = good_table

        # Invalid content nested two levels down is reported identically.
        inner.urlpatterns[:] = [None]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/1/", urlconf=conf)
        inner.urlpatterns[:] = good_inner

        # Restoring valid contents in place needs no rebuild: the previous,
        # last-known-good snapshot survived every failed edit.
        match = resolve("/old/nested/item/3/", urlconf=conf)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.kwargs, {"pk": 3})

    def test_failed_in_place_edit_keeps_last_good_snapshot(self):
        conf = MutableURLConf([path("ok/<int:n>/", old_view, name="ok")])
        resolver = get_resolver(conf)
        self.assertEqual(resolve("/ok/1/", urlconf=conf).kwargs, {"n": 1})

        conf.urlpatterns[:] = [object()]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/ok/1/", urlconf=conf)
        # While the bad table is exposed nothing serves, but the resolver --
        # and hence its last good table -- is still the very same object.
        self.assertIs(get_resolver(conf), resolver)

        conf.urlpatterns[:] = [path("ok/<int:n>/", new_view, name="ok")]
        match = resolve("/ok/2/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"n": 2})

    def test_reverse_observes_in_place_append_and_delete(self):
        conf = MutableURLConf([path("old/<int:n>/", old_view, name="oldname")])
        self.assertEqual(
            reverse("oldname", kwargs={"n": 1}, urlconf=conf), "/old/1/"
        )
        conf.urlpatterns[:] = [path("new/<slug:s>/", new_view, name="newname")]
        self.assertEqual(
            reverse("newname", kwargs={"s": "ab"}, urlconf=conf), "/new/ab/"
        )
        with self.assertRaises(NoReverseMatch):
            reverse("oldname", kwargs={"n": 1}, urlconf=conf)

    def test_percent_encoding_after_in_place_replace(self):
        conf, inner, middle, root = _build_in_place_conf()
        resolve("/old/nested/item/1/", urlconf=conf)
        inner.urlpatterns[:] = [
            path("code/<inplace_code:c>/", code_view, name="code"),
            path("strict/<strict_code:c>/", code_view, name="strict"),
            path("thing/<slug:slug>/", new_view, name="thing"),
        ]
        root[:] = [path("new/", include((middle, "outer"), namespace="outer"))]

        # Mixed percent-encoded Unicode, spaces and reserved characters are
        # decoded exactly once; an encoded slash stays inside the single
        # parameter and emerges as the converter's declared, non-string type.
        match = resolve("/new/nested/code/a%2Fb/", urlconf=conf)
        self.assertEqual(match.kwargs, {"c": CodeValue("a/b")})
        self.assertIsInstance(match.kwargs["c"], CodeValue)
        self.assertEqual(list(match.kwargs), ["c"])
        match = resolve("/new/nested/code/a%252Fb/", urlconf=conf)
        self.assertEqual(match.kwargs, {"c": CodeValue("a%2Fb")})
        match = resolve(
            "/new/nested/code/caf%C3%A9%20x%3F%26%3D%3A/", urlconf=conf
        )
        self.assertEqual(match.kwargs, {"c": CodeValue("café x?&=:")})

        # Empty, out-of-range, illegal, malformed and missing/extra paths are
        # only ever a Resolver404 -- a converter exception never escapes and
        # no sibling is returned as a partial match.
        failures = [
            "/new/nested/code//",  # empty value
            "/new/nested/code/abcdefghijklm/",  # decoded value out of range
            "/new/nested/strict/%ZZ/",  # malformed percent escape
            "/new/nested/strict/a%2Fb/",  # encoded slash smuggled in
            "/new/nested/strict/a%20b/",  # encoded space smuggled in
            "/new/nested/thing/",  # missing segment
            "/new/nested/thing/ab/extra/",  # extra segment
        ]
        for path_value in failures:
            with self.subTest(path_value=path_value):
                with self.assertRaises(Resolver404):
                    resolve(path_value, urlconf=conf)

        # After the run of failures, a legal path is still decided entirely by
        # the live list: view, namespace, keys and value types.
        match = resolve("/new/nested/code/a%2Fb/", urlconf=conf)
        self.assertIs(match.func, code_view)
        self.assertEqual(match.namespaces, ["outer", "innerapp"])
        self.assertEqual(match.kwargs, {"c": CodeValue("a/b")})
        self.assertIsInstance(match.kwargs["c"], CodeValue)

    def test_plain_routes_keep_trailing_slash_semantics(self):
        conf, inner, middle, root = _build_in_place_conf()
        resolve("/old/nested/item/1/", urlconf=conf)
        root[:] = [path("simple/<int:n>/", other_view, name="simple")]
        # An ordinary, non-namespaced route introduced in place still requires
        # its trailing slash (resolve() itself reports a 404).
        self.assertEqual(resolve("/simple/3/", urlconf=conf).kwargs, {"n": 3})
        with self.assertRaises(Resolver404):
            resolve("/simple/3", urlconf=conf)

    def test_two_urlconfs_edited_in_place_do_not_leak_state(self):
        def build(view, name, seg):
            inner = MutableURLConf(
                [path(f"{seg}/<int:pk>/", view, name=name)], app_name="ia"
            )
            outer = MutableURLConf(
                [
                    path(
                        f"{name}/",
                        include(
                            (
                                [
                                    path(
                                        "n/",
                                        include((inner, "ia"), namespace="ia"),
                                    )
                                ],
                                "o",
                            ),
                            namespace="o",
                        ),
                    )
                ]
            )
            return outer, inner

        conf_a, inner_a = build(old_view, "aa", "a")
        conf_b, inner_b = build(new_view, name="bb", seg="b")
        fields_a = _match_fields(resolve("/aa/n/a/1/", urlconf=conf_a))
        fields_b = _match_fields(resolve("/bb/n/b/2/", urlconf=conf_b))

        # A is edited in place; B is made invalid in place and then fixed.
        inner_a.urlpatterns[:] = [
            path("x/<slug:s>/", code_view, name="xx")
        ]
        inner_b.urlpatterns[:] = [None]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/bb/n/b/2/", urlconf=conf_b)
        inner_b.urlpatterns[:] = [path("b/<int:pk>/", new_view, name="bb")]

        def code_field(value):
            return _match_fields(resolve(f"/aa/n/x/{value}/", urlconf=conf_a))

        def b_field(value):
            return _match_fields(resolve(f"/bb/n/b/{value}/", urlconf=conf_b))

        # Field-for-field baselines for the current tables of A and B.
        a_now = code_field("hi")
        b_now = b_field(2)
        self.assertEqual(a_now[0], code_view)
        self.assertEqual(a_now[3], "xx")
        self.assertEqual(a_now[2], {"s": "hi"})
        self.assertIsInstance(a_now[2]["s"], str)
        self.assertEqual(b_now, fields_b)

        # A then B ...
        a_after_a_first = code_field("hi")
        b_after_a_first = b_field(9)
        # ... and B then A. Swapping the call order leaves each conf's result
        # field-for-field identical; no view, namespace or captured value
        # crosses from one object to the other.
        b_after_b_first = b_field(9)
        a_after_b_first = code_field("hi")
        self.assertEqual(a_after_b_first, a_after_a_first)
        self.assertEqual(b_after_b_first, b_after_a_first)
        self.assertEqual(code_field("zz")[2], {"s": "zz"})
        self.assertEqual(b_field(9)[2], {"pk": 9})
        self.assertIsInstance(b_field(9)[2]["pk"], int)
        with self.assertRaises(Resolver404):
            resolve("/aa/n/a/1/", urlconf=conf_a)

        # Rewriting a list in place with freshly constructed, equivalent
        # entries yields field-for-field identical results, for both confs.
        inner_b.urlpatterns[:] = [path("b/<int:pk>/", new_view, name="bb")]
        self.assertEqual(b_field(2), fields_b)
        inner_a.urlpatterns[:] = [path("x/<slug:s>/", code_view, name="xx")]
        self.assertEqual(code_field("hi"), a_now)
