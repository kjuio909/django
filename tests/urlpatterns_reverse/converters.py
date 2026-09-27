class ZeroPaddedIntConverter:
    """
    Converter with observable (de)serialization: Python values are ints while
    the reversed URL carries a zero-padded form. The serialized form still
    matches ``regex``; ``to_url()`` rejects negatives.
    """

    regex = "[0-9]+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        value = int(value)
        if value < 0:
            raise ValueError("Negative values are not allowed.")
        return f"{value:03d}"


class BoundedIntConverter:
    """
    An int converter with a domain rule: values must be within [0, 999].
    Out-of-range digits still match the regex but are rejected in
    to_python(), so resolve() must fall through to a sibling route rather
    than treating the digits as a match.
    """

    regex = "[0-9]+"

    def to_python(self, value):
        value = int(value)
        if not 0 <= value <= 999:
            raise ValueError("Value must be between 0 and 999.")
        return value

    def to_url(self, value):
        return str(value)


class ExplodingConverter:
    """
    A converter whose to_python() raises ValueError from deep inside its
    parsing for one specific input. The resolver must surface only the
    conventional resolution failure (Resolver404) -- never a half-built
    match -- and must still resolve a following valid path.
    """

    regex = "[^/]+"

    def to_python(self, value):
        def parse(token):
            if token == "explode":
                # Raised by an internal helper, as a real parser would when
                # the value is rejected.
                raise ValueError("internal lookup rejected the value")
            return token.upper()

        return parse(value)

    def to_url(self, value):
        return value
