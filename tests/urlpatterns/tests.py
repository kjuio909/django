import os
import string
import uuid

from django.conf.urls.i18n import i18n_patterns
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.test.utils import override_settings
from django.urls import (
    NoReverseMatch,
    Resolver404,
    include,
    path,
    re_path,
    register_converter,
    resolve,
    reverse,
)
from django.urls.converters import REGISTERED_CONVERTERS, IntConverter, get_converters
from django.urls.resolvers import _route_to_regex, get_resolver
from django.utils.translation import override
from django.views import View

from .converters import (
    Base64Converter,
    BoundedTextConverter,
    DynamicConverter,
    NoneReturningConverter,
    RejectingConverter,
)
from .views import empty_view

included_kwargs = {"base": b"hello", "value": b"world"}
converter_test_data = (
    # ('url', ('url_name', 'app_name', {kwargs})),
    # aGVsbG8= is 'hello' encoded in base64.
    ("/base64/aGVsbG8=/", ("base64", "", {"value": b"hello"})),
    (
        "/base64/aGVsbG8=/subpatterns/d29ybGQ=/",
        ("subpattern-base64", "", included_kwargs),
    ),
    (
        "/base64/aGVsbG8=/namespaced/d29ybGQ=/",
        ("subpattern-base64", "namespaced-base64", included_kwargs),
    ),
)


@override_settings(ROOT_URLCONF="urlpatterns.path_urls")
class SimplifiedURLTests(SimpleTestCase):
    def test_path_lookup_without_parameters(self):
        match = resolve("/articles/2003/")
        self.assertEqual(match.url_name, "articles-2003")
        self.assertEqual(match.args, ())
        self.assertEqual(match.kwargs, {})
        self.assertEqual(match.route, "articles/2003/")
        self.assertEqual(match.captured_kwargs, {})
        self.assertEqual(match.extra_kwargs, {})

    def test_path_lookup_with_typed_parameters(self):
        match = resolve("/articles/2015/")
        self.assertEqual(match.url_name, "articles-year")
        self.assertEqual(match.args, ())
        self.assertEqual(match.kwargs, {"year": 2015})
        self.assertEqual(match.route, "articles/<int:year>/")
        self.assertEqual(match.captured_kwargs, {"year": 2015})
        self.assertEqual(match.extra_kwargs, {})

    def test_path_lookup_with_multiple_parameters(self):
        match = resolve("/articles/2015/04/12/")
        self.assertEqual(match.url_name, "articles-year-month-day")
        self.assertEqual(match.args, ())
        self.assertEqual(match.kwargs, {"year": 2015, "month": 4, "day": 12})
        self.assertEqual(match.route, "articles/<int:year>/<int:month>/<int:day>/")
        self.assertEqual(match.captured_kwargs, {"year": 2015, "month": 4, "day": 12})
        self.assertEqual(match.extra_kwargs, {})

    def test_path_lookup_with_multiple_parameters_and_extra_kwarg(self):
        match = resolve("/books/2015/04/12/")
        self.assertEqual(match.url_name, "books-year-month-day")
        self.assertEqual(match.args, ())
        self.assertEqual(
            match.kwargs, {"year": 2015, "month": 4, "day": 12, "extra": True}
        )
        self.assertEqual(match.route, "books/<int:year>/<int:month>/<int:day>/")
        self.assertEqual(match.captured_kwargs, {"year": 2015, "month": 4, "day": 12})
        self.assertEqual(match.extra_kwargs, {"extra": True})

    def test_path_lookup_with_extra_kwarg(self):
        match = resolve("/books/2007/")
        self.assertEqual(match.url_name, "books-2007")
        self.assertEqual(match.args, ())
        self.assertEqual(match.kwargs, {"extra": True})
        self.assertEqual(match.route, "books/2007/")
        self.assertEqual(match.captured_kwargs, {})
        self.assertEqual(match.extra_kwargs, {"extra": True})

    def test_two_variable_at_start_of_path_pattern(self):
        match = resolve("/en/foo/")
        self.assertEqual(match.url_name, "lang-and-path")
        self.assertEqual(match.kwargs, {"lang": "en", "url": "foo"})
        self.assertEqual(match.route, "<lang>/<path:url>/")
        self.assertEqual(match.captured_kwargs, {"lang": "en", "url": "foo"})
        self.assertEqual(match.extra_kwargs, {})

    def test_re_path(self):
        match = resolve("/regex/1/")
        self.assertEqual(match.url_name, "regex")
        self.assertEqual(match.kwargs, {"pk": "1"})
        self.assertEqual(match.route, "^regex/(?P<pk>[0-9]+)/$")
        self.assertEqual(match.captured_kwargs, {"pk": "1"})
        self.assertEqual(match.extra_kwargs, {})

    def test_re_path_with_optional_parameter(self):
        for url, kwargs in (
            ("/regex_optional/1/2/", {"arg1": "1", "arg2": "2"}),
            ("/regex_optional/1/", {"arg1": "1"}),
        ):
            with self.subTest(url=url):
                match = resolve(url)
                self.assertEqual(match.url_name, "regex_optional")
                self.assertEqual(match.kwargs, kwargs)
                self.assertEqual(
                    match.route,
                    r"^regex_optional/(?P<arg1>\d+)/(?:(?P<arg2>\d+)/)?",
                )
                self.assertEqual(match.captured_kwargs, kwargs)
                self.assertEqual(match.extra_kwargs, {})

    def test_re_path_with_missing_optional_parameter(self):
        match = resolve("/regex_only_optional/")
        self.assertEqual(match.url_name, "regex_only_optional")
        self.assertEqual(match.kwargs, {})
        self.assertEqual(match.args, ())
        self.assertEqual(
            match.route,
            r"^regex_only_optional/(?:(?P<arg1>\d+)/)?",
        )
        self.assertEqual(match.captured_kwargs, {})
        self.assertEqual(match.extra_kwargs, {})

    def test_path_lookup_with_inclusion(self):
        match = resolve("/included_urls/extra/something/")
        self.assertEqual(match.url_name, "inner-extra")
        self.assertEqual(match.route, "included_urls/extra/<extra>/")

    def test_path_lookup_with_empty_string_inclusion(self):
        match = resolve("/more/99/")
        self.assertEqual(match.url_name, "inner-more")
        self.assertEqual(match.route, r"^more/(?P<extra>\w+)/$")
        self.assertEqual(match.kwargs, {"extra": "99", "sub-extra": True})
        self.assertEqual(match.captured_kwargs, {"extra": "99"})
        self.assertEqual(match.extra_kwargs, {"sub-extra": True})

    def test_path_lookup_with_double_inclusion(self):
        match = resolve("/included_urls/more/some_value/")
        self.assertEqual(match.url_name, "inner-more")
        self.assertEqual(match.route, r"included_urls/more/(?P<extra>\w+)/$")

    def test_path_reverse_without_parameter(self):
        url = reverse("articles-2003")
        self.assertEqual(url, "/articles/2003/")

    def test_path_reverse_with_parameter(self):
        url = reverse(
            "articles-year-month-day", kwargs={"year": 2015, "month": 4, "day": 12}
        )
        self.assertEqual(url, "/articles/2015/4/12/")

    @override_settings(ROOT_URLCONF="urlpatterns.path_base64_urls")
    def test_converter_resolve(self):
        for url, (url_name, app_name, kwargs) in converter_test_data:
            with self.subTest(url=url):
                match = resolve(url)
                self.assertEqual(match.url_name, url_name)
                self.assertEqual(match.app_name, app_name)
                self.assertEqual(match.kwargs, kwargs)

    @override_settings(ROOT_URLCONF="urlpatterns.path_base64_urls")
    def test_converter_reverse(self):
        for expected, (url_name, app_name, kwargs) in converter_test_data:
            if app_name:
                url_name = "%s:%s" % (app_name, url_name)
            with self.subTest(url=url_name):
                url = reverse(url_name, kwargs=kwargs)
                self.assertEqual(url, expected)

    @override_settings(ROOT_URLCONF="urlpatterns.path_base64_urls")
    def test_converter_reverse_with_second_layer_instance_namespace(self):
        kwargs = included_kwargs.copy()
        kwargs["last_value"] = b"world"
        url = reverse("instance-ns-base64:subsubpattern-base64", kwargs=kwargs)
        self.assertEqual(url, "/base64/aGVsbG8=/subpatterns/d29ybGQ=/d29ybGQ=/")

    def test_path_inclusion_is_matchable(self):
        match = resolve("/included_urls/extra/something/")
        self.assertEqual(match.url_name, "inner-extra")
        self.assertEqual(match.kwargs, {"extra": "something"})

    def test_path_inclusion_is_reversible(self):
        url = reverse("inner-extra", kwargs={"extra": "something"})
        self.assertEqual(url, "/included_urls/extra/something/")

    def test_invalid_kwargs(self):
        msg = "kwargs argument must be a dict, but got str."
        with self.assertRaisesMessage(TypeError, msg):
            path("hello/", empty_view, "name")
        with self.assertRaisesMessage(TypeError, msg):
            re_path("^hello/$", empty_view, "name")

    def test_invalid_converter(self):
        msg = "URL route 'foo/<nonexistent:var>/' uses invalid converter 'nonexistent'."
        with self.assertRaisesMessage(ImproperlyConfigured, msg):
            path("foo/<nonexistent:var>/", empty_view)

    def test_warning_override_default_converter(self):
        msg = "Converter 'int' is already registered."
        with self.assertRaisesMessage(ValueError, msg):
            register_converter(IntConverter, "int")

    def test_warning_override_converter(self):
        msg = "Converter 'base64' is already registered."
        try:
            with self.assertRaisesMessage(ValueError, msg):
                register_converter(Base64Converter, "base64")
                register_converter(Base64Converter, "base64")
        finally:
            REGISTERED_CONVERTERS.pop("base64", None)

    def test_invalid_view(self):
        msg = "view must be a callable or a list/tuple in the case of include()."
        with self.assertRaisesMessage(TypeError, msg):
            path("articles/", "invalid_view")

    def test_invalid_view_instance(self):
        class EmptyCBV(View):
            pass

        msg = "view must be a callable, pass EmptyCBV.as_view(), not EmptyCBV()."
        with self.assertRaisesMessage(TypeError, msg):
            path("foo", EmptyCBV())

    def test_whitespace_in_route(self):
        msg = "URL route %r cannot contain whitespace in angle brackets <…>"
        for whitespace in string.whitespace:
            with self.subTest(repr(whitespace)):
                route = "space/<int:num>/extra/<str:%stest>" % whitespace
                with self.assertRaisesMessage(ImproperlyConfigured, msg % route):
                    path(route, empty_view)
        # Whitespaces are valid in paths.
        p = path("space%s/<int:num>/" % string.whitespace, empty_view)
        match = p.resolve("space%s/1/" % string.whitespace)
        self.assertEqual(match.kwargs, {"num": 1})

    def test_path_trailing_newlines(self):
        tests = [
            "/articles/2003/\n",
            "/articles/2010/\n",
            "/en/foo/\n",
            "/included_urls/extra/\n",
            "/regex/1/\n",
            "/users/1/\n",
        ]
        for url in tests:
            with self.subTest(url=url), self.assertRaises(Resolver404):
                resolve(url)


@override_settings(ROOT_URLCONF="urlpatterns.converter_urls")
class ConverterTests(SimpleTestCase):
    def test_matching_urls(self):
        def no_converter(x):
            return x

        test_data = (
            ("int", {"0", "1", "01", 1234567890}, int),
            ("str", {"abcxyz"}, no_converter),
            ("path", {"allows.ANY*characters"}, no_converter),
            ("slug", {"abcxyz-ABCXYZ_01234567890"}, no_converter),
            ("uuid", {"39da9369-838e-4750-91a5-f7805cd82839"}, uuid.UUID),
        )
        for url_name, url_suffixes, converter in test_data:
            for url_suffix in url_suffixes:
                url = "/%s/%s/" % (url_name, url_suffix)
                with self.subTest(url=url):
                    match = resolve(url)
                    self.assertEqual(match.url_name, url_name)
                    self.assertEqual(match.kwargs, {url_name: converter(url_suffix)})
                    # reverse() works with string parameters.
                    string_kwargs = {url_name: url_suffix}
                    self.assertEqual(reverse(url_name, kwargs=string_kwargs), url)
                    # reverse() also works with native types (int, UUID, etc.).
                    if converter is not no_converter:
                        # The converted value might be different for int (a
                        # leading zero is lost in the conversion).
                        converted_value = match.kwargs[url_name]
                        converted_url = "/%s/%s/" % (url_name, converted_value)
                        self.assertEqual(
                            reverse(url_name, kwargs={url_name: converted_value}),
                            converted_url,
                        )

    def test_nonmatching_urls(self):
        test_data = (
            ("int", {"-1", "letters"}),
            ("str", {"", "/"}),
            ("path", {""}),
            ("slug", {"", "stars*notallowed"}),
            (
                "uuid",
                {
                    "",
                    "9da9369-838e-4750-91a5-f7805cd82839",
                    "39da9369-838-4750-91a5-f7805cd82839",
                    "39da9369-838e-475-91a5-f7805cd82839",
                    "39da9369-838e-4750-91a-f7805cd82839",
                    "39da9369-838e-4750-91a5-f7805cd8283",
                },
            ),
        )
        for url_name, url_suffixes in test_data:
            for url_suffix in url_suffixes:
                url = "/%s/%s/" % (url_name, url_suffix)
                with self.subTest(url=url), self.assertRaises(Resolver404):
                    resolve(url)


@override_settings(ROOT_URLCONF="urlpatterns.path_same_name_urls")
class SameNameTests(SimpleTestCase):
    def test_matching_urls_same_name(self):
        @DynamicConverter.register_to_url
        def requires_tiny_int(value):
            if value > 5:
                raise ValueError
            return value

        tests = [
            (
                "number_of_args",
                [
                    ([], {}, "0/"),
                    ([1], {}, "1/1/"),
                ],
            ),
            (
                "kwargs_names",
                [
                    ([], {"a": 1}, "a/1/"),
                    ([], {"b": 1}, "b/1/"),
                ],
            ),
            (
                "converter",
                [
                    (["a/b"], {}, "path/a/b/"),
                    (["a b"], {}, "str/a%20b/"),
                    (["a-b"], {}, "slug/a-b/"),
                    (["2"], {}, "int/2/"),
                    (
                        ["39da9369-838e-4750-91a5-f7805cd82839"],
                        {},
                        "uuid/39da9369-838e-4750-91a5-f7805cd82839/",
                    ),
                ],
            ),
            (
                "regex",
                [
                    (["ABC"], {}, "uppercase/ABC/"),
                    (["abc"], {}, "lowercase/abc/"),
                ],
            ),
            (
                "converter_to_url",
                [
                    ([6], {}, "int/6/"),
                    ([1], {}, "tiny_int/1/"),
                ],
            ),
        ]
        for url_name, cases in tests:
            for args, kwargs, url_suffix in cases:
                expected_url = "/%s/%s" % (url_name, url_suffix)
                with self.subTest(url=expected_url):
                    self.assertEqual(
                        reverse(url_name, args=args, kwargs=kwargs),
                        expected_url,
                    )


class ParameterRestrictionTests(SimpleTestCase):
    def test_integer_parameter_name_causes_exception(self):
        msg = (
            "URL route 'hello/<int:1>/' uses parameter name '1' which isn't "
            "a valid Python identifier."
        )
        with self.assertRaisesMessage(ImproperlyConfigured, msg):
            path(r"hello/<int:1>/", lambda r: None)

    def test_non_identifier_parameter_name_causes_exception(self):
        msg = (
            "URL route 'b/<int:book.id>/' uses parameter name 'book.id' which "
            "isn't a valid Python identifier."
        )
        with self.assertRaisesMessage(ImproperlyConfigured, msg):
            path(r"b/<int:book.id>/", lambda r: None)

    def test_allows_non_ascii_but_valid_identifiers(self):
        # \u0394 is "GREEK CAPITAL LETTER DELTA", a valid identifier.
        p = path("hello/<str:\u0394>/", lambda r: None)
        match = p.resolve("hello/1/")
        self.assertEqual(match.kwargs, {"\u0394": "1"})


@override_settings(ROOT_URLCONF="urlpatterns.path_dynamic_urls")
class ConversionExceptionTests(SimpleTestCase):
    """How are errors in Converter.to_python() and to_url() handled?"""

    def test_resolve_value_error_means_no_match(self):
        @DynamicConverter.register_to_python
        def raises_value_error(value):
            raise ValueError()

        with self.assertRaises(Resolver404):
            resolve("/dynamic/abc/")

    def test_resolve_type_error_means_no_match(self):
        # Any exception from a converter's to_python() -- not just
        # ValueError -- reports a plain resolution failure so that converter
        # internals never leak through resolve().
        @DynamicConverter.register_to_python
        def raises_type_error(value):
            raise TypeError("This type error does not propagate.")

        with self.assertRaises(Resolver404):
            resolve("/dynamic/abc/")

    def test_resolve_none_return_means_no_match(self):
        # A converter that returns None instead of raising reports "no
        # value", which is a non-match rather than None reaching the view as
        # a keyword argument.
        @DynamicConverter.register_to_python
        def returns_none(value):
            return None

        with self.assertRaises(Resolver404):
            resolve("/dynamic/abc/")

    def test_reverse_value_error_means_no_match(self):
        @DynamicConverter.register_to_url
        def raises_value_error(value):
            raise ValueError

        with self.assertRaises(NoReverseMatch):
            reverse("dynamic", kwargs={"value": object()})

    def test_reverse_type_error_propagates(self):
        @DynamicConverter.register_to_url
        def raises_type_error(value):
            raise TypeError("This type error propagates.")

        with self.assertRaisesMessage(TypeError, "This type error propagates."):
            reverse("dynamic", kwargs={"value": object()})


@override_settings(ROOT_URLCONF="urlpatterns.encoded_urls")
class EncodedPathResolveTests(SimpleTestCase):
    """
    django.urls.resolve() on a modular URLconf (two levels of include(),
    application namespaces, language prefixes, trailing slashes and typed
    converters) must map concrete, possibly percent-encoded, paths to a
    single view and reconstruct the business parameters.
    """

    def test_nested_includes_view_name_and_namespace_chain(self):
        match = resolve("/org/12/sec/news/article/42/")
        self.assertEqual(match.url_name, "article")
        self.assertEqual(match.view_name, "mid:inner:article")
        # Outer-to-inner namespace and app-name order is stable.
        self.assertEqual(match.namespaces, ["mid", "inner"])
        self.assertEqual(match.app_names, ["mid", "inner"])
        # Only genuine captures become parameters; the include prefixes
        # themselves are not folded into kwargs.
        self.assertEqual(match.kwargs, {"org": 12, "section": "news", "pk": 42})
        # The ancestor include captures never masquerade as endpoint
        # captures.
        self.assertEqual(match.captured_kwargs, {"pk": 42})

    def test_nested_include_types(self):
        match = resolve("/org/12/sec/news/article/42/")
        self.assertIsInstance(match.kwargs["org"], int)
        self.assertIsInstance(match.kwargs["pk"], int)
        self.assertIsInstance(match.kwargs["section"], str)

    def test_percent_encoded_unicode_space_and_reserved_characters(self):
        # caf%C3%A9 -> café, %20 -> space, %3F%26%3D%3A -> ?&=:; decoded
        # exactly once and left as the converter's declared type (str).
        match = resolve("/word/caf%C3%A9%20x%3F%26%3D%3A/")
        self.assertEqual(match.view_name, "plain-word")
        self.assertEqual(match.kwargs, {"word": "café x?&=:"})

    def test_encoded_digits_convert_to_int(self):
        match = resolve("/plain/%34%32/")
        self.assertEqual(match.kwargs, {"pk": 42})
        self.assertIsInstance(match.kwargs["pk"], int)

    def test_encoded_value_decoded_exactly_once(self):
        # %252F decodes once to the literal text "%2F", which then fails an
        # <int> capture rather than becoming a slash or the digit 4.
        with self.assertRaises(Resolver404):
            resolve("/plain/%2534/")
        match = resolve("/org/12/sec/news/file/%252F/")
        self.assertEqual(match.kwargs["rest"], "%2F")

    def test_custom_converter_gets_decoded_value_and_declared_type(self):
        match = resolve("/org/12/sec/news/count/%33%37/")
        self.assertEqual(match.view_name, "mid:inner:count")
        self.assertEqual(match.kwargs["number"], 37)
        self.assertIsInstance(match.kwargs["number"], int)

    def test_encoded_slash_is_path_converter_content(self):
        # %2F stays parameter content for <path>: one parameter, decoded
        # once, and never split into extra path levels.
        match = resolve("/org/12/sec/news/file/a%2Fb%2Fc/")
        self.assertEqual(match.view_name, "mid:inner:file")
        self.assertEqual(
            match.kwargs,
            {"org": 12, "section": "news", "rest": "a/b/c"},
        )

    def test_encoded_slash_rejected_by_non_path_converters(self):
        for url in (
            "/org/12/sec/news/slug/a%2Fb/",
            "/plain/1%2F2/",
            "/org/12/sec/news/count/1%2F2/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(Resolver404):
                    resolve(url)

    def test_encoded_space_rejected_by_slug_and_int(self):
        with self.assertRaises(Resolver404):
            resolve("/org/12/sec/news/slug/a%20b/")
        with self.assertRaises(Resolver404):
            resolve("/plain/%20/")

    def test_int_and_slug_empty_or_illegal_values_do_not_hit_siblings(self):
        for url in (
            "/plain//",
            "/plain/abc/",
            "/plain/-1/",
            "/plain/1.5/",
            "/org/12/sec/news/slug//",
            "/org/12/sec/news/slug/star*bad/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(Resolver404):
                    resolve(url)

    def test_malformed_percent_encoding_is_content_not_ignorable_tail(self):
        # A malformed escape next to a typed capture is part of the value and
        # fails a converter that cannot accept it; it is never a droppable
        # suffix that lets the route match.
        for url in ("/plain/42%/", "/plain/42%ZZ/"):
            with self.subTest(url=url):
                with self.assertRaises(Resolver404):
                    resolve(url)
        # A permissive converter keeps the malformed sequence verbatim as
        # parameter content instead of ignoring or re-decoding it.
        match = resolve("/word/ab%2/")
        self.assertEqual(match.kwargs, {"word": "ab%2"})
        match = resolve("/word/%/")
        self.assertEqual(match.kwargs, {"word": "%"})

    def test_invalid_utf8_does_not_match(self):
        with self.assertRaises(Resolver404):
            resolve("/word/%ff%fe/")

    def test_query_string_and_fragment_are_path_content(self):
        for url in ("/plain/42/?x=1", "/plain/42/#frag"):
            with self.subTest(url=url):
                with self.assertRaises(Resolver404):
                    resolve(url)

    def test_trailing_and_duplicate_slashes_are_significant(self):
        with self.assertRaises(Resolver404):
            resolve("/plain/42")
        with self.assertRaises(Resolver404):
            resolve("/plain//42/")

    def test_default_arguments_are_restored(self):
        match = resolve("/org/12/sec/news/defaulted/9/")
        # A route-defined default for a captured parameter name takes
        # precedence over the capture (Django's extra-options semantics),
        # and the unrelated default is restored as well.
        self.assertEqual(
            match.kwargs,
            {"org": 12, "section": "news", "pk": 3, "verified": True},
        )
        self.assertEqual(match.captured_kwargs, {"pk": 9})
        self.assertEqual(match.extra_kwargs, {"pk": 3, "verified": True})

    def test_missing_extra_and_unknown_namespace_fail(self):
        for url in (
            "/org/12/sec/news/",
            "/org/12/sec/news/article/42/extra/",
            "/org/12/zzz/news/article/42/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(Resolver404):
                    resolve(url)

    def test_default_and_switched_language_prefix_hit_same_named_view(self):
        default = resolve("/en/loc/blue/article/1/")
        self.assertEqual(default.view_name, "inner:article")
        self.assertEqual(default.kwargs, {"book": "blue", "pk": 1})
        with override("fr"):
            switched = resolve("/fr/loc/red/article/2/")
        self.assertEqual(switched.view_name, default.view_name)
        self.assertEqual(switched.namespaces, default.namespaces)
        self.assertEqual(switched.app_names, default.app_names)
        self.assertEqual(switched.kwargs, {"book": "red", "pk": 2})

    def test_repeated_and_alternated_resolution_is_stable(self):
        first = resolve("/org/1/sec/a/article/1/")
        for _ in range(3):
            other = resolve("/plain/5/")
            self.assertEqual(other.view_name, "plain")
            self.assertEqual(other.kwargs, {"pk": 5})
            with override("fr"):
                localized = resolve("/fr/loc/a/article/1/")
            self.assertEqual(localized.view_name, "inner:article")
            again = resolve("/org/1/sec/a/article/1/")
            self.assertEqual(again.view_name, first.view_name)
            self.assertEqual(again.namespaces, first.namespaces)
            self.assertEqual(again.app_names, first.app_names)
            self.assertEqual(again.kwargs, first.kwargs)

    def test_failure_then_success_equals_direct_success(self):
        with self.assertRaises(Resolver404):
            resolve("/plain/not-an-int/")
        after_failure = resolve("/plain/7/")
        direct = resolve("/plain/7/")
        self.assertEqual(after_failure.view_name, direct.view_name)
        self.assertEqual(after_failure.namespaces, direct.namespaces)
        self.assertEqual(after_failure.kwargs, direct.kwargs)

    def test_legacy_re_path_keeps_raw_match(self):
        # A non-namespaced regex route keeps its historical behavior: the
        # capture is the raw, undecoded text and there is no namespace.
        match = resolve("/legacy/a%2Fb/")
        self.assertEqual(match.view_name, "legacy")
        self.assertEqual(match.namespaces, [])
        self.assertEqual(match.app_names, [])
        self.assertEqual(match.kwargs, {"rest": "a%2Fb"})


def _temporary_i18n_urlconf(prefix_default_language):
    # A URLconf built for the duration of a single resolve() call, handed
    # over directly as an (unhashable) list rather than installed as
    # settings.ROOT_URLCONF.
    from django.conf.urls.i18n import i18n_patterns

    inner = [path("article/<int:pk>/", empty_view, name="article")]
    return [
        *i18n_patterns(
            path("loc/<slug:book>/", include((inner, "inner"), namespace="inner")),
            prefix_default_language=prefix_default_language,
        ),
        path("plain/<int:pk>/", empty_view, name="plain"),
        re_path(r"^legacy/(?P<rest>.+)/$", empty_view, name="legacy"),
    ]


class TemporaryURLConfResolveTests(SimpleTestCase):
    """
    resolve() must accept a caller-supplied, temporary URLconf -- including a
    plain list of patterns -- and language-prefix matching must be driven by
    the requested path, never by the active language.
    """

    def _snapshot(self, match):
        return (
            match.view_name,
            match.url_name,
            match.args,
            dict(match.kwargs),
            list(match.app_names),
            list(match.namespaces),
            match.route,
            {key: type(value) for key, value in match.kwargs.items()},
        )

    def test_unhashable_list_urlconf_resolves(self):
        urlconf = _temporary_i18n_urlconf(True)
        match = resolve("/en/loc/blue/article/1/", urlconf)
        self.assertEqual(match.view_name, "inner:article")
        self.assertEqual(match.namespaces, ["inner"])
        self.assertEqual(match.kwargs, {"book": "blue", "pk": 1})
        self.assertIsInstance(match.kwargs["pk"], int)

    def test_prefixed_default_language(self):
        urlconf = _temporary_i18n_urlconf(True)
        with override("en"):
            default = resolve("/en/loc/blue/article/1/", urlconf)
            other = resolve("/fr/loc/red/article/2/", urlconf)
        # The path's prefix, not the active language, decides the match.
        with override("fr"):
            default_switched = resolve("/en/loc/blue/article/1/", urlconf)
            other_switched = resolve("/fr/loc/red/article/2/", urlconf)
        self.assertEqual(self._snapshot(default), self._snapshot(default_switched))
        self.assertEqual(self._snapshot(other), self._snapshot(other_switched))
        self.assertEqual(default.view_name, other.view_name)
        self.assertEqual(default.namespaces, other.namespaces)
        self.assertEqual(default.app_names, other.app_names)
        self.assertEqual(
            default.route, "en/loc/<slug:book>/article/<int:pk>/"
        )
        self.assertEqual(other.route, "fr/loc/<slug:book>/article/<int:pk>/")
        # No-prefix requests and unknown languages keep failing.
        for path in ("/loc/blue/article/1/", "/xx/loc/blue/article/1/"):
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf)

    def test_unprefixed_default_language(self):
        urlconf = _temporary_i18n_urlconf(False)
        # The default language is only reachable without its prefix.
        unprefixed = resolve("/loc/blue/article/1/", urlconf)
        self.assertEqual(unprefixed.view_name, "inner:article")
        self.assertEqual(unprefixed.kwargs, {"book": "blue", "pk": 1})
        self.assertEqual(unprefixed.route, "loc/<slug:book>/article/<int:pk>/")
        # The extra default-language prefix fails even while another language
        # is active, and it never reaches an unprefixed sibling.
        with self.assertRaises(Resolver404):
            resolve("/en/loc/blue/article/1/", urlconf)
        # A non-default language prefix still resolves no matter the active
        # language; the unprefixed default mount does too.
        with override("fr"):
            other = resolve("/fr/loc/red/article/2/", urlconf)
            default = resolve("/loc/blue/article/1/", urlconf)
        self.assertEqual(other.view_name, "inner:article")
        self.assertEqual(other.kwargs, {"book": "red", "pk": 2})
        self.assertEqual(self._snapshot(default), self._snapshot(unprefixed))

    @override_settings(LANGUAGE_CODE="en-us")
    def test_unprefixed_regional_default_language(self):
        urlconf = _temporary_i18n_urlconf(False)
        # "en" is the supported variant of the "en-us" default language.
        match = resolve("/loc/blue/article/1/", urlconf)
        self.assertEqual(match.view_name, "inner:article")
        for path in ("/en/loc/blue/article/1/", "/en-us/loc/blue/article/1/"):
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf)

    def test_failure_then_success_is_unchanged(self):
        urlconf = _temporary_i18n_urlconf(True)
        baseline = self._snapshot(resolve("/plain/7/", urlconf))
        for path in (
            "/plain/not-an-int/",
            "/xx/plain/7/",
            "/en/loc/blue/article/not-an-int/",
        ):
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf)
        self.assertEqual(self._snapshot(resolve("/plain/7/", urlconf)), baseline)

    def test_order_independence_and_stable_types(self):
        urlconf = _temporary_i18n_urlconf(True)

        def resolve_all():
            legacy = resolve("/legacy/a%2Fb/", urlconf)
            with override("fr"):
                localized = resolve("/fr/loc/b/article/2/", urlconf)
            plain = resolve("/plain/3/", urlconf)
            return legacy, localized, plain

        first = [self._snapshot(m) for m in resolve_all()]
        second = [self._snapshot(m) for m in reversed(resolve_all())]
        # Reversing the call order must not change any public result.
        self.assertEqual(first, list(reversed(second)))
        # A plain route keeps its trailing-slash rule and int kwargs even
        # after language switches.
        with self.assertRaises(Resolver404):
            resolve("/plain/3", urlconf)
        self.assertEqual(resolve("/plain/3/", urlconf).kwargs, {"pk": 3})
        self.assertIsInstance(resolve("/plain/3/", urlconf).kwargs["pk"], int)

    def test_plain_then_i18n_then_plain_identical(self):
        urlconf = _temporary_i18n_urlconf(True)
        before = resolve("/plain/5/", urlconf)
        with override("fr"):
            resolve("/fr/loc/b/article/5/", urlconf)
        after = resolve("/plain/5/", urlconf)
        self.assertEqual(self._snapshot(before), self._snapshot(after))


_LOCALE_DIR = os.path.join(os.path.dirname(__file__), "locale")


def _i18n_acceptance_urlconf(prefix_default_language):
    # A temporary URLconf handed directly to resolve() as an unhashable
    # list: two levels of include() with application namespaces, language
    # prefixes whose default-prefix behavior is switchable, a plain route
    # and an old-style, non-namespaced re_path().
    inner_urlpatterns = [
        path("article/<int:pk>/", empty_view, name="article"),
        path("code/<btext:code>/", empty_view, name="code"),
    ]
    middle_urlpatterns = [
        path(
            "sec/<slug:section>/",
            include((inner_urlpatterns, "inner"), namespace="inner"),
        ),
    ]
    return [
        *i18n_patterns(
            path(
                "top/<slug:book>/",
                include((middle_urlpatterns, "mid"), namespace="mid"),
            ),
            prefix_default_language=prefix_default_language,
        ),
        path("plain/<int:pk>/", empty_view, name="plain"),
        re_path(r"^legacy/(?P<rest>.+)/$", empty_view, name="legacy"),
    ]


@override_settings(
    USE_I18N=True,
    LANGUAGE_CODE="en",
    LANGUAGES=[("en", "English"), ("zh", "Chinese")],
    LOCALE_PATHS=[_LOCALE_DIR],
)
class I18NPrefixAndConverterResolveTests(SimpleTestCase):
    """
    Stable forward resolution (django.urls.resolve) under switchable default
    language prefixes and custom-converter boundaries. Every call is driven
    solely by the URLconf and path it receives.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._registered = {}
        for type_name, converter_cls in (
            ("btext", BoundedTextConverter),
            ("reject", RejectingConverter),
            ("noneval", NoneReturningConverter),
        ):
            instance = converter_cls()
            cls._registered[type_name] = REGISTERED_CONVERTERS.get(type_name)
            REGISTERED_CONVERTERS[type_name] = instance
        get_converters.cache_clear()
        _route_to_regex.cache_clear()

    @classmethod
    def tearDownClass(cls):
        for type_name, previous in cls._registered.items():
            if previous is None:
                REGISTERED_CONVERTERS.pop(type_name, None)
            else:
                REGISTERED_CONVERTERS[type_name] = previous
        get_converters.cache_clear()
        _route_to_regex.cache_clear()
        super().tearDownClass()

    def _snapshot(self, match):
        return (
            match.func,
            match.url_name,
            tuple(match.args),
            tuple((k, v, type(v)) for k, v in sorted(match.kwargs.items())),
            tuple(match.app_names),
            tuple(match.namespaces),
            match.route,
        )

    def _urlconf_pair(self):
        return (
            _i18n_acceptance_urlconf(True),
            _i18n_acceptance_urlconf(False),
        )

    # -- default-language prefix switch -----------------------------------

    def test_prefixed_default_and_other_language(self):
        urlconf, _ = self._urlconf_pair()
        en = resolve("/en/top/book1/sec/sec1/article/10/", urlconf)
        zh = resolve("/zh/top/book2/sec/sec2/article/20/", urlconf)
        self.assertEqual(en.view_name, "mid:inner:article")
        self.assertEqual(zh.view_name, "mid:inner:article")
        self.assertEqual(en.namespaces, ["mid", "inner"])
        self.assertEqual(en.app_names, ["mid", "inner"])
        self.assertEqual(en.kwargs, {"book": "book1", "section": "sec1", "pk": 10})
        self.assertEqual(zh.kwargs, {"book": "book2", "section": "sec2", "pk": 20})
        self.assertIsInstance(en.kwargs["pk"], int)
        self.assertEqual(
            en.route,
            "en/top/<slug:book>/sec/<slug:section>/article/<int:pk>/",
        )
        self.assertEqual(
            zh.route,
            "zh/top/<slug:book>/sec/<slug:section>/article/<int:pk>/",
        )
        # The request's prefix, not the active language, decides the match.
        with override("zh"):
            en_switched = resolve("/en/top/book1/sec/sec1/article/10/", urlconf)
        with override("en"):
            zh_switched = resolve("/zh/top/book2/sec/sec2/article/20/", urlconf)
        self.assertEqual(self._snapshot(en_switched), self._snapshot(en))
        self.assertEqual(self._snapshot(zh_switched), self._snapshot(zh))
        # With prefixing on, the default language has no unprefixed mount.
        with self.assertRaises(Resolver404):
            resolve("/top/book1/sec/sec1/article/10/", urlconf)

    def test_unprefixed_default_language_only(self):
        _, urlconf = self._urlconf_pair()
        unprefixed = resolve("/top/book1/sec/sec1/article/10/", urlconf)
        self.assertEqual(unprefixed.view_name, "mid:inner:article")
        self.assertEqual(
            unprefixed.kwargs, {"book": "book1", "section": "sec1", "pk": 10}
        )
        self.assertEqual(
            unprefixed.route,
            "top/<slug:book>/sec/<slug:section>/article/<int:pk>/",
        )
        # The extra default-language prefix always fails and never reaches an
        # unprefixed sibling, regardless of the active language.
        for active in ("en", "zh"):
            with self.subTest(active=active), override(active):
                with self.assertRaises(Resolver404):
                    resolve("/en/top/book1/sec/sec1/article/10/", urlconf)
        # The unprefixed request is the default mount even with zh active:
        # the language never leaks into the captured parameters.
        with override("zh"):
            switched = resolve("/top/book1/sec/sec1/article/10/", urlconf)
        self.assertEqual(self._snapshot(switched), self._snapshot(unprefixed))
        # The non-default language still needs its prefix.
        with override("en"):
            zh = resolve("/zh/top/book2/sec/sec2/article/20/", urlconf)
        self.assertEqual(zh.kwargs, {"book": "book2", "section": "sec2", "pk": 20})

    def test_mismatched_active_language_segment_is_not_captured(self):
        urlconf, _ = self._urlconf_pair()
        with override("zh"):
            match = resolve("/en/top/book1/sec/sec1/article/10/", urlconf)
        self.assertNotIn("en", match.kwargs.values())
        self.assertEqual(
            match.kwargs, {"book": "book1", "section": "sec1", "pk": 10}
        )
        # The prefixed mount cannot masquerade as the plain sibling.
        with self.assertRaises(Resolver404):
            resolve("/en/plain/10/", urlconf)

    # -- alternation / repeatability --------------------------------------

    def test_alternating_paths_then_back_to_first(self):
        urlconf, _ = self._urlconf_pair()
        first = self._snapshot(resolve("/en/top/b/sec/s/article/1/", urlconf))
        resolve("/zh/top/c/sec/t/article/2/", urlconf)
        legacy = resolve("/legacy/a%2Fb/", urlconf)
        self.assertEqual(legacy.view_name, "legacy")
        self.assertEqual(legacy.namespaces, [])
        self.assertEqual(legacy.app_names, [])
        self.assertEqual(legacy.kwargs, {"rest": "a%2Fb"})
        again = self._snapshot(resolve("/en/top/b/sec/s/article/1/", urlconf))
        self.assertEqual(again, first)

    def test_call_order_does_not_change_results(self):
        urlconf, _ = self._urlconf_pair()
        paths = [
            "/en/top/b/sec/s/article/1/",
            "/zh/top/c/sec/t/article/2/",
            "/legacy/x/",
            "/plain/3/",
        ]

        def run(order):
            return [self._snapshot(resolve(p, urlconf)) for p in order]

        forward = run(paths)
        backward = run(reversed(paths))
        self.assertEqual(forward, list(reversed(backward)))

    def test_same_path_resolves_identically_on_repeat(self):
        urlconf, _ = self._urlconf_pair()
        first = self._snapshot(resolve("/en/top/b/sec/s/code/ab/", urlconf))
        for _ in range(3):
            self.assertEqual(
                self._snapshot(resolve("/en/top/b/sec/s/code/ab/", urlconf)),
                first,
            )

    # -- custom converter --------------------------------------------------

    def test_bounded_converter_accepts_and_returns_single_value(self):
        urlconf, _ = self._urlconf_pair()
        match = resolve("/en/top/b/sec/s/code/ab/", urlconf)
        self.assertEqual(match.view_name, "mid:inner:code")
        self.assertEqual(
            match.kwargs, {"book": "b", "section": "s", "code": "AB"}
        )
        self.assertIsInstance(match.kwargs["code"], str)
        self.assertEqual(match.captured_kwargs, {"code": "AB"})

    def test_encoded_unicode_space_and_reserved_chars_decoded_once(self):
        urlconf, _ = self._urlconf_pair()
        match = resolve(
            "/zh/top/b/sec/s/code/caf%C3%A9%20x%3F%26%3D%3A/", urlconf
        )
        # caf%C3%A9 -> café, %20 -> space, %3F%26%3D%3A -> ?&=:; to_python()
        # receives the decoded text exactly once.
        self.assertEqual(match.kwargs["code"], "CAFÉ X?&=:")
        self.assertEqual(
            list(match.kwargs), ["book", "section", "code"]
        )

    def test_encoded_acceptable_value_decoded_once(self):
        urlconf, _ = self._urlconf_pair()
        match = resolve("/en/top/b/sec/s/code/%61%62/", urlconf)
        self.assertEqual(match.kwargs["code"], "AB")
        # Double-encoded text decodes once into a literal '%' the converter
        # regex rejects; it never gets decoded a second time.
        with self.assertRaises(Resolver404):
            resolve("/en/top/b/sec/s/code/%2561%2562/", urlconf)

    def test_encoded_slash_does_not_change_path_level(self):
        urlconf, _ = self._urlconf_pair()
        with self.assertRaises(Resolver404):
            resolve("/en/top/b/sec/s/code/a%2Fb/", urlconf)

    def test_bounded_converter_rejects_invalid_values(self):
        urlconf, _ = self._urlconf_pair()
        invalid_paths = (
            "/en/top/b/sec/s/code//",                    # empty
            "/en/top/b/sec/s/code/abcdefghijklm/",       # over the bound
            "/en/top/b/sec/s/code/ab%2A/",               # encoded '*'
            "/en/top/b/sec/s/code/ab%/",                 # malformed escape
            "/en/top/b/sec/s/code/ab%ZZ/",               # malformed escape
            "/en/top/b/sec/s/code/%ff%fe/",              # invalid UTF-8
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf)

    def test_rejecting_and_none_returning_converters_are_404(self):
        urlconf = [
            path("bad/<reject:n>/", empty_view, name="bad"),
            path("nil/<noneval:n>/", empty_view, name="nil"),
        ]
        with self.assertRaises(Resolver404):
            resolve("/bad/5/", urlconf)
        with self.assertRaises(Resolver404):
            resolve("/nil/5/", urlconf)

    # -- failure semantics -------------------------------------------------

    def test_invalid_requests_only_raise_resolver404(self):
        urlconf, _ = self._urlconf_pair()
        invalid_paths = (
            "/xx/top/b/sec/s/article/1/",        # unknown language
            "/en/top/b/sec/s/article/",          # missing capture
            "/en/top/b/sec/s/article/1/more/",   # extra path
            "/en//top/b/sec/s/article/1/",       # duplicate slash
            "/plain//7/",                        # duplicate slash
            "/plain/7/?x=1",                     # query string
            "/plain/7/#frag",                    # fragment
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf)

    def test_failure_leaves_no_state_for_the_next_call(self):
        urlconf, _ = self._urlconf_pair()
        baseline = self._snapshot(resolve("/plain/7/", urlconf))
        for path in (
            "/plain/not-an-int/",
            "/xx/top/b/sec/s/article/1/",
            "/en/top/b/sec/s/article/not-an-int/",
            "/en/top/b/sec/s/code/ab%2A/",
        ):
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf)
        self.assertEqual(self._snapshot(resolve("/plain/7/", urlconf)), baseline)

    def test_switching_prefix_flag_leaves_no_state(self):
        prefixed, unprefixed = self._urlconf_pair()
        prefixed_match = self._snapshot(
            resolve("/en/top/b/sec/s/article/1/", prefixed)
        )
        unprefixed_match = self._snapshot(
            resolve("/top/b/sec/s/article/1/", unprefixed)
        )
        # Re-resolution after using the other URLconf is unchanged.
        self.assertEqual(
            self._snapshot(resolve("/en/top/b/sec/s/article/1/", prefixed)),
            prefixed_match,
        )
        self.assertEqual(
            self._snapshot(resolve("/top/b/sec/s/article/1/", unprefixed)),
            unprefixed_match,
        )
        # The prefix switch cannot change converter types or parameter keys.
        plain_prefixed = resolve("/plain/9/", prefixed)
        plain_unprefixed = resolve("/plain/9/", unprefixed)
        self.assertEqual(set(plain_prefixed.kwargs), set(plain_unprefixed.kwargs))
        self.assertEqual(
            {k: type(v) for k, v in plain_prefixed.kwargs.items()},
            {k: type(v) for k, v in plain_unprefixed.kwargs.items()},
        )

    # -- non-internationalized compatibility ------------------------------

    def test_plain_route_name_and_trailing_slash(self):
        urlconf, _ = self._urlconf_pair()
        match = resolve("/plain/7/", urlconf)
        self.assertEqual(match.view_name, "plain")
        self.assertEqual(match.namespaces, [])
        self.assertEqual(match.kwargs, {"pk": 7})
        self.assertIsInstance(match.kwargs["pk"], int)
        with self.assertRaises(Resolver404):
            resolve("/plain/7", urlconf)


def _query_fragment_urlconf():
    # A temporary URLconf handed directly to resolve(): converter endpoints
    # both with and without a trailing slash, a capture followed by a route
    # literal, an include(), language prefixes and a legacy re_path().
    from django.conf.urls.i18n import i18n_patterns

    nested = [path("d/<path:p>", empty_view, name="inc-d")]
    return [
        *i18n_patterns(
            path("q/<path:p>", empty_view, name="q"),
            path("u/<str:name>", empty_view, name="u"),
            path("mid/<path:p>/after/", empty_view, name="mid-after"),
            path("inc/", include(nested)),
        ),
        path("plain/<path:p>", empty_view, name="plain-p"),
        path("literal/<path:p>/after/", empty_view, name="literal-after"),
        re_path(r"^legacy/(?P<oid>[0-9]+)/$", empty_view, name="legacy"),
    ]


@override_settings(
    USE_I18N=True,
    LANGUAGE_CODE="en",
    LANGUAGES=[("en", "English"), ("zh", "Chinese")],
    LOCALE_PATHS=[_LOCALE_DIR],
)
class QueryFragmentTerminalCaptureResolveTests(SimpleTestCase):
    """
    A literal '?' or '#' -- the query-string and fragment delimiters -- must
    never be silently swallowed by a converter capture that terminates a
    route. The WSGI gateway strips a genuine query/fragment before building
    PATH_INFO, but a percent-encoded '%3F'/'%23' is decoded into a literal
    '?'/'#' on its way in, so the resolver distinguishes the two by position:
    a delimiter that still has route text after it is data (as in the admin's
    '<path:object_id>/change/'), while one reached by the greedy, terminating
    capture of a <path>/<str> route is a query or fragment leaking into the
    match and must fail with Resolver404.
    """

    def setUp(self):
        self.urlconf = _query_fragment_urlconf()

    def test_terminal_capture_rejects_query_and_fragment(self):
        invalid_paths = (
            "/plain/a?x=1",
            "/plain/a#frag",
            "/plain/a?x=1#f",
            "/q/a?x=1",
            "/q/a#frag",
            "/q/a?x=1#f",
            "/u/a?x=1",
            "/u/a#frag",
            "/inc/d/a?x=1",
            "/inc/d/a#f",
            "/en/q/a?x=1",
            "/en/u/a#frag",
            "/zh/q/a?x=1",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, self.urlconf)

    def test_repeated_resolution_after_query_failure_is_unchanged(self):
        with self.assertRaises(Resolver404):
            resolve("/plain/a?x=1", self.urlconf)
        match = resolve("/plain/a%3Fx%3D1", self.urlconf)
        self.assertEqual(match.kwargs, {"p": "a?x=1"})
        with self.assertRaises(Resolver404):
            resolve("/plain/a#f", self.urlconf)
        again = resolve("/plain/a%3Fx%3D1", self.urlconf)
        self.assertEqual(again.kwargs, match.kwargs)
        self.assertEqual(type(again.kwargs["p"]), type(match.kwargs["p"]))

    def test_encoded_delimiter_in_terminal_capture_is_data(self):
        # '%3F'/'%23' are decoded once and kept as the single parameter value;
        # a double-encoded escape decodes once into a literal '%3F'.
        self.assertEqual(
            resolve("/plain/a%3Fx%3D1", self.urlconf).kwargs, {"p": "a?x=1"}
        )
        self.assertEqual(
            resolve("/plain/a%23frag", self.urlconf).kwargs, {"p": "a#frag"}
        )
        self.assertEqual(resolve("/zh/q/a%3Fb", self.urlconf).kwargs, {"p": "a?b"})
        self.assertEqual(
            resolve("/plain/a%253Fb", self.urlconf).kwargs, {"p": "a%3Fb"}
        )

    def test_literal_delimiter_with_following_route_text_is_data(self):
        # A capture followed by a route literal (or more of an include) can
        # hold a decoded '?'/'#', mirroring the admin object-id routes. The
        # parameter is returned once, with its type and key unchanged by the
        # language prefix.
        en = resolve("/en/mid/a?x=1/after/", self.urlconf)
        zh = resolve("/zh/mid/a%23b/after/", self.urlconf)
        self.assertEqual(en.kwargs, {"p": "a?x=1"})
        self.assertEqual(zh.kwargs, {"p": "a#b"})
        plain = resolve("/literal/a?x=1/after/", self.urlconf)
        self.assertEqual(plain.kwargs, {"p": "a?x=1"})
        self.assertEqual(type(en.kwargs["p"]), type(plain.kwargs["p"]))
        self.assertEqual(list(en.kwargs), list(plain.kwargs))

    def test_legacy_re_path_with_trailing_slash_rejects_suffix(self):
        with self.assertRaises(Resolver404):
            resolve("/legacy/9/?x=1", self.urlconf)
        with self.assertRaises(Resolver404):
            resolve("/legacy/9/#f", self.urlconf)
        self.assertEqual(resolve("/legacy/9/", self.urlconf).kwargs, {"oid": "9"})


def refresh_view_one(request):
    raise NotImplementedError


def refresh_view_two(request):
    raise NotImplementedError


class _RefreshableURLConf:
    """A plain urlconf object whose urlpatterns can be replaced at runtime."""

    def __init__(self, urlpatterns):
        self.urlpatterns = urlpatterns


def _old_patterns():
    return [
        path("old/<int:pk>/", refresh_view_one, name="old"),
        re_path(r"^legacy/(?P<rest>.+)/$", refresh_view_one, name="legacy"),
    ]


def _new_nested_patterns():
    # Two levels of include() with application namespaces, a trailing slash
    # and a typed converter on the include prefix as well as the endpoint.
    inner_urlpatterns = [
        path("article/<int:article_id>/", refresh_view_two, name="article"),
    ]
    middle_urlpatterns = [
        path(
            "sec/<slug:section>/",
            include((inner_urlpatterns, "inner"), namespace="inner"),
        ),
    ]
    return [
        path(
            "org/<int:org>/",
            include((middle_urlpatterns, "mid"), namespace="mid"),
        ),
    ]


def _match_snapshot(match):
    return (
        match.func,
        match.view_name,
        match.url_name,
        match.args,
        dict(match.kwargs),
        {key: type(value) for key, value in match.kwargs.items()},
        list(match.app_names),
        list(match.namespaces),
        match.route,
        dict(match.captured_kwargs),
        dict(match.extra_kwargs),
    )


class RuntimeURLConfRefreshTests(SimpleTestCase):
    """
    resolve(path, urlconf=...) must observe a urlpatterns table replaced on
    the very urlconf object the caller passes in -- without the caller
    clearing an internal cache -- while failures, other urlconfs and legacy
    routes never leak state into a later successful match.
    """

    def setUp(self):
        self.urlconf = _RefreshableURLConf(_old_patterns())

    def test_replacement_is_observed_without_clearing_cache(self):
        match = resolve("/old/42/", self.urlconf)
        self.assertIs(match.func, refresh_view_one)
        self.assertEqual(match.view_name, "old")
        self.assertEqual(match.kwargs, {"pk": 42})

        self.urlconf.urlpatterns = _new_nested_patterns()
        match = resolve("/org/12/sec/news/article/7/", self.urlconf)
        # The new table provides the new view, namespace chain and captures.
        self.assertIs(match.func, refresh_view_two)
        self.assertEqual(match.view_name, "mid:inner:article")
        self.assertEqual(match.url_name, "article")
        self.assertEqual(match.namespaces, ["mid", "inner"])
        self.assertEqual(match.app_names, ["mid", "inner"])
        self.assertEqual(match.kwargs, {"org": 12, "section": "news", "article_id": 7})
        # Include prefixes are not endpoint captures and never duplicated.
        self.assertEqual(match.captured_kwargs, {"article_id": 7})
        self.assertEqual(
            match.route,
            "org/<int:org>/sec/<slug:section>/article/<int:article_id>/",
        )
        # The previous route is gone immediately.
        with self.assertRaises(Resolver404):
            resolve("/old/42/", self.urlconf)

    def test_types_and_single_decoding_after_refresh(self):
        self.urlconf.urlpatterns = [
            path("num/<int:value>/", refresh_view_two, name="num"),
            path("word/<str:word>/", refresh_view_one, name="word"),
        ]
        match = resolve("/num/%33%37/", self.urlconf)
        self.assertEqual(match.kwargs, {"value": 37})
        self.assertIsInstance(match.kwargs["value"], int)
        # A double-encoded value decodes once into the literal escape text.
        match = resolve("/word/%2537/", self.urlconf)
        self.assertEqual(match.kwargs, {"word": "%37"})

    def test_failures_report_resolver404_and_do_not_pollute(self):
        self.urlconf.urlpatterns = _new_nested_patterns()
        # A missing/extra segment and a converter-rejecting value all 404.
        for url in (
            "/org/12/sec/news/",
            "/org/12/sec/news/article/7/extra/",
            "/org/12/sec/news/article/not-an-int/",
            "/org/not-an-int/sec/news/article/7/",
            "/org/12/sec/a%20b/article/7/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(Resolver404):
                    resolve(url, self.urlconf)
        # A following success carries no trace of the failed attempts.
        match = resolve("/org/12/sec/news/article/7/", self.urlconf)
        self.assertEqual(match.namespaces, ["mid", "inner"])
        self.assertEqual(match.app_names, ["mid", "inner"])
        self.assertEqual(match.kwargs, {"org": 12, "section": "news", "article_id": 7})
        self.assertEqual(match.captured_kwargs, {"article_id": 7})

    def test_resolve_nonexistent_path_then_refresh(self):
        with self.assertRaises(Resolver404):
            resolve("/does/not/exist/", self.urlconf)
        self.urlconf.urlpatterns = _new_nested_patterns()
        match = resolve("/org/3/sec/tech/article/9/", self.urlconf)
        self.assertEqual(match.view_name, "mid:inner:article")
        self.assertEqual(match.kwargs, {"org": 3, "section": "tech", "article_id": 9})

    def test_refresh_then_old_path_does_not_leak_namespace_or_kwargs(self):
        self.urlconf.urlpatterns = _new_nested_patterns()
        resolve("/org/1/sec/a/article/1/", self.urlconf)
        self.urlconf.urlpatterns = [
            path("plain/<int:pk>/", refresh_view_one, name="plain"),
        ]
        match = resolve("/plain/5/", self.urlconf)
        self.assertEqual(match.view_name, "plain")
        self.assertEqual(match.namespaces, [])
        self.assertEqual(match.app_names, [])
        self.assertEqual(match.kwargs, {"pk": 5})
        self.assertEqual(match.captured_kwargs, {"pk": 5})
        with self.assertRaises(Resolver404):
            resolve("/org/1/sec/a/article/1/", self.urlconf)

    def test_two_urlconfs_alternate_without_cross_contamination(self):
        other = _RefreshableURLConf(
            [path("other/<int:x>/", refresh_view_two, name="other")]
        )
        self.urlconf.urlpatterns = _new_nested_patterns()
        first = resolve("/org/1/sec/a/article/1/", self.urlconf)
        second = resolve("/other/2/", other)
        third = resolve("/org/4/sec/b/article/3/", self.urlconf)
        fourth = resolve("/other/5/", other)
        self.assertEqual(first.namespaces, ["mid", "inner"])
        self.assertEqual(second.namespaces, [])
        self.assertEqual(third.namespaces, ["mid", "inner"])
        self.assertEqual(fourth.namespaces, [])
        self.assertEqual(second.kwargs, {"x": 2})
        self.assertEqual(third.kwargs, {"org": 4, "section": "b", "article_id": 3})
        self.assertEqual(fourth.kwargs, {"x": 5})

    def test_unrefreshed_urlconf_is_unchanged(self):
        stable = _RefreshableURLConf(
            [path("stable/<int:pk>/", refresh_view_two, name="stable")]
        )
        resolve("/stable/1/", stable)
        # Replacing the first urlconf repeatedly never touches the second.
        self.urlconf.urlpatterns = _new_nested_patterns()
        resolve("/org/1/sec/a/article/1/", self.urlconf)
        self.urlconf.urlpatterns = [path("x/", refresh_view_one)]
        match = resolve("/stable/2/", stable)
        self.assertIs(match.func, refresh_view_two)
        self.assertEqual(match.view_name, "stable")
        self.assertEqual(match.kwargs, {"pk": 2})

    def test_invalid_replacement_keeps_previous_table_until_fixed(self):
        resolve("/old/1/", self.urlconf)
        # An entry that is not a pattern cannot replace the working table.
        self.urlconf.urlpatterns = [object()]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/1/", self.urlconf)
        # An uncompilable regex likewise fails without committing.
        self.urlconf.urlpatterns = [re_path(r"[", refresh_view_two, name="bad")]
        with self.assertRaises(ImproperlyConfigured):
            resolve("/old/1/", self.urlconf)
        # Fixing the table gives the complete new result immediately.
        self.urlconf.urlpatterns = _new_nested_patterns()
        match = resolve("/org/7/sec/news/article/8/", self.urlconf)
        self.assertEqual(match.view_name, "mid:inner:article")
        self.assertEqual(match.kwargs, {"org": 7, "section": "news", "article_id": 8})
        with self.assertRaises(Resolver404):
            resolve("/old/1/", self.urlconf)

    def test_repeated_equivalent_replacements_are_comparable(self):
        table = [path("plain/<int:pk>/", refresh_view_one, name="plain")]
        self.urlconf.urlpatterns = table
        baseline = _match_snapshot(resolve("/plain/9/", self.urlconf))
        for _ in range(3):
            self.urlconf.urlpatterns = [
                path("plain/<int:pk>/", refresh_view_one, name="plain")
            ]
            self.assertEqual(
                _match_snapshot(resolve("/plain/9/", self.urlconf)), baseline
            )

    def test_legacy_re_path_keeps_raw_match_after_refresh(self):
        self.urlconf.urlpatterns = _new_nested_patterns()
        resolve("/org/1/sec/a/article/1/", self.urlconf)
        self.urlconf.urlpatterns = _old_patterns()
        match = resolve("/legacy/a%2Fb/", self.urlconf)
        self.assertEqual(match.view_name, "legacy")
        self.assertEqual(match.namespaces, [])
        self.assertEqual(match.app_names, [])
        # re_path() captures stay raw and undecoded.
        self.assertEqual(match.kwargs, {"rest": "a%2Fb"})
        with self.assertRaises(Resolver404):
            resolve("/org/1/sec/a/article/1/", self.urlconf)

    def test_reverse_observes_refresh(self):
        self.urlconf.urlpatterns = _new_nested_patterns()
        self.assertEqual(
            reverse(
                "mid:inner:article",
                kwargs={"org": 1, "section": "a", "article_id": 2},
                urlconf=self.urlconf,
            ),
            "/org/1/sec/a/article/2/",
        )
        self.urlconf.urlpatterns = [
            path("plain/<int:pk>/", refresh_view_one, name="plain"),
        ]
        self.assertEqual(
            reverse("plain", kwargs={"pk": 5}, urlconf=self.urlconf), "/plain/5/"
        )
        with self.assertRaises(NoReverseMatch):
            reverse(
                "mid:inner:article",
                kwargs={"org": 1, "section": "a", "article_id": 2},
                urlconf=self.urlconf,
            )

    def test_resolver_identity_is_stable_across_refresh(self):
        first = get_resolver(self.urlconf)
        resolve("/old/1/", self.urlconf)
        self.urlconf.urlpatterns = _new_nested_patterns()
        resolve("/org/1/sec/a/article/1/", self.urlconf)
        # The same long-lived resolver now serves the new table.
        self.assertIs(get_resolver(self.urlconf), first)

    def test_in_place_change_to_held_list_remains_visible(self):
        # An in-place edit of the retained patterns list is seen by resolve()
        # without replacing urlpatterns or clearing a cache. (Assigning a new
        # urlpatterns object is the refresh covered by the other tests; an
        # in-place edit that leaves an invalid candidate is covered in
        # urlpatterns_reverse.test_urlconf_refresh.)
        table = [path("a/<int:pk>/", refresh_view_one, name="a")]
        self.urlconf.urlpatterns = table
        self.assertEqual(resolve("/a/1/", self.urlconf).kwargs, {"pk": 1})
        table.append(path("b/<int:x>/", refresh_view_two, name="b"))
        self.assertEqual(resolve("/b/2/", self.urlconf).kwargs, {"x": 2})
        self.assertIs(resolve("/b/2/", self.urlconf).func, refresh_view_two)
        self.assertEqual(resolve("/a/3/", self.urlconf).kwargs, {"pk": 3})


