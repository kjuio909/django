from collections.abc import Iterable
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from asgiref.local import Local

from django.http import QueryDict
from django.utils.functional import Promise, lazy
from django.utils.translation import override

from .exceptions import NoReverseMatch, Resolver404
from .resolvers import _get_cached_resolver, get_ns_resolver, get_resolver
from .utils import get_callable

# SCRIPT_NAME prefixes for each thread are stored here. If there's no entry for
# the current thread (which is the only one we ever access), it is assumed to
# be empty.
_prefixes = Local()

# Overridden URLconfs for each thread are stored here.
_urlconfs = Local()


def _query_leaves(value):
    """
    Yield the scalar query values contained in *value*, in input order.

    Strings, bytes, lazy string proxies and non-iterable objects are yielded
    unchanged so that urllib's urlencode() semantics apply (str() conversion,
    bytes quoting, ``None`` -> ``"None"`` and so on). Lists, tuples, sets and
    other iterables are expanded recursively, so a container is never written
    to the URL through its repr().
    """
    if isinstance(value, (str, bytes, Promise)):
        yield value
    elif isinstance(value, Iterable):
        for item in value:
            yield from _query_leaves(item)
    else:
        yield value


def _query_pairs(query):
    """
    Yield (key, scalar_value) pairs for a reverse() ``query`` argument.

    The iteration order of the supplied container is preserved. Objects
    exposing an ``items()`` method are treated as mappings (mirroring
    urllib.parse.urlencode()), other sequences are walked in their own order.
    The container validation mirrors urlencode() so the same TypeErrors are
    raised for invalid input; the argument is only read, never modified.
    """
    if hasattr(query, "items"):
        items = query.items()
    else:
        try:
            # Mirrors urlencode(): only sequences whose first element is a
            # (key, value) tuple are accepted; bare scalars, strings and
            # non-indexable containers fail here.
            if len(query) and not isinstance(query[0], tuple):
                raise TypeError
        except TypeError as err:
            raise TypeError(
                "not a valid non-string sequence or mapping object"
            ) from err
        items = query
    for pair in items:
        key, value = pair
        for leaf in _query_leaves(value):
            yield key, leaf


def _append_query_fragment(url, query=None, fragment=None):
    """
    Append an encoded query string and/or fragment identifier to *url*, a
    path returned by the URL resolver.

    The query string follows the path and the fragment always comes last.
    Separators are never added for an absent value, an explicitly empty
    fragment still terminates the URL with ``#``, and a query or fragment
    already present in *url* is joined rather than given a second separator.
    """
    scheme, netloc, path, existing_query, existing_fragment = urlsplit(url)
    if query is not None:
        if isinstance(query, QueryDict):
            # QueryDict.urlencode() keeps every repeated value, in insertion
            # order, and honours the QueryDict's own encoding.
            query_string = query.urlencode()
        else:
            # Values are already expanded into scalars, so doseq=False keeps
            # lazy string proxies intact (doseq=True would iterate them
            # character by character). An empty container yields "".
            query_string = urlencode(list(_query_pairs(query)), doseq=False)
        if query_string:
            existing_query = (
                f"{existing_query}&{query_string}" if existing_query else query_string
            )
    fragment_is_empty = False
    if fragment is not None:
        # A plain str is accepted, as are lazy string proxies, which behave
        # exactly like str once evaluated. Everything else raises TypeError,
        # matching the previous str-concatenation contract.
        if not isinstance(fragment, (str, Promise)):
            raise TypeError(
                "fragment must be a string, not %s" % type(fragment).__name__
            )
        fragment = str(fragment)
        fragment_is_empty = not fragment
        existing_fragment = fragment
    url = urlunsplit((scheme, netloc, path, existing_query, existing_fragment))
    # urlunsplit() drops an empty fragment, but an explicitly supplied empty
    # fragment must still end the URL with "#".
    if fragment_is_empty and not url.endswith("#"):
        url += "#"
    return url


def resolve(path, urlconf=None):
    if urlconf is None:
        urlconf = get_urlconf()
    return get_resolver(urlconf).resolve(path)


def reverse(
    viewname,
    urlconf=None,
    args=None,
    kwargs=None,
    current_app=None,
    *,
    query=None,
    fragment=None,
):
    if urlconf is None:
        urlconf = get_urlconf()
    resolver = get_resolver(urlconf)
    args = args or []
    kwargs = kwargs or {}

    prefix = get_script_prefix()

    if not isinstance(viewname, str):
        view = viewname
    else:
        *path, view = viewname.split(":")

        if current_app:
            current_path = current_app.split(":")
            current_path.reverse()
        else:
            current_path = None

        resolved_path = []
        ns_pattern = ""
        ns_converters = {}
        for ns in path:
            current_ns = current_path.pop() if current_path else None
            # Lookup the name to see if it could be an app identifier.
            try:
                app_list = resolver.app_dict[ns]
                # Yes! Path part matches an app in the current Resolver.
                if current_ns and current_ns in app_list:
                    # If we are reversing for a particular app, use that
                    # namespace.
                    ns = current_ns
                elif ns not in app_list:
                    # The name isn't shared by one of the instances (i.e.,
                    # the default) so pick the first instance as the default.
                    ns = app_list[0]
            except KeyError:
                pass

            if ns != current_ns:
                current_path = None

            try:
                extra, resolver = resolver.namespace_dict[ns]
                resolved_path.append(ns)
                ns_pattern += extra
                ns_converters.update(resolver.pattern.converters)
            except KeyError as key:
                if resolved_path:
                    raise NoReverseMatch(
                        "%s is not a registered namespace inside '%s'"
                        % (key, ":".join(resolved_path))
                    )
                else:
                    raise NoReverseMatch("%s is not a registered namespace" % key)
        if ns_pattern:
            resolver = get_ns_resolver(
                ns_pattern, resolver, tuple(ns_converters.items())
            )

    resolved_url = resolver._reverse_with_prefix(view, prefix, *args, **kwargs)
    if query is not None or fragment is not None:
        resolved_url = _append_query_fragment(resolved_url, query, fragment)
    return resolved_url


reverse_lazy = lazy(reverse, str)


def clear_url_caches():
    get_callable.cache_clear()
    _get_cached_resolver.cache_clear()
    get_ns_resolver.cache_clear()


def set_script_prefix(prefix):
    """
    Set the script prefix for the current thread.
    """
    if not prefix.endswith("/"):
        prefix += "/"
    _prefixes.value = prefix


def get_script_prefix():
    """
    Return the currently active script prefix. Useful for client code that
    wishes to construct their own URLs manually (although accessing the request
    instance is normally going to be a lot cleaner).
    """
    return getattr(_prefixes, "value", "/")


def clear_script_prefix():
    """
    Unset the script prefix for the current thread.
    """
    try:
        del _prefixes.value
    except AttributeError:
        pass


def set_urlconf(urlconf_name):
    """
    Set the URLconf for the current thread or asyncio task (overriding the
    default one in settings). If urlconf_name is None, revert back to the
    default.
    """
    if urlconf_name:
        _urlconfs.value = urlconf_name
    else:
        if hasattr(_urlconfs, "value"):
            del _urlconfs.value


def get_urlconf(default=None):
    """
    Return the root URLconf to use for the current thread or asyncio task if it
    has been changed from the default one.
    """
    return getattr(_urlconfs, "value", default)


def is_valid_path(path, urlconf=None):
    """
    Return the ResolverMatch if the given path resolves against the default URL
    resolver, False otherwise. This is a convenience method to make working
    with "is this a match?" cases easier, avoiding try...except blocks.
    """
    try:
        return resolve(path, urlconf)
    except Resolver404:
        return False


def translate_url(url, lang_code):
    """
    Given a URL (absolute or relative), try to get its translated version in
    the `lang_code` language (either by i18n_patterns or by translated regex).
    Return the original URL if no translated version is found.
    """
    parsed = urlsplit(url)
    try:
        # URL may be encoded.
        match = resolve(unquote(parsed.path))
    except Resolver404:
        pass
    else:
        to_be_reversed = (
            "%s:%s" % (match.namespace, match.url_name)
            if match.namespace
            else match.url_name
        )
        with override(lang_code):
            try:
                url = reverse(to_be_reversed, args=match.args, kwargs=match.kwargs)
            except NoReverseMatch:
                pass
            else:
                url = urlunsplit(
                    (parsed.scheme, parsed.netloc, url, parsed.query, parsed.fragment)
                )
    return url
