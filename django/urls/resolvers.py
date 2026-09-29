"""
This module converts requested URLs to callback view functions.

URLResolver is the main class here. Its resolve() method takes a URL (as
a string) and returns a ResolverMatch object which provides access to all
attributes of the resolved URL match.
"""

import functools
import inspect
import re
import string
import sys
import weakref
from importlib import import_module
from pickle import PicklingError
from urllib.parse import quote, unquote

from asgiref.local import Local

from django.conf import settings
from django.core.checks import Error, Warning
from django.core.checks.urls import check_resolver
from django.core.exceptions import ImproperlyConfigured
from django.utils.datastructures import MultiValueDict
from django.utils.functional import cached_property
from django.utils.http import RFC3986_SUBDELIMS, escape_leading_slashes
from django.utils.regex_helper import _lazy_re_compile, normalize
from django.utils.translation import (
    get_language,
    get_language_from_path,
    get_supported_language_variant,
)

from .converters import get_converters
from .exceptions import NoReverseMatch, Resolver404
from .utils import get_callable


class ResolverMatch:
    def __init__(
        self,
        func,
        args,
        kwargs,
        url_name=None,
        app_names=None,
        namespaces=None,
        route=None,
        tried=None,
        captured_kwargs=None,
        extra_kwargs=None,
    ):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.url_name = url_name
        self.route = route
        self.tried = tried
        self.captured_kwargs = captured_kwargs
        self.extra_kwargs = extra_kwargs

        # If a URLRegexResolver doesn't have a namespace or app_name, it passes
        # in an empty value.
        self.app_names = [x for x in app_names if x] if app_names else []
        self.app_name = ":".join(self.app_names)
        self.namespaces = [x for x in namespaces if x] if namespaces else []
        self.namespace = ":".join(self.namespaces)

        if hasattr(func, "view_class"):
            func = func.view_class
        if not hasattr(func, "__name__"):
            # A class-based view
            self._func_path = func.__class__.__module__ + "." + func.__class__.__name__
        else:
            # A function-based view
            self._func_path = func.__module__ + "." + func.__name__

        view_path = url_name or self._func_path
        self.view_name = ":".join([*self.namespaces, view_path])

    def __getitem__(self, index):
        return (self.func, self.args, self.kwargs)[index]

    def __repr__(self):
        if isinstance(self.func, functools.partial):
            func = repr(self.func)
        else:
            func = self._func_path
        return (
            "ResolverMatch(func=%s, args=%r, kwargs=%r, url_name=%r, "
            "app_names=%r, namespaces=%r, route=%r%s%s)"
            % (
                func,
                self.args,
                self.kwargs,
                self.url_name,
                self.app_names,
                self.namespaces,
                self.route,
                (
                    f", captured_kwargs={self.captured_kwargs!r}"
                    if self.captured_kwargs
                    else ""
                ),
                f", extra_kwargs={self.extra_kwargs!r}" if self.extra_kwargs else "",
            )
        )

    def __reduce_ex__(self, protocol):
        raise PicklingError(f"Cannot pickle {self.__class__.__qualname__}.")


def get_resolver(urlconf=None):
    if urlconf is None:
        urlconf = settings.ROOT_URLCONF
    return _get_cached_resolver(urlconf)


# One resolver per urlconf. The resolver is long-lived and version-aware: it
# reloads its patterns itself when the urlconf's ``urlpatterns`` is replaced
# (a *refresh*), so an explicit urlconf handed to resolve() observes the
# replacement without the caller clearing any cache, and a refresh onto an
# invalid table cannot destroy the resolver's previously working table.
_resolver_cache: dict = {}

# Materialized snapshot of each one-shot iterable exposed as a routing table
# (a generator), keyed by the iterable itself with a weak reference. A list
# or tuple is held directly and needs no snapshot; a generator is consumed
# exactly once, and a freshly constructed child resolver that ends up
# pointing at the same exposed generator (e.g. the parent table is rebuilt
# with include() while a nested module keeps exposing the same object) shares
# its snapshot instead of iterating the exhausted generator a second time.
# The weak key lets a discarded iterable -- and its snapshot -- be reclaimed.
_oneshot_patterns_cache = weakref.WeakKeyDictionary()


def _get_cached_resolver(urlconf=None):
    try:
        hash(urlconf)
    except TypeError:
        # An unhashable urlconf (a plain list/tuple of patterns handed in by
        # the caller) is ephemeral: it cannot be used as a cache key and must
        # not be shared with a later call that happens to pass an equal
        # object. Build a dedicated resolver instead of caching it.
        return URLResolver(RegexPattern(r"^/"), urlconf)
    try:
        return _resolver_cache[urlconf]
    except KeyError:
        resolver = URLResolver(RegexPattern(r"^/"), urlconf)
        _resolver_cache[urlconf] = resolver
        return resolver


@functools.cache
def get_ns_resolver(ns_pattern, resolver, converters, patterns_version):
    # Build a namespaced resolver for the given parent URLconf pattern.
    # This makes it possible to have captured parameters in the parent
    # URLconf pattern. patterns_version identifies the parent's currently
    # loaded routing table: a refresh replaces the table and the cached
    # resolver built from the old one is simply not reused.
    pattern = RegexPattern(ns_pattern)
    pattern.converters = dict(converters)
    url_patterns = resolver.url_patterns
    ns_resolver = URLResolver(pattern, url_patterns)
    return URLResolver(RegexPattern(r"^/"), [ns_resolver])


class LocaleRegexDescriptor:
    def __get__(self, instance, cls=None):
        """
        Return a compiled regular expression based on the active language.
        """
        if instance is None:
            return self
        # As a performance optimization, if the given regex string is a regular
        # string (not a lazily-translated string proxy), compile it once and
        # avoid per-language compilation.
        pattern = instance._regex
        if isinstance(pattern, str):
            instance.__dict__["regex"] = self._compile(pattern)
            return instance.__dict__["regex"]
        language_code = get_language()
        if language_code not in instance._regex_dict:
            instance._regex_dict[language_code] = self._compile(str(pattern))
        return instance._regex_dict[language_code]

    def _compile(self, regex):
        try:
            return re.compile(regex)
        except re.error as e:
            raise ImproperlyConfigured(
                f'"{regex}" is not a valid regular expression: {e}'
            ) from e


class CheckURLMixin:
    def describe(self):
        """
        Format the URL pattern for display in warning messages.
        """
        description = "'{}'".format(self)
        if self.name:
            description += " [name='{}']".format(self.name)
        return description

    def _check_pattern_startswith_slash(self):
        """
        Check that the pattern does not begin with a forward slash.
        """
        if not settings.APPEND_SLASH:
            # Skip check as it can be useful to start a URL pattern with a
            # slash when APPEND_SLASH=False.
            return []
        if self._regex.startswith(("/", "^/", "^\\/")) and not self._regex.endswith(
            "/"
        ):
            warning = Warning(
                "Your URL pattern {} has a route beginning with a '/'. Remove this "
                "slash as it is unnecessary. If this pattern is targeted in an "
                "include(), ensure the include() pattern has a trailing '/'.".format(
                    self.describe()
                ),
                id="urls.W002",
            )
            return [warning]
        else:
            return []


class RegexPattern(CheckURLMixin):
    regex = LocaleRegexDescriptor()

    def __init__(self, regex, name=None, is_endpoint=False):
        self._regex = regex
        self._regex_dict = {}
        self._is_endpoint = is_endpoint
        self.name = name
        self.converters = {}

    def match(self, path):
        match = (
            self.regex.fullmatch(path)
            if self._is_endpoint and self.regex.pattern.endswith("$")
            else self.regex.search(path)
        )
        if match:
            # If there are any named groups, use those as kwargs, ignoring
            # non-named groups. Otherwise, pass all non-named arguments as
            # positional arguments.
            kwargs = match.groupdict()
            args = () if kwargs else match.groups()
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            return path[match.end() :], args, kwargs
        return None

    def check(self):
        warnings = []
        warnings.extend(self._check_pattern_startswith_slash())
        if not self._is_endpoint:
            warnings.extend(self._check_include_trailing_dollar())
        return warnings

    def _check_include_trailing_dollar(self):
        if self._regex.endswith("$") and not self._regex.endswith(r"\$"):
            return [
                Warning(
                    "Your URL pattern {} uses include with a route ending with a '$'. "
                    "Remove the dollar from the route to avoid problems including "
                    "URLs.".format(self.describe()),
                    id="urls.W001",
                )
            ]
        else:
            return []

    def __str__(self):
        return str(self._regex)


_PATH_PARAMETER_COMPONENT_RE = _lazy_re_compile(
    r"<(?:(?P<converter>[^>:]+):)?(?P<parameter>[^>]+)>"
)

whitespace_set = frozenset(string.whitespace)

@functools.lru_cache
def _compiled_converter_regex(regex):
    return re.compile(regex)


# A well-formed percent-encoded octet, e.g. "%2F" or "%c3".
_PERCENT_ENCODED_RE = r"%[0-9A-Fa-f]{2}"
# A literal '%' that does not begin a well-formed escape. It is disjoint
# from _PERCENT_ENCODED_RE, so the two alternatives can never compete for
# the same input (which would make matching exponentially ambiguous).
_LONE_PERCENT_RE = r"%(?![0-9A-Fa-f]{2})"


def _match_capture(parameter, converter_regex):
    # The raw-path counterpart of a converter's capture. Matching runs
    # against the still-encoded path so that an encoded slash ("%2F")
    # cannot introduce a new path level. The pieces are pairwise disjoint
    # and fixed-width, so an input string has a single possible split and
    # matching stays linear:
    #   * a well-formed percent escape is consumed as one 3-character token
    #     and decoded later (an encoded slash can therefore never open a new
    #     path level),
    #   * a lone, non-escape '%' is only accepted when the converter itself
    #     accepts '%' (the path may already have been decoded once by WSGI),
    #   * everything else is matched by the converter's own regex, whose
    #     greediness still decides where the capture ends relative to route
    #     literals (so <int:pk>-<slug:slug> cannot let '-' bleed into pk).
    # The decoded capture is re-validated against the converter regex before
    # conversion, so broadening it with escapes cannot let an invalid value
    # through.
    escape = _PERCENT_ENCODED_RE
    if "%" not in converter_regex and _compiled_converter_regex(
        converter_regex
    ).fullmatch("%") is None:
        # The converter never involves '%' (built-in int/slug/uuid, most
        # custom converters). Converter runs and escape tokens are then
        # disjoint and fixed-width: a run never consumes a '%', so at every
        # '%' position the escape token is the single possible move. This
        # keeps the converter's own greediness (so "<int:pk>-<slug:slug>"
        # cannot let '-' bleed into pk) and matching linear.
        run = f"(?:{converter_regex})?"
        token = f"{run}(?:{escape}{run})*"
    else:
        # The converter accepts '%' itself (<str>, <path> or a custom
        # converter whose regex mentions '%'). Raw runs are single
        # characters taken from everything except '%' (and, when the
        # converter rejects it, '/'); '%' only reaches them through an
        # escape or the disjoint lone-percent token. Splitting into
        # fixed-width single-character tokens prevents an exponentially
        # ambiguous split when the converter regex would itself match an
        # escape sequence. Newlines are excluded, mirroring a converter
        # regex that uses '.'. The post-match fullmatch against the
        # converter regex keeps its value restrictions.
        allows_slash = (
            _compiled_converter_regex(converter_regex).fullmatch("/") is not None
        )
        raw_char = r"[^%\n]" if allows_slash else r"[^%/\n]"
        run = f"(?:{raw_char}|{_LONE_PERCENT_RE})*"
        token = f"{run}(?:{escape}{run})*"
    return f"(?P<{parameter}>{token})"


def _decode_route_capture(value):
    """
    Percent-decode a captured route component exactly once.

    The resolver matches the raw path, so every captured component is
    decoded here before being handed to its converter. Decoding follows
    urllib's lenient rules: every well-formed percent-encoded octet is
    decoded once (so "%252F" becomes the literal text "%2F" rather than
    "/"), while a '%' that does not introduce a well-formed escape is left
    untouched because the path may already have been decoded once by WSGI.
    The decoded bytes must be valid UTF-8; otherwise ValueError is raised so
    the caller treats the route as non-matching rather than returning
    replacement characters.
    """
    if "%" not in value:
        return value
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError as e:
        raise ValueError("Route component is not valid UTF-8") from e


@functools.lru_cache
def _route_to_regex(route, is_endpoint):
    """
    Convert a path pattern into a regular expression. Return the regular
    expression used for reversing, the regular expression used for matching
    raw (still percent-encoded) paths, and a dictionary mapping the capture
    names to the converters. For example, 'foo/<int:pk>' returns
    '^foo\\/(?P<pk>[0-9]+)', a match regex that also accepts percent-encoded
    octets in the capture, and {'pk': <django.urls.converters.IntConverter>}.
    """
    parts = ["^"]
    match_parts = ["^"]
    all_converters = get_converters()
    converters = {}
    previous_end = 0
    for match_ in _PATH_PARAMETER_COMPONENT_RE.finditer(route):
        if not whitespace_set.isdisjoint(match_[0]):
            raise ImproperlyConfigured(
                f"URL route {route!r} cannot contain whitespace in angle brackets <…>."
            )
        # Default to make converter "str" if unspecified (parameter always
        # matches something).
        raw_converter, parameter = match_.groups(default="str")
        if not parameter.isidentifier():
            raise ImproperlyConfigured(
                f"URL route {route!r} uses parameter name {parameter!r} which "
                "isn't a valid Python identifier."
            )
        try:
            converter = all_converters[raw_converter]
        except KeyError as e:
            raise ImproperlyConfigured(
                f"URL route {route!r} uses invalid converter {raw_converter!r}."
            ) from e
        converters[parameter] = converter

        start, end = match_.span()
        literal = re.escape(route[previous_end:start])
        previous_end = end
        parts.append(literal)
        match_parts.append(literal)
        parts.append(f"(?P<{parameter}>{converter.regex})")
        match_parts.append(_match_capture(parameter, converter.regex))

    tail = re.escape(route[previous_end:])
    parts.append(tail)
    match_parts.append(tail)
    if is_endpoint:
        parts.append(r"\Z")
        match_parts.append(r"\Z")
    return "".join(parts), "".join(match_parts), converters


class LocaleRegexRouteDescriptor:
    def __get__(self, instance, cls=None):
        """
        Return a compiled regular expression based on the active language.
        """
        if instance is None:
            return self
        # As a performance optimization, if the given route is a regular string
        # (not a lazily-translated string proxy), compile it once and avoid
        # per-language compilation.
        if isinstance(instance._route, str):
            instance.__dict__["regex"] = re.compile(instance._regex)
            return instance.__dict__["regex"]
        language_code = get_language()
        if language_code not in instance._regex_dict:
            instance._regex_dict[language_code] = re.compile(
                _route_to_regex(str(instance._route), instance._is_endpoint)[0]
            )
        return instance._regex_dict[language_code]


class LocaleRouteMatchRegexDescriptor:
    def __get__(self, instance, cls=None):
        """
        Return the compiled regular expression used to match raw (still
        percent-encoded) paths, based on the active language.
        """
        if instance is None:
            return self
        if isinstance(instance._route, str):
            instance.__dict__["match_regex"] = re.compile(instance._match_regex)
            return instance.__dict__["match_regex"]
        language_code = get_language()
        if language_code not in instance._match_regex_dict:
            instance._match_regex_dict[language_code] = re.compile(
                _route_to_regex(str(instance._route), instance._is_endpoint)[1]
            )
        return instance._match_regex_dict[language_code]


class RoutePattern(CheckURLMixin):
    regex = LocaleRegexRouteDescriptor()
    match_regex = LocaleRouteMatchRegexDescriptor()

    def __init__(self, route, name=None, is_endpoint=False):
        self._route = route
        self._regex, self._match_regex, self.converters = _route_to_regex(
            str(route), is_endpoint
        )
        self._regex_dict = {}
        self._match_regex_dict = {}
        self._is_endpoint = is_endpoint
        self.name = name

    def match(self, path):
        # Only use regex overhead if there are converters.
        if self.converters:
            if match := self.match_regex.search(path):
                # RoutePattern doesn't allow non-named groups so args are
                # ignored.
                kwargs = match.groupdict()
                match_end = match.end()
                for key, value in kwargs.items():
                    converter = self.converters[key]
                    try:
                        # A literal '?' or '#' introduces the query string or
                        # the fragment. Such a character reaches PATH_INFO
                        # only as decoded data: an encoded '%3F'/'%23' arrives
                        # as '?'/'#' while a genuine query or fragment is
                        # stripped before the resolver, so it can only appear
                        # at the *end* of the path. A capture that terminates
                        # the whole match (nothing of the path remains and no
                        # route literal follows it) -- notably a greedy
                        # <path>/<str> endpoint -- would otherwise swallow a
                        # trailing query or fragment as parameter data and
                        # silently ignore the suffix; reject the match
                        # instead. A capture followed by more route text (an
                        # include() prefix or an endpoint literal) can still
                        # hold a decoded '?'/'#' as ordinary data. The test
                        # runs on the raw capture, so an encoded
                        # '%3F'/'%23' is never affected.
                        if (
                            value is not None
                            and match_end == len(path)
                            and match.end(key) == match_end
                            and ("?" in value or "#" in value)
                        ):
                            return None
                        # Matching runs against the raw, still-encoded path
                        # so an encoded slash cannot split a capture into
                        # path levels. Decode the capture exactly once and
                        # let the converter validate the decoded value; a
                        # bad UTF-8 sequence or a value the converter
                        # rejects makes the whole pattern non-matching.
                        decoded = _decode_route_capture(value)
                        # The match regex admits percent escapes in addition
                        # to the converter's own characters; re-check the
                        # decoded value against the converter's regex so an
                        # escape cannot smuggle in a disallowed character
                        # (an encoded slash into a <slug>, a space into an
                        # <int>, and so on).
                        if not _compiled_converter_regex(converter.regex).fullmatch(
                            decoded
                        ):
                            return None
                        converted = converter.to_python(decoded)
                        # A converter signals "no value" by raising; returning
                        # None is not a valid captured parameter (an empty
                        # capture is already rejected by the regex above), so
                        # treat it as a non-match instead of handing None to
                        # the view or letting it masquerade as a real kwarg.
                        if converted is None:
                            return None
                        kwargs[key] = converted
                    except Exception:
                        return None
                return path[match.end() :], (), kwargs
        # If this is an endpoint, the path should be exactly the same as the
        # route.
        elif self._is_endpoint:
            if self._route == path:
                return "", (), {}
        # If this isn't an endpoint, the path should start with the route.
        elif path.startswith(route := str(self._route)):
            return path.removeprefix(route), (), {}
        return None

    def check(self):
        warnings = [
            *self._check_pattern_startswith_slash(),
            *self._check_pattern_unmatched_angle_brackets(),
        ]
        route = self._route
        if "(?P<" in route or route.startswith("^") or route.endswith("$"):
            warnings.append(
                Warning(
                    "Your URL pattern {} has a route that contains '(?P<', begins "
                    "with a '^', or ends with a '$'. This was likely an oversight "
                    "when migrating to django.urls.path().".format(self.describe()),
                    id="2_0.W001",
                )
            )
        return warnings

    def _check_pattern_unmatched_angle_brackets(self):
        warnings = []
        msg = "Your URL pattern %s has an unmatched '%s' bracket."
        brackets = re.findall(r"[<>]", str(self._route))
        open_bracket_counter = 0
        for bracket in brackets:
            if bracket == "<":
                open_bracket_counter += 1
            elif bracket == ">":
                open_bracket_counter -= 1
                if open_bracket_counter < 0:
                    warnings.append(
                        Warning(msg % (self.describe(), ">"), id="urls.W010")
                    )
                    open_bracket_counter = 0
        if open_bracket_counter > 0:
            warnings.append(Warning(msg % (self.describe(), "<"), id="urls.W010"))
        return warnings

    def __str__(self):
        return str(self._route)


class LocalePrefixPattern:
    def __init__(self, prefix_default_language=True):
        self.prefix_default_language = prefix_default_language
        self.converters = {}

    @property
    def regex(self):
        # This is only used by reverse() and cached in _reverse_dict.
        return re.compile(re.escape(self.language_prefix))

    @property
    def language_prefix(self):
        language_code = get_language() or settings.LANGUAGE_CODE
        if language_code == settings.LANGUAGE_CODE and not self.prefix_default_language:
            return ""
        else:
            return "%s/" % language_code

    def match(self, path):
        # Resolution is driven by the requested path, not by the active
        # language: a path carrying a supported language prefix must resolve
        # identically no matter which language is active when resolve() is
        # called, and an unprefixed path must reach the default language
        # mount regardless of it. get_language_from_path() validates the
        # segment with the language-code regular expression, so a path such
        # as "de-simple-page-test/" is never mistaken for a "de" prefix by
        # variant fallback.
        prefix, sep, rest = path.partition("/")
        language = get_language_from_path("/" + path) if sep else None
        if language is not None:
            if not self.prefix_default_language:
                # Compare against the supported variant of the default
                # language so a regional default ("en-us" supported through
                # "en") is recognized under either spelling; the default
                # language is mounted without a prefix, so a prefixed
                # request for it keeps failing here and cannot fall through
                # to an unprefixed sibling.
                try:
                    default_language = get_supported_language_variant(
                        settings.LANGUAGE_CODE
                    ).lower()
                except LookupError:
                    default_language = settings.LANGUAGE_CODE.lower()
                if language.lower() == default_language:
                    return None
            return rest, (), {}
        if not self.prefix_default_language:
            # No language segment: this is the unprefixed default language
            # mount even when another language happens to be active.
            return path, (), {}
        return None

    def check(self):
        return []

    def describe(self):
        return "'{}'".format(self)

    def __str__(self):
        return self.language_prefix


class URLPattern:
    def __init__(self, pattern, callback, default_args=None, name=None):
        self.pattern = pattern
        self.callback = callback  # the view
        self.default_args = default_args or {}
        self.name = name

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self.pattern.describe())

    def check(self):
        warnings = self._check_pattern_name()
        warnings.extend(self.pattern.check())
        warnings.extend(self._check_callback())
        return warnings

    def _check_pattern_name(self):
        """
        Check that the pattern name does not contain a colon.
        """
        if self.pattern.name is not None and ":" in self.pattern.name:
            warning = Warning(
                "Your URL pattern {} has a name including a ':'. Remove the colon, to "
                "avoid ambiguous namespace references.".format(self.pattern.describe()),
                id="urls.W003",
            )
            return [warning]
        else:
            return []

    def _check_callback(self):
        from django.views import View

        view = self.callback
        if inspect.isclass(view) and issubclass(view, View):
            return [
                Error(
                    "Your URL pattern %s has an invalid view, pass %s.as_view() "
                    "instead of %s."
                    % (
                        self.pattern.describe(),
                        view.__name__,
                        view.__name__,
                    ),
                    id="urls.E009",
                )
            ]
        return []

    def resolve(self, path):
        match = self.pattern.match(path)
        if match:
            new_path, args, captured_kwargs = match
            # Pass any default args as **kwargs.
            kwargs = {**captured_kwargs, **self.default_args}
            return ResolverMatch(
                self.callback,
                args,
                kwargs,
                self.pattern.name,
                route=str(self.pattern),
                captured_kwargs=captured_kwargs,
                extra_kwargs=self.default_args,
            )

    @cached_property
    def lookup_str(self):
        """
        A string that identifies the view (e.g. 'path.to.view_function' or
        'path.to.ClassBasedView').
        """
        callback = self.callback
        if isinstance(callback, functools.partial):
            callback = callback.func
        if hasattr(callback, "view_class"):
            callback = callback.view_class
        elif not hasattr(callback, "__name__"):
            return callback.__module__ + "." + callback.__class__.__name__
        return callback.__module__ + "." + callback.__qualname__


class URLResolver:
    def __init__(
        self, pattern, urlconf_name, default_kwargs=None, app_name=None, namespace=None
    ):
        self.pattern = pattern
        # urlconf_name is the dotted Python path to the module defining
        # urlpatterns. It may also be an object with an urlpatterns attribute
        # or urlpatterns itself.
        self.urlconf_name = urlconf_name
        self.callback = None
        self.default_kwargs = default_kwargs or {}
        self.namespace = namespace
        self.app_name = app_name
        self._reverse_dict = {}
        self._namespace_dict = {}
        self._app_dict = {}
        # set of dotted paths to all functions and classes that are used in
        # urlpatterns
        self._callback_strs = set()
        self._populated = False
        self._local = Local()
        # The routing table currently loaded. ``_loaded_patterns_ref`` is the
        # exact object the urlconf exposes as ``urlpatterns`` (kept for an
        # identity comparison: because it is held, its id can never be reused
        # by a replacement), ``_loaded_url_patterns`` is the materialized
        # sequence walked by resolve(), and ``_loaded_patterns_signature``
        # holds the entries it was loaded with in order. A reusable list is
        # held by reference, so its identity survives an in-place edit; the
        # signature detects that edit (an in-place replacement, append, pop or
        # reorder), which is a refresh like any other.
        # _patterns_generation advances on every committed refresh and
        # versions derived caches. See _reload_patterns().
        self._loaded = False
        self._loaded_urlconf_module = None
        self._loaded_patterns_ref = None
        self._loaded_url_patterns = None
        self._loaded_patterns_signature = None
        self._patterns_generation = 0

    def __repr__(self):
        if isinstance(self.urlconf_name, list) and self.urlconf_name:
            # Don't bother to output the whole list, it can be huge
            urlconf_repr = "<%s list>" % self.urlconf_name[0].__class__.__name__
        else:
            urlconf_repr = repr(self.urlconf_name)
        return "<%s %s (%s:%s) %s>" % (
            self.__class__.__name__,
            urlconf_repr,
            self.app_name,
            self.namespace,
            self.pattern.describe(),
        )

    def check(self):
        messages = []
        for pattern in self.url_patterns:
            messages.extend(check_resolver(pattern))
        return messages or self.pattern.check()

    def _populate(self):
        # Short-circuit if called recursively in this thread to prevent
        # infinite recursion. Concurrent threads may call this at the same
        # time and will need to continue, so set 'populating' on a
        # thread-local variable.
        if getattr(self._local, "populating", False):
            return
        try:
            self._local.populating = True
            lookups = MultiValueDict()
            namespaces = {}
            apps = {}
            language_code = get_language()
            for url_pattern in reversed(self.url_patterns):
                p_pattern = url_pattern.pattern.regex.pattern
                p_pattern = p_pattern.removeprefix("^")
                if isinstance(url_pattern, URLPattern):
                    self._callback_strs.add(url_pattern.lookup_str)
                    bits = normalize(url_pattern.pattern.regex.pattern)
                    lookups.appendlist(
                        url_pattern.callback,
                        (
                            bits,
                            p_pattern,
                            url_pattern.default_args,
                            url_pattern.pattern.converters,
                        ),
                    )
                    if url_pattern.name is not None:
                        lookups.appendlist(
                            url_pattern.name,
                            (
                                bits,
                                p_pattern,
                                url_pattern.default_args,
                                url_pattern.pattern.converters,
                            ),
                        )
                else:  # url_pattern is a URLResolver.
                    url_pattern._populate()
                    if url_pattern.app_name:
                        apps.setdefault(url_pattern.app_name, []).append(
                            url_pattern.namespace
                        )
                        namespaces[url_pattern.namespace] = (
                            p_pattern,
                            url_pattern,
                            # Keep the include()'s own extra kwargs so a
                            # reverse of a nested name can fill a parameter
                            # captured on this include's route from its
                            # default.
                            url_pattern.default_kwargs,
                        )
                    else:
                        for name in url_pattern.reverse_dict:
                            for (
                                matches,
                                pat,
                                defaults,
                                converters,
                            ) in url_pattern.reverse_dict.getlist(name):
                                new_matches = normalize(p_pattern + pat)
                                lookups.appendlist(
                                    name,
                                    (
                                        new_matches,
                                        p_pattern + pat,
                                        {
                                            **url_pattern.default_kwargs,
                                            **defaults,
                                        },
                                        {
                                            **self.pattern.converters,
                                            **url_pattern.pattern.converters,
                                            **converters,
                                        },
                                    ),
                                )
                        for namespace, (
                            prefix,
                            sub_pattern,
                            sub_defaults,
                        ) in url_pattern.namespace_dict.items():
                            current_converters = url_pattern.pattern.converters
                            sub_pattern.pattern.converters.update(current_converters)
                            # Accumulate the extra kwargs of every include()
                            # flattened away here; the nearer the include, the
                            # higher the precedence, mirroring the order in
                            # which resolve() merges them.
                            namespaces[namespace] = (
                                p_pattern + prefix,
                                sub_pattern,
                                {
                                    **url_pattern.default_kwargs,
                                    **sub_defaults,
                                },
                            )
                        for app_name, namespace_list in url_pattern.app_dict.items():
                            apps.setdefault(app_name, []).extend(namespace_list)
                    self._callback_strs.update(url_pattern._callback_strs)
            self._namespace_dict[language_code] = namespaces
            self._app_dict[language_code] = apps
            self._reverse_dict[language_code] = lookups
            self._populated = True
        finally:
            self._local.populating = False

    @property
    def reverse_dict(self):
        # Pick up a replaced urlpatterns before reading a cache derived from
        # the patterns; a refresh clears these lookups (see
        # _reload_patterns()).
        self._reload_patterns()
        language_code = get_language()
        if language_code not in self._reverse_dict:
            self._populate()
        return self._reverse_dict[language_code]

    @property
    def namespace_dict(self):
        self._reload_patterns()
        language_code = get_language()
        if language_code not in self._namespace_dict:
            self._populate()
        return self._namespace_dict[language_code]

    @property
    def app_dict(self):
        self._reload_patterns()
        language_code = get_language()
        if language_code not in self._app_dict:
            self._populate()
        return self._app_dict[language_code]

    @staticmethod
    def _extend_tried(tried, pattern, sub_tried=None):
        if sub_tried is None:
            tried.append([pattern])
        else:
            tried.extend([pattern, *t] for t in sub_tried)

    @staticmethod
    def _join_route(route1, route2):
        """Join two routes, without the starting ^ in the second route."""
        if not route1:
            return route2
        route2 = route2.removeprefix("^")
        return route1 + route2

    @staticmethod
    def _resolver_route(pattern, path):
        """
        The literal route a resolver pattern consumed from *path*.

        A plain include's route is language-independent. A
        LocalePrefixPattern renders the *active* language, which need not be
        the prefix the requested path actually carried; reconstruct the
        matched prefix from the path so ResolverMatch.route is identical no
        matter which language is active when resolve() runs.
        """
        if isinstance(pattern.pattern, LocalePrefixPattern):
            if get_language_from_path("/" + path) is not None:
                return path.partition("/")[0] + "/"
            return ""
        return str(pattern.pattern)

    def _is_callback(self, name):
        if not self._populated:
            self._populate()
        return name in self._callback_strs

    def resolve(self, path):
        path = str(path)  # path may be a reverse_lazy object
        tried = []
        match = self.pattern.match(path)
        if match:
            new_path, args, kwargs = match
            for pattern in self.url_patterns:
                try:
                    sub_match = pattern.resolve(new_path)
                except Resolver404 as e:
                    self._extend_tried(tried, pattern, e.args[0].get("tried"))
                else:
                    if sub_match:
                        # Merge captured arguments in match with submatch
                        sub_match_dict = {**kwargs, **self.default_kwargs}
                        # Update the sub_match_dict with the kwargs from the
                        # sub_match.
                        sub_match_dict.update(sub_match.kwargs)
                        # If there are *any* named groups, ignore all non-named
                        # groups. Otherwise, pass all non-named arguments as
                        # positional arguments.
                        sub_match_args = sub_match.args
                        if not sub_match_dict:
                            sub_match_args = args + sub_match.args
                        current_route = (
                            ""
                            if isinstance(pattern, URLPattern)
                            else self._resolver_route(pattern, new_path)
                        )
                        self._extend_tried(tried, pattern, sub_match.tried)
                        return ResolverMatch(
                            sub_match.func,
                            sub_match_args,
                            sub_match_dict,
                            sub_match.url_name,
                            [self.app_name, *sub_match.app_names],
                            [self.namespace, *sub_match.namespaces],
                            self._join_route(current_route, sub_match.route),
                            tried,
                            captured_kwargs=sub_match.captured_kwargs,
                            extra_kwargs={
                                **self.default_kwargs,
                                **sub_match.extra_kwargs,
                            },
                        )
                    tried.append([pattern])
            raise Resolver404({"tried": tried, "path": new_path})
        raise Resolver404({"path": path})

    def _current_patterns(self):
        """
        Return ``(urlconf_module, patterns_ref)`` for the routing table the
        urlconf currently exposes. *patterns_ref* is the raw (not yet
        materialized) object found under ``urlpatterns`` -- or the urlconf
        itself when it is the pattern container.
        """
        if isinstance(self.urlconf_name, str):
            urlconf_module = import_module(self.urlconf_name)
        else:
            urlconf_module = self.urlconf_name
        return urlconf_module, getattr(
            urlconf_module, "urlpatterns", urlconf_module
        )

    def _exposed_patterns_ref(self):
        """
        Like the second item of _current_patterns(), but never imports:
        return None for a dotted path whose module is not loaded yet.
        """
        if isinstance(self.urlconf_name, str):
            module = sys.modules.get(self.urlconf_name)
            if module is None:
                return None
            return getattr(module, "urlpatterns", module)
        return getattr(self.urlconf_name, "urlpatterns", self.urlconf_name)

    @staticmethod
    def _materialize_patterns(patterns_ref, urlconf_name):
        """
        Turn the object exposed as ``urlpatterns`` into a walkable sequence.

        A reusable sequence (a list or tuple, the normal case) is returned as
        is so in-place edits stay visible; a one-shot iterable (a generator)
        is materialized into a list exactly once. The snapshot is shared by
        identity of the exposed iterable, so a freshly constructed child
        resolver that ends up pointing at the same already-consumed object
        (its parent table was rebuilt with include() while the nested module
        kept exposing it) reuses the snapshot instead of iterating it a second
        time and seeing an empty table.
        """
        if isinstance(patterns_ref, (list, tuple)):
            return patterns_ref
        try:
            snapshot = _oneshot_patterns_cache.get(patterns_ref)
        except TypeError:
            snapshot = None
        if snapshot is not None:
            return snapshot
        try:
            snapshot = list(patterns_ref)
        except TypeError as e:
            msg = (
                "The included URLconf '{name}' does not appear to have any "
                "patterns in it. If you see the 'urlpatterns' variable with "
                "valid patterns in the file then the issue is probably caused "
                "by a circular import."
            )
            raise ImproperlyConfigured(msg.format(name=urlconf_name)) from e
        try:
            _oneshot_patterns_cache[patterns_ref] = snapshot
        except TypeError:
            # An unhashable or non-weak-referenceable one-shot iterable can
            # only be snapshotted for this resolver; it cannot be shared with
            # a resolver constructed later.
            pass
        return snapshot

    @staticmethod
    def _patterns_signature(patterns):
        """
        Content fingerprint of a materialized routing table: a tuple holding
        (by identity) the entries in their loaded order.

        A reusable sequence (a list or tuple) is held by reference, so an
        in-place edit (``patterns[:] = ...``, append(), pop(), item
        assignment) keeps the same object and cannot be told from an
        unchanged table by identity alone. Comparing the live entries against
        the ones held here with ``is`` changes exactly when an entry is added,
        removed, replaced or reordered -- even by an equal-but-distinct
        object, which is still an observable edit. Holding the entries also
        keeps their addresses live, so the comparison cannot be fooled by an
        id being reused after a removed entry was garbage collected.

        It is only ever computed for an already materialized sequence, so a
        one-shot iterable (a generator), whose content cannot change once
        consumed, is never walked again.
        """
        return tuple(patterns)

    def _build_candidate(self, urlconf_module, patterns_ref):
        """
        Build and validate -- without installing -- the routing table this
        resolver would serve if it adopted *patterns_ref*.

        Returns ``(patterns, signature, children)`` where *patterns* is the
        materialized candidate table, *signature* is its content fingerprint
        (see _patterns_signature()) and *children* lists, for every include()
        in it whose nested table must be (re)installed,
        ``(resolver, urlconf_module, patterns_ref, patterns, signature,
        children)``.

        Nested tables are read through the candidate child URLResolver
        objects -- the very objects resolve() later walks -- rather than
        re-importing them, so building never disturbs a resolver reachable
        from another (still live) table. A child that already serves the
        candidate table -- by both identity and content fingerprint, so an
        in-place edit to a nested list is not missed -- reuses its installed
        snapshot, so a materialized one-shot iterable is never iterated a
        second time and an unchanged child is left alone. Every entry must be
        a pattern with a compilable regex; any failure raises
        ImproperlyConfigured before anything is installed.
        """
        patterns = self._materialize_patterns(patterns_ref, self.urlconf_name)
        signature = self._patterns_signature(patterns)
        children = []
        for url_pattern in patterns:
            if not isinstance(url_pattern, (URLPattern, URLResolver)):
                raise ImproperlyConfigured(
                    "The URLconf %r contains an entry that is not a valid URL "
                    "pattern: %r" % (self.urlconf_name, url_pattern)
                )
            # Accessing regex compiles it, so an invalid regular expression
            # fails the refresh rather than the first request reaching it.
            _ = url_pattern.pattern.regex
            if isinstance(url_pattern, URLResolver):
                nested_module, nested_ref = url_pattern._current_patterns()
                if url_pattern._matches_loaded_table(nested_ref):
                    # The candidate child already serves this table (a held
                    # list/tuple by identity and, for a mutable list, with
                    # unchanged content, or a one-shot iterable it
                    # snapshotted): reuse it untouched.
                    continue
                (
                    nested_patterns,
                    nested_signature,
                    nested_children,
                ) = url_pattern._build_candidate(nested_module, nested_ref)
                children.append(
                    (
                        url_pattern,
                        nested_module,
                        nested_ref,
                        nested_patterns,
                        nested_signature,
                        nested_children,
                    )
                )
        return patterns, signature, children

    @staticmethod
    def _install_candidate(patterns, signature, children):
        """
        Install a built candidate: nested tables deepest-first, then reset
        this resolver's derived caches. Only called after the whole candidate
        tree has built and validated successfully.
        """
        for (
            child,
            child_module,
            child_ref,
            child_patterns,
            child_signature,
            child_children,
        ) in children:
            child._install_candidate(
                child_patterns, child_signature, child_children
            )
            child._loaded_urlconf_module = child_module
            child._loaded_patterns_ref = child_ref
            child._loaded_url_patterns = child_patterns
            child._loaded_patterns_signature = child_signature
            child._patterns_generation += 1
            child._reverse_dict = {}
            child._namespace_dict = {}
            child._app_dict = {}
            child._callback_strs = set()
            child._populated = False
            child._loaded = True

    def _table_matches_signature(self, patterns_ref, signature):
        """
        Whether an exposed list still holds the entries captured by
        *signature* (see _patterns_signature()), in the same order.

        Comparison is by identity and incremental, stopping at the first
        changed entry, so the common case (the list was not touched) typically
        does no work beyond reading a length and allocates nothing. Only a
        mutable list needs this: a tuple is immutable, and a one-shot
        iterable's content is frozen once consumed, so for either identity is
        sufficient.
        """
        if not isinstance(patterns_ref, list):
            return True
        if len(patterns_ref) != len(signature):
            return False
        for index, pattern in enumerate(patterns_ref):
            if pattern is not signature[index]:
                return False
        return True

    def _matches_loaded_table(self, patterns_ref):
        """
        Whether *patterns_ref* is exactly the routing table currently loaded:
        the same exposed object and, for a held list edited in place,
        unchanged content (see _patterns_signature()).
        """
        if not self._loaded or patterns_ref is not self._loaded_patterns_ref:
            return False
        return self._table_matches_signature(
            patterns_ref, self._loaded_patterns_signature
        )

    def _reload_patterns(self):
        """
        Make sure the cached urlconf module and its urlpatterns reflect the
        urlconf's current routing table.

        A caller-supplied urlconf may change its ``urlpatterns`` at runtime
        (an observable configuration refresh) in one of two ways:

        * assigning a new ``urlpatterns`` object, or
        * editing the existing list in place (``patterns[:] = ...``,
          append(), pop(), item assignment or a reorder).

        The currently exposed table is compared with the one held here first
        by identity and then, for a held mutable list, by a content
        fingerprint (see _patterns_signature()); either kind of difference is
        a refresh. Identity -- rather than ``id()`` -- is what matters because
        the held reference keeps the old object alive, so its address can
        never be reused by a replacement. On refresh the patterns and every
        cache derived from them (reverse lookups, namespaces and callback
        strings) are rebuilt from the new table on demand, without the caller
        clearing any cache or reassigning the urlconf.

        A refresh onto a table that fails to load is never committed: the
        whole candidate tree (nested includes included) is built and validated
        before anything is installed, so an invalid replacement -- including
        invalid content written into the very same list -- cannot leave a
        half-installed table, the previously working table keeps serving, and
        the next edit (e.g. after the list is fixed) takes effect immediately.

        As historically, a urlpatterns that is itself a reusable sequence (a
        list or tuple, the normal case) is held by reference. A one-shot
        iterable (a generator) is snapshotted once.
        """
        # Hot path: if the table currently exposed is the one already loaded
        # -- the same object and, for a held list, unchanged content -- there
        # is nothing to do. This avoids an import_module() lookup on every
        # resolve() and keeps the untouched case allocation free.
        if self._loaded:
            current_ref = self._exposed_patterns_ref()
            if current_ref is not None and self._matches_loaded_table(current_ref):
                return
        urlconf_module, patterns_ref = self._current_patterns()
        if self._matches_loaded_table(patterns_ref):
            return
        if self._loaded:
            # A refresh replaces or edits a table that still works. Build and
            # validate the entire candidate -- nested includes included --
            # before installing any of it; _build_candidate() raises (usually
            # ImproperlyConfigured) without touching self or any resolver
            # reachable from the live table, so the previous table --
            # including its snapshot of a list that has since been edited in
            # place -- is left completely intact.
            new_patterns, new_signature, children = self._build_candidate(
                urlconf_module, patterns_ref
            )
            self._install_candidate(new_patterns, new_signature, children)
        else:
            # An initial load keeps its historical, lazy behavior: hold the
            # exposed sequence (snapshotting a one-shot iterable once) and
            # only fail on a pattern the request actually reaches.
            new_patterns = self._materialize_patterns(
                patterns_ref, self.urlconf_name
            )
            new_signature = self._patterns_signature(new_patterns)
        self._loaded_urlconf_module = urlconf_module
        self._loaded_patterns_ref = patterns_ref
        self._loaded_url_patterns = new_patterns
        self._loaded_patterns_signature = new_signature
        self._patterns_generation += 1
        self._reverse_dict = {}
        self._namespace_dict = {}
        self._app_dict = {}
        self._callback_strs = set()
        self._populated = False
        self._loaded = True

    @property
    def urlconf_module(self):
        self._reload_patterns()
        return self._loaded_urlconf_module

    @property
    def url_patterns(self):
        self._reload_patterns()
        return self._loaded_url_patterns

    @property
    def patterns_version(self):
        # Identifies the routing table currently loaded; advances on every
        # committed refresh. Used to key derived caches (see
        # get_ns_resolver()).
        self._reload_patterns()
        return self._patterns_generation

    def resolve_error_handler(self, view_type):
        callback = getattr(self.urlconf_module, "handler%s" % view_type, None)
        if not callback:
            # No handler specified in file; use lazy import, since
            # django.conf.urls imports this file.
            from django.conf import urls

            callback = getattr(urls, "handler%s" % view_type)
        return get_callable(callback)

    def reverse(self, lookup_view, *args, **kwargs):
        return self._reverse_with_prefix(lookup_view, "", *args, **kwargs)

    def _reverse_with_prefix(
        self, lookup_view, _prefix, *args, _ns_default_kwargs=None, **kwargs
    ):
        if args and kwargs:
            raise ValueError("Don't mix *args and **kwargs in call to reverse()!")

        if not self._populated:
            self._populate()

        possibilities = self.reverse_dict.getlist(lookup_view)

        for possibility, pattern, defaults, converters in possibilities:
            # Defaults contributed by include()s above a namespaced resolver
            # fill parameters captured on those ancestor routes. They have
            # lower precedence than the endpoint's own defaults, mirroring
            # the order resolve() merges them in.
            if _ns_default_kwargs:
                defaults = {**_ns_default_kwargs, **defaults}
            for result, params in possibility:
                if args:
                    if len(args) != len(params):
                        continue
                    candidate_subs = dict(zip(params, args))
                else:
                    if set(kwargs).symmetric_difference(params).difference(defaults):
                        continue
                    matches = True
                    for k, v in defaults.items():
                        if k in params:
                            continue
                        if kwargs.get(k, v) != v:
                            matches = False
                            break
                    if not matches:
                        continue
                    # The guard above guarantees every parameter absent from
                    # kwargs has a default. Fill in-pattern parameters from
                    # their defaults so they are substituted like any other
                    # captured value -- serialized through their converter and
                    # validated against the pattern -- instead of crashing on
                    # the missing key. Defaults for non-pattern parameters are
                    # only used in the matching check above and never reach the
                    # URL. kwargs is left untouched.
                    candidate_subs = {
                        k: defaults[k]
                        for k in params
                        if k in defaults and k not in kwargs
                    }
                    candidate_subs.update(kwargs)
                # Convert the candidate subs to text using Converter.to_url().
                text_candidate_subs = {}
                match = True
                for k, v in candidate_subs.items():
                    if k in converters:
                        try:
                            text_candidate_subs[k] = converters[k].to_url(v)
                        except ValueError:
                            match = False
                            break
                    else:
                        text_candidate_subs[k] = str(v)
                if not match:
                    continue
                # WSGI provides decoded URLs, without %xx escapes, and the URL
                # resolver operates on such URLs. First substitute arguments
                # without quoting to build a decoded URL and look for a match.
                # Then, if we have a match, redo the substitution with quoted
                # arguments in order to return a properly encoded URL.
                candidate_pat = _prefix.replace("%", "%%") + result
                if re.search(
                    "^%s%s" % (re.escape(_prefix), pattern),
                    candidate_pat % text_candidate_subs,
                ):
                    # safe characters from `pchar` definition of RFC 3986
                    safe_chars = RFC3986_SUBDELIMS + "/~:@"
                    # Quote the substituted arguments and the pattern
                    # skeleton separately. Arguments are fully encoded,
                    # while "?" and "#" literals from the pattern itself
                    # remain structural so that a reversed URL can carry a
                    # query string or a fragment identifier.
                    quoted_subs = {
                        k: quote(str(v), safe=safe_chars)
                        for k, v in text_candidate_subs.items()
                    }
                    url = quote(_prefix, safe=safe_chars) + quote(
                        result % quoted_subs, safe=safe_chars + "?#%"
                    )
                    # Don't allow construction of scheme relative urls.
                    return escape_leading_slashes(url)
        # lookup_view can be URL name or callable, but callables are not
        # friendly in error messages.
        m = getattr(lookup_view, "__module__", None)
        n = getattr(lookup_view, "__name__", None)
        if m is not None and n is not None:
            lookup_view_s = "%s.%s" % (m, n)
        else:
            lookup_view_s = lookup_view

        patterns = [pattern for (_, pattern, _, _) in possibilities]
        if patterns:
            if args:
                arg_msg = "arguments '%s'" % (args,)
            elif kwargs:
                arg_msg = "keyword arguments '%s'" % kwargs
            else:
                arg_msg = "no arguments"
            msg = "Reverse for '%s' with %s not found. %d pattern(s) tried: %s" % (
                lookup_view_s,
                arg_msg,
                len(patterns),
                patterns,
            )
        else:
            msg = (
                "Reverse for '%(view)s' not found. '%(view)s' is not "
                "a valid view function or pattern name." % {"view": lookup_view_s}
            )
        raise NoReverseMatch(msg)
