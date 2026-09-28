"""Custom path converters used by the i18n URL resolution tests."""


class BoundedConverter:
    """
    A bounded converter for an unreserved-ish character set: one to eight
    Unicode word characters, dots, tildes or dashes. It never matches a slash
    or a percent sign, so the raw matching machinery has to widen its capture
    for percent escapes on its own.
    """

    regex = r"[\w.~-]{1,8}"

    def to_python(self, value):
        # A converter is allowed to reject a value by raising; "forbidden"
        # exercises a value that passes the regex but is refused here.
        if value == "forbidden":
            raise ValueError("value is forbidden")
        return value

    def to_url(self, value):
        return value


class ExplodingConverter:
    """A converter whose to_python() raises a non-ValueError exception."""

    regex = r"[a-z]{1,4}"

    def to_python(self, value):
        raise RuntimeError("converter blew up")

    def to_url(self, value):
        return value
