"""
Stability tests for django.urls.resolve() around the i18n language prefix
toggle and custom path converters.

The tests build a temporary URLconf with English and Chinese language
prefixes, a switchable default-language prefix, a two-level include and a
namespace-less old-style route, then call resolve() directly on concrete
paths.
"""

import os

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from django.urls.exceptions import Resolver404
from django.urls.resolvers import get_resolver
from django.utils.translation import activate, deactivate, override

from . import urls
from . import views

LANGUAGES = [("en", "English"), ("zh", "Chinese")]
LOCALE_PATHS = [os.path.join(os.path.dirname(__file__), "locale")]


@override_settings(
    USE_I18N=True,
    LANGUAGE_CODE="en",
    LANGUAGES=LANGUAGES,
    LOCALE_PATHS=LOCALE_PATHS,
)
class ResolveI18NTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.prefixed = urls.i18n_urlconf(prefix_default_language=True)
        cls.unprefixed = urls.i18n_urlconf(prefix_default_language=False)
        activate("en")

    @classmethod
    def tearDownClass(cls):
        deactivate()
        super().tearDownClass()

    def assertResolves(self, path_, urlconf, func, lang="en", **kwargs):
        with override(lang):
            match = resolve(path_, urlconf)
        self.assertIs(match.func, func)
        self.assertEqual(match.kwargs, kwargs)
        return match

    def assert404(self, path_, urlconf, lang="en"):
        """
        Every rejection must end as the conventional Resolver404 -- no
        internal exception may leak and no partial match be returned.
        """
        with override(lang):
            with self.assertRaises(Resolver404):
                resolve(path_, urlconf)

    # -- default language prefix switch -----------------------------------

    def test_prefixed_paths_resolve_under_every_active_language(self):
        for active in ("en", "zh"):
            for prefix in ("en", "zh"):
                with self.subTest(active=active, prefix=prefix):
                    self.assertResolves(
                        f"/{prefix}/simple/", self.prefixed, views.simple_view,
                        lang=active,
                    )

    def test_unprefixed_default_fails_when_prefix_enabled(self):
        self.assert404("/simple/", self.prefixed, "en")
        self.assert404("/simple/", self.prefixed, "zh")

    def test_default_language_unprefixed_when_prefix_disabled(self):
        for active in ("en", "zh"):
            with self.subTest(active=active):
                self.assertResolves(
                    "/simple/", self.unprefixed, views.simple_view, lang=active
                )

    def test_prefixed_default_keeps_failing_when_prefix_disabled(self):
        for active in ("en", "zh"):
            self.assert404("/en/simple/", self.unprefixed, active)

    def test_other_language_still_prefixed_when_default_is_not(self):
        self.assertResolves(
            "/zh/simple/", self.unprefixed, views.simple_view, lang="en"
        )

    # -- active language vs. request prefix disagreement ------------------

    def test_request_prefix_drives_resolution_not_active_language(self):
        match = self.assertResolves(
            "/en/shop/market/item/42/",
            self.prefixed,
            views.item_view,
            lang="zh",
            pk=42,
        )
        self.assertEqual(match.namespaces, ["market"])
        self.assertEqual(match.app_names, ["market"])
        self.assertEqual(match.url_name, "item")
        self.assertIsInstance(match.kwargs["pk"], int)
        # The route is reconstructed from the requested path, so it keeps the
        # "en/" segment even though Chinese was active.
        self.assertTrue(match.route.startswith("en/"))

    def test_other_prefix_identical_under_swapped_active_language(self):
        with override("zh"):
            en_match = resolve("/en/shop/market/item/42/", self.prefixed)
        with override("en"):
            zh_match = resolve("/zh/shop/market/item/42/", self.prefixed)
        for match in (en_match, zh_match):
            self.assertIs(match.func, views.item_view)
            self.assertEqual(match.kwargs, {"pk": 42})
            self.assertEqual(match.namespaces, ["market"])
            self.assertEqual(match.app_names, ["market"])
            self.assertEqual(match.url_name, "item")

    def test_unknown_language_segment_is_404(self):
        for active in ("en", "zh"):
            self.assert404("/fr/simple/", self.prefixed, active)

    def test_language_lookalike_segment_is_not_a_prefix(self):
        # The segment is validated as a whole language code; neither a
        # hyphenated lookalike nor a bare segment may be mistaken for "en".
        for path_ in ("/en-simple/", "/english/simple/", "/e/simple/"):
            with self.subTest(path=path_):
                self.assert404(path_, self.prefixed)

    # -- alternating resolution -------------------------------------------

    def test_alternating_languages_and_routes_is_deterministic(self):
        def signature(path_):
            match = resolve(path_, self.prefixed)
            return (
                match.func,
                match.args,
                match.kwargs,
                match.url_name,
                tuple(match.app_names),
                tuple(match.namespaces),
                tuple((k, type(v)) for k, v in match.kwargs.items()),
            )

        first = signature("/en/shop/market/item/7/")
        with override("zh"):
            self.assertEqual(
                signature("/zh/shop/market/item/7/")[0], views.item_view
            )
            legacy = resolve("/en/legacy/123/", self.prefixed)
        self.assertIs(legacy.func, views.legacy_view)
        self.assertEqual(legacy.url_name, "legacy")
        self.assertEqual(legacy.namespaces, [])
        # Old-style regex captures stay strings.
        self.assertEqual(legacy.kwargs, {"legacy_id": "123"})
        self.assertIsInstance(legacy.kwargs["legacy_id"], str)
        # Back to the first path: view identity, namespace chain, parameter
        # values and their types match the first call one by one.
        with override("zh"):
            again = signature("/en/shop/market/item/7/")
        self.assertEqual(first, again)

    # -- custom converter: percent-encoding boundaries --------------------

    def test_mixed_percent_encoded_unicode_decoded_once(self):
        match = self.assertResolves(
            "/en/bounded/caf%C3%A9/", self.prefixed, views.bounded_view,
            name="café",
        )
        self.assertIsInstance(match.kwargs["name"], str)

    def test_fully_encoded_cjk_is_a_single_parameter(self):
        self.assertResolves(
            "/zh/bounded/%E4%B8%AD/", self.prefixed, views.bounded_view,
            lang="zh", name="中",
        )

    def test_encoded_value_decoded_only_once(self):
        # "%25E4..." decodes once to the literal text "%E4%B8%AD", which the
        # converter rejects; it must never be decoded a second time into
        # "中".
        self.assert404("/en/bounded/%25E4%25B8%25AD/", self.prefixed)

    def test_encoded_value_through_nested_include(self):
        match = self.assertResolves(
            "/zh/shop/market/bounded/caf%C3%A9/",
            self.prefixed,
            views.bounded_view,
            lang="en",
            name="café",
        )
        self.assertEqual(match.namespaces, ["market"])
        # The prefix changes neither the parameter key nor its type.
        self.assertEqual(list(match.kwargs), ["name"])
        self.assertIsInstance(match.kwargs["name"], str)

    def test_encoded_slash_does_not_open_a_path_level(self):
        self.assert404("/en/bounded/a%2Fb/", self.prefixed)
        self.assert404("/en/shop/market/bounded/a%2Fb/", self.prefixed)

    def test_space_and_reserved_characters_are_rejected(self):
        for path_ in (
            "/en/bounded/a%20b/",
            "/en/bounded/a b/",
            "/en/bounded/a%3Fb/",
            "/en/bounded/a@b/",
        ):
            with self.subTest(path=path_):
                self.assert404(path_, self.prefixed)

    def test_bounded_length_enforced_after_decoding(self):
        self.assert404("/en/bounded/abcdefghi/", self.prefixed)
        # Nine encoded "é" characters: the raw form passes any length-based
        # matching, so the bound must be re-applied after decoding.
        self.assert404("/en/bounded/" + "%C3%A9" * 9 + "/", self.prefixed)

    def test_empty_malformed_and_invalid_escapes_are_rejected(self):
        for path_ in (
            "/en/bounded//",
            "/en/bounded/a%zzb/",
            "/en/bounded/a%4/",
            "/en/bounded/%/",
            "/en/bounded/%FF/",
            "/en/bounded/%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9/",
        ):
            with self.subTest(path=path_):
                self.assert404(path_, self.prefixed)

    def test_converter_rejection_is_a_404(self):
        # Rejected by to_python() raising ValueError.
        self.assert404("/en/bounded/forbidden/", self.prefixed)
        # A non-ValueError raised inside a converter must be swallowed into a
        # Resolver404 too; internal exceptions never leak.
        self.assert404("/en/exploding/abcd/", self.prefixed)
        # Rejected by the int converter.
        self.assert404("/en/shop/market/item/abc/", self.prefixed)

    def test_percent_encoded_digits_are_decoded_then_converted(self):
        # "%32%33" are encoded "2" and "3"; the decoded value converts to the
        # int 123 regardless of the active language/prefix.
        with override("en"):
            match = resolve("/zh/shop/market/item/1%32%33/", self.prefixed)
        self.assertEqual(match.kwargs, {"pk": 123})
        self.assertIsInstance(match.kwargs["pk"], int)
        with override("zh"):
            other = resolve("/en/shop/market/item/1%32%33/", self.prefixed)
        self.assertEqual(list(other.kwargs), list(match.kwargs))
        self.assertEqual(other.kwargs, match.kwargs)

    def test_truncated_or_overlong_utf8_is_a_404(self):
        for path_ in (
            "/en/shop/market/item/%E4%B8/",
            "/zh/shop/market/item/%E4/",
            "/en/bounded/%80/",
            "/en/bounded/%C0%AF/",
        ):
            with self.subTest(path=path_):
                self.assert404(path_, self.prefixed)

    # -- malformed paths ---------------------------------------------------

    def test_missing_extra_and_duplicate_segments_are_404(self):
        for path_ in (
            "/en/shop/market/item//",
            "/en/shop/market/item/1/extra/",
            "//simple/",
            "/en//simple/",
            "/en/shop//market/item/1/",
            "/",
        ):
            with self.subTest(path=path_):
                self.assert404(path_, self.prefixed)

    def test_query_and_fragment_tails_are_part_of_the_path(self):
        # resolve() resolves paths, not full URLs: a "?" or "#" tail has no
        # special meaning and cannot make "/en/simple/" match.
        self.assert404("/en/simple/?x=1", self.prefixed)
        self.assert404("/en/simple/#frag", self.prefixed)

    # -- independence between calls ---------------------------------------

    def test_failure_leaves_no_state_for_the_next_call(self):
        good = "/en/shop/market/item/9/"
        baseline = resolve(good, self.prefixed)
        for bad in (
            "/fr/simple/",
            "/en/bounded/a%2Fb/",
            "/en/shop/market/item/abc/",
            "/xx/",
        ):
            with self.subTest(bad=bad):
                self.assert404(bad, self.prefixed)
                follow_up = resolve(good, self.prefixed)
                self.assertEqual(follow_up.func, baseline.func)
                self.assertEqual(follow_up.kwargs, baseline.kwargs)
                self.assertEqual(follow_up.namespaces, baseline.namespaces)
                self.assertEqual(follow_up.route, baseline.route)

    def test_toggle_change_leaves_no_state(self):
        good = "/en/shop/market/item/9/"
        baseline = resolve(good, self.prefixed)
        # The same path under the other URLconf fails.
        self.assert404(good, self.unprefixed)
        # ... and the next call is decided solely by its own arguments.
        follow_up = resolve(good, self.prefixed)
        self.assertEqual(follow_up.kwargs, baseline.kwargs)
        unprefixed = resolve("/shop/market/item/9/", self.unprefixed)
        self.assertIs(unprefixed.func, views.item_view)
        self.assertEqual(unprefixed.kwargs, {"pk": 9})

    def test_active_language_change_leaves_no_state(self):
        with override("zh"):
            zh_match = resolve("/zh/simple/", self.prefixed)
        with override("en"):
            en_match = resolve("/en/simple/", self.prefixed)
        with override("zh"):
            zh_again = resolve("/zh/simple/", self.prefixed)
        self.assertEqual(zh_match.kwargs, zh_again.kwargs)
        self.assertEqual(zh_match.func, zh_again.func)
        self.assertEqual(
            [type(v) for v in zh_match.kwargs.values()],
            [type(v) for v in en_match.kwargs.values()],
        )

    def test_call_order_does_not_change_results(self):
        def signature(path_):
            match = resolve(path_, self.prefixed)
            return match.func, match.kwargs, tuple(match.namespaces)

        forward = [
            signature("/en/simple/"),
            signature("/zh/bounded/%E4%B8%AD/"),
            signature("/en/legacy/55/"),
        ]
        reverse = [
            signature("/en/legacy/55/"),
            signature("/zh/bounded/%E4%B8%AD/"),
            signature("/en/simple/"),
        ]
        self.assertEqual(forward, list(reversed(reverse)))

    def test_repeated_resolution_is_identical(self):
        first = resolve("/en/shop/market/item/3/", self.prefixed)
        second = resolve("/en/shop/market/item/3/", self.prefixed)
        self.assertEqual(first.func, second.func)
        self.assertEqual(first.args, second.args)
        self.assertEqual(first.kwargs, second.kwargs)
        self.assertEqual(first.url_name, second.url_name)
        self.assertEqual(first.app_names, second.app_names)
        self.assertEqual(first.namespaces, second.namespaces)
        self.assertEqual(first.route, second.route)

    def test_hashable_urlconf_reuses_one_resolver_without_residue(self):
        # A tuple of patterns is hashable, so the same cached URLResolver
        # instance serves every call and would expose any state left behind
        # by a previous resolution.
        urlconf = tuple(self.prefixed)
        self.assertIs(get_resolver(urlconf), get_resolver(urlconf))
        with override("en"):
            baseline = resolve("/en/shop/market/item/4/", urlconf)
        with override("zh"):
            resolve("/zh/shop/market/item/4/", urlconf)
        self.assert404("/fr/shop/market/item/4/", urlconf)
        with override("en"):
            again = resolve("/en/shop/market/item/4/", urlconf)
        self.assertEqual(again.func, baseline.func)
        self.assertEqual(again.kwargs, baseline.kwargs)
        self.assertEqual(again.namespaces, baseline.namespaces)
        self.assertEqual(again.route, baseline.route)

    # -- plain, non-internationalized routes ------------------------------

    def test_plain_routes_keep_view_name_and_trailing_slash(self):
        match = resolve("/plain/5/", urls.plain_urlconf)
        self.assertEqual(match.view_name, "plain")
        self.assertEqual(match.kwargs, {"pk": 5})
        with self.assertRaises(Resolver404):
            resolve("/plain/5", urls.plain_urlconf)
        with self.assertRaises(Resolver404):
            resolve("/plain/x/", urls.plain_urlconf)
        repeated = resolve("/plain/5/", urls.plain_urlconf)
        self.assertEqual(repeated.kwargs, match.kwargs)
        self.assertEqual(repeated.view_name, match.view_name)
