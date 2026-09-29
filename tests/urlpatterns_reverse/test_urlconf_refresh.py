"""
Tests for changing an explicitly supplied URLconf's ``urlpatterns`` at
runtime and resolving through ``django.urls.resolve(path, urlconf=...)``.

A URLconf object handed to resolve() is long lived and version aware: when
its ``urlpatterns`` is replaced (a *refresh*) or mutated in place on the very
list object the urlconf exposes (``urlpatterns[:] = ...`` or an
append/delete) the next resolve() must observe the new table without any
cache being cleared by the caller or the urlconf object being rebuilt, a
refresh onto an invalid table must raise a configuration error without
destroying the previously working table, and different URLconf objects must
never leak state into one another.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import (
    NoReverseMatch,
    Resolver404,
    include,
    path,
    re_path,
    resolve,
    reverse,
)
from django.urls.converters import REGISTERED_CONVERTERS, get_converters
from django.urls.resolvers import _route_to_regex


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


class Label:
    """A non-string parameter value, as a custom converter must be able to
    return."""

    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        return isinstance(other, Label) and other.text == self.text

    def __repr__(self):
        return "Label(%r)" % self.text


class LabelConverter:
    # Accepts Unicode, spaces, reserved characters and a decoded slash; never a
    # literal '%' (so percent escapes are the only way such data can arrive),
    # a query/fragment delimiter or a newline.
    regex = r"[^%?#\n]+"

    def to_python(self, value):
        return Label(value)

    def to_url(self, value):
        return value.text


def _label_patterns():
    # Two levels of include(), a trailing slash everywhere and a custom
    # converter returning a non-string type.
    inner = MutableURLConf(
        [path("item/<label:value>/", new_view, name="item")], app_name="innerapp"
    )
    middle = [
        path("nested/", include((inner, "innerapp"), namespace="innerapp"))
    ]
    return inner, [
        path("new/", include((middle, "outer"), namespace="outer")),
        path("plain/<int:n>/", other_view, name="plain"),
    ]


class InPlaceURLConfRefreshTests(SimpleTestCase):
    """
    The same urlconf object, and the very list exposed as its
    ``urlpatterns``, safely carries additions, deletions and content
    replacements made in place: no object is rebuilt by the caller and no
    global cache is cleared.
    """

    CONVERTER_NAME = "label"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._previous = REGISTERED_CONVERTERS.get(cls.CONVERTER_NAME)
        REGISTERED_CONVERTERS[cls.CONVERTER_NAME] = LabelConverter()
        get_converters.cache_clear()
        _route_to_regex.cache_clear()

    @classmethod
    def tearDownClass(cls):
        if cls._previous is None:
            REGISTERED_CONVERTERS.pop(cls.CONVERTER_NAME, None)
        else:
            REGISTERED_CONVERTERS[cls.CONVERTER_NAME] = cls._previous
        get_converters.cache_clear()
        _route_to_regex.cache_clear()
        super().tearDownClass()

    def test_in_place_content_replacement_is_observed_on_next_call(self):
        conf, _ = _conf("old")
        table = conf.urlpatterns  # retained throughout the test
        match = resolve("/old/nested/item/42/", urlconf=conf)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.namespace, "outer:innerapp")
        self.assertEqual(match.kwargs, {"pk": 42})
        self.assertIsInstance(match.kwargs["pk"], int)
        old_fields = _match_fields(match)

        # Replace the *contents* of the same list object in place: the new
        # routes (two include levels, a trailing slash, a custom converter)
        # are hit by the very next resolve().
        _, new_table = _label_patterns()
        table[:] = new_table
        self.assertIs(conf.urlpatterns, table)
        match = resolve("/new/nested/item/caf%C3%A9%20x/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.namespaces, ["outer", "innerapp"])
        self.assertEqual(match.app_names, ["outer", "innerapp"])
        self.assertEqual(match.kwargs, {"value": Label("café x")})
        self.assertIsInstance(match.kwargs["value"], Label)
        self.assertEqual(
            match.route, "new/nested/item/<label:value>/"
        )
        # Old paths fail; the answer cannot come from a stale snapshot. The
        # old include prefix is gone even though the new table happens to keep
        # a "plain/<int>" sibling.
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/42/", urlconf=conf)
        self.assertEqual(resolve("/plain/7/", urlconf=conf).kwargs, {"n": 7})

        # Restoring equivalent old contents in place restores the result
        # field by field.
        table[:] = _patterns("old")[1]
        self.assertEqual(
            _match_fields(resolve("/old/nested/item/42/", urlconf=conf)),
            old_fields,
        )

    def test_encoded_slash_stays_in_one_parameter_and_is_decoded_once(self):
        conf, _ = _conf("old")
        _, new_table = _label_patterns()
        conf.urlpatterns[:] = new_table
        # Mixed percent-encoded Unicode, a space, reserved characters and an
        # encoded slash all land in a single parameter; the slash is decoded
        # once and never introduces a path level.
        match = resolve(
            "/new/nested/item/caf%C3%A9%20x%2Fa!%26b/", urlconf=conf
        )
        self.assertEqual(match.kwargs, {"value": Label("café x/a!&b")})
        # A double-encoded slash decodes once into the literal text "%2F",
        # which the converter regex rejects.
        with self.assertRaises(Resolver404):
            resolve("/new/nested/item/a%252Fb/", urlconf=conf)

    def test_invalid_values_only_raise_resolver404(self):
        conf, _ = _conf("old")
        _, new_table = _label_patterns()
        conf.urlpatterns[:] = new_table
        for path_value in (
            "/new/nested/item//",            # empty value
            "/plain/1/2/",                   # extra segment on a <int>
            "/new/nested/item/%ZZ/",        # malformed percent escape
            "/new/nested/item/a%2/",         # truncated escape
            "/plain/a%20b/",                 # space rejected by <int>
            "/plain/abc/",                   # illegal value for <int>
        ):
            with self.subTest(path_value=path_value):
                with self.assertRaises(Resolver404):
                    resolve(path_value, urlconf=conf)
        # After every failure, a valid path is governed by the live table.
        match = resolve("/new/nested/item/ok/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(list(match.kwargs), ["value"])
        self.assertIsInstance(match.kwargs["value"], Label)

    def test_single_in_place_append_and_delete_are_visible(self):
        conf, _ = _conf("old")
        table = conf.urlpatterns
        resolve("/plain/7/", urlconf=conf)

        appended = path("added/<slug:s>/", new_view, name="added")
        table.append(appended)
        match = resolve("/added/xy/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"s": "xy"})
        # Unchanged siblings keep their order and match.
        self.assertEqual(
            resolve("/plain/8/", urlconf=conf).kwargs, {"n": 8}
        )
        self.assertIs(table[1].name, "plain")
        self.assertIs(table[2], appended)

        # Deleting the appended route removes it on the next call; the
        # surviving siblings are untouched.
        del table[-1]
        with self.assertRaises(Resolver404):
            resolve("/added/xy/", urlconf=conf)
        match = resolve("/plain/9/", urlconf=conf)
        self.assertIs(match.func, other_view)
        self.assertEqual(match.kwargs, {"n": 9})
        self.assertEqual(len(table), 2)

    def test_middle_entry_replacement_is_detected(self):
        conf, _ = _conf("old")
        table = conf.urlpatterns
        resolve("/plain/1/", urlconf=conf)
        # Same length and unchanged endpoints: only a contents comparison can
        # see that the first entry was replaced.
        table[0] = path("swapped/<int:w>/", new_view, name="swapped")
        self.assertEqual(resolve("/swapped/3/", urlconf=conf).kwargs, {"w": 3})
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/1/", urlconf=conf)
        # The untouched sibling still matches.
        self.assertEqual(resolve("/plain/2/", urlconf=conf).kwargs, {"n": 2})

    def test_in_place_invalid_candidate_raises_and_keeps_last_snapshot(self):
        conf, inner = _conf("old")
        table = conf.urlpatterns
        resolve("/old/nested/item/1/", urlconf=conf)

        # Invalid candidates written in place on the retained list are
        # configuration errors -- never a 404, an internal exception or a
        # partial match -- for every path.
        for bad_entry in (None, object(), 42):
            with self.subTest(bad_entry=bad_entry):
                table[1] = bad_entry
                with self.assertRaises(ImproperlyConfigured):
                    resolve("/old/nested/item/1/", urlconf=conf)
                with self.assertRaises(ImproperlyConfigured):
                    resolve("/anything/at/all/", urlconf=conf)
                # The failed edit cannot destroy the last valid snapshot, nor
                # the retained list object.
                self.assertIs(conf.urlpatterns, table)
        table[1] = path("plain/<int:n>/", other_view, name="plain")

        # A whole-list invalid replacement in place behaves the same.
        table[:] = ["not-a-pattern"]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/1/", urlconf=conf)

        # An invalid pattern (uncompilable regex) edited in place likewise.
        table[:] = [re_path(r"[", old_view, name="bad")]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/1/", urlconf=conf)

        # Restoring valid contents on the same list takes effect immediately,
        # without rebuilding the URLconf. Rebuild the table around the very
        # same nested urlconf object (inner) so its in-place edits are
        # observable.
        middle = [
            path(
                "nested/",
                include((inner, "innerapp"), namespace="innerapp"),
            )
        ]
        table[:] = [
            path("old/", include((middle, "outer"), namespace="outer")),
            path("plain/<int:n>/", other_view, name="plain"),
        ]
        match = resolve("/old/nested/item/5/", urlconf=conf)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.kwargs, {"pk": 5})
        self.assertEqual(match.namespace, "outer:innerapp")

        # A bad *nested* in-place edit is a configuration error too, and
        # restoring the nested table serves again right away.
        inner.urlpatterns[:] = [None]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/nested/item/5/", urlconf=conf)
        inner.urlpatterns[:] = _inner("old")
        match = resolve("/old/nested/item/6/", urlconf=conf)
        self.assertIs(match.func, old_view)
        self.assertEqual(match.kwargs, {"pk": 6})

    def test_two_urlconfs_alternating_in_place_edits_do_not_leak(self):
        conf_a, inner_a = _conf("old")
        conf_b, inner_b = _conf("new")

        def snapshot(conf, path_value):
            return _match_fields(resolve(path_value, urlconf=conf))

        a_path = "/old/nested/item/1/"
        b_path = "/new/nested/thing/b/"
        a_before = snapshot(conf_a, a_path)
        b_before = snapshot(conf_b, b_path)

        # In-place rewrites, including a temporary invalid edit on A, must
        # never cross into B.
        inner_a.urlpatterns[:] = _inner("new")
        inner_a.urlpatterns[:] = [object()]  # A is temporarily invalid
        with self.assertRaises(ImproperlyConfigured):
            resolve(a_path, urlconf=conf_a)
        self.assertEqual(snapshot(conf_b, b_path), b_before)
        inner_a.urlpatterns[:] = _inner("old")

        # Repeatedly writing equivalent contents keeps both objects stable.
        for _ in range(2):
            conf_a.urlpatterns[:] = _patterns("old")[1]
            conf_b.urlpatterns[:] = _patterns("new")[1]
        self.assertEqual(snapshot(conf_a, a_path), a_before)
        self.assertEqual(snapshot(conf_b, b_path), b_before)

        # Swapping the call order must give field-by-field identical results.
        self.assertEqual(snapshot(conf_b, b_path), b_before)
        self.assertEqual(snapshot(conf_a, a_path), a_before)

    def test_success_and_failure_alternation_reflects_the_live_list(self):
        conf, _ = _conf("old")
        resolve("/old/nested/item/1/", urlconf=conf)
        conf.urlpatterns[:] = _patterns("new")[1]
        with self.assertRaises(Resolver404):
            resolve("/old/nested/item/1/", urlconf=conf)
        match = resolve("/new/nested/thing/ab/", urlconf=conf)
        self.assertIs(match.func, new_view)
        self.assertEqual(match.kwargs, {"slug": "ab"})
        # An invalid in-place edit fails as a configuration error; a valid
        # edit on the same list then wins immediately.
        conf.urlpatterns[:] = [None]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/new/nested/thing/ab/", urlconf=conf)
        conf.urlpatterns[:] = _patterns("fixed")[1]
        match = resolve("/fixed/nested/fixed/a/b/", urlconf=conf)
        self.assertIs(match.func, other_view)
        self.assertEqual(match.kwargs, {"p": "a/b"})

    def test_reverse_uses_a_table_replaced_in_place(self):
        conf = MutableURLConf([path("old/<int:n>/", old_view, name="oldname")])
        self.assertEqual(
            reverse("oldname", kwargs={"n": 1}, urlconf=conf), "/old/1/"
        )
        # The retained list's contents are replaced in place; reverse
        # caches derived from the old table are rebuilt.
        conf.urlpatterns[:] = [
            path("new/<slug:s>/", new_view, name="newname")
        ]
        self.assertEqual(
            reverse("newname", kwargs={"s": "ab"}, urlconf=conf), "/new/ab/"
        )
        with self.assertRaises(NoReverseMatch):
            reverse("oldname", kwargs={"n": 1}, urlconf=conf)
