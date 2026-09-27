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
