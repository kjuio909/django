import base64


class Base64Converter:
    regex = r"[a-zA-Z0-9+/]*={0,2}"

    def to_python(self, value):
        return base64.b64decode(value, validate=True)

    def to_url(self, value):
        return base64.b64encode(value).decode("ascii")


class DynamicConverter:
    _dynamic_to_python = None
    _dynamic_to_url = None

    @property
    def regex(self):
        return r"[0-9a-zA-Z]+"

    @regex.setter
    def regex(self):
        raise Exception("You can't modify the regular expression.")

    def to_python(self, value):
        return type(self)._dynamic_to_python(value)

    def to_url(self, value):
        return type(self)._dynamic_to_url(value)

    @classmethod
    def register_to_python(cls, value):
        cls._dynamic_to_python = value

    @classmethod
    def register_to_url(cls, value):
        cls._dynamic_to_url = value


class DecodedIntConverter:
    # The resolver percent-decodes captures exactly once before calling
    # to_python(), which only has to validate the decoded value.
    regex = "[0-9]+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(value)


class BoundedTextConverter:
    # One to twelve characters admitting Unicode text, spaces and reserved
    # URL characters, but never a '%', a '/' or a '*'. Matching runs against
    # the raw path: a percent escape is decoded once and then re-validated
    # against this regex, so an encoded slash or star cannot be smuggled in
    # and an over-long value stays bounded.
    regex = r"[^%/*\n]{1,12}"

    def to_python(self, value):
        # Return a distinct type of value (upper-cased) so tests can assert
        # the decoded text -- not its raw encoding -- reached the converter
        # exactly once.
        return value.upper()

    def to_url(self, value):
        return str(value).lower()


class RejectingConverter:
    # Accepts the text syntactically, but the value is always rejected.
    regex = "[0-9]+"

    def to_python(self, value):
        raise ValueError("value is not allowed")

    def to_url(self, value):
        return str(value)


class NoneReturningConverter:
    regex = "[0-9]+"

    def to_python(self, value):
        return None

    def to_url(self, value):
        return str(value)
