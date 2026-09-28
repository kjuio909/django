import json

from django.core import checks
from django.db.models import NOT_PROVIDED, Field
from django.db.models.expressions import ColPairs
from django.db.models.fields.tuple_lookups import (
    TupleExact,
    TupleGreaterThan,
    TupleGreaterThanOrEqual,
    TupleIn,
    TupleIsNull,
    TupleLessThan,
    TupleLessThanOrEqual,
)
from django.utils.functional import cached_property
from django.utils.regex_helper import _lazy_re_compile

# Characters that are quoted when a primary key value is encoded as a string.
# The comma is quoted too, so it can be used as the separator between the
# parts of a composite primary key without being confused with a value.
QUOTE_MAP = {i: "_%02X" % i for i in b'":/_#?;@&=+$,"[]<>%\n\\'}
UNQUOTE_MAP = {v: chr(k) for k, v in QUOTE_MAP.items()}
UNQUOTE_RE = _lazy_re_compile("_(?:%s)" % "|".join([x[1:] for x in UNQUOTE_MAP]))
# Separator between the parts of an encoded composite primary key. It is also
# quoted, so commas that are part of a value (e.g. "a,b" -> "a_2Cb") cannot be
# confused with the separator.
COMPOSITE_PK_SEPARATOR = ","
# Representation of a None part in an encoded composite primary key. "@" is
# quoted to "_40" when it's part of a value, so the literal token can only
# appear in an encoded key as the None marker. It also keeps empty strings
# ("") distinct from None.
COMPOSITE_PK_NULL = "@"


def quote(s):
    """
    Encode a primary key value as a string in which characters that would be
    problematic in a URL or in an encoded composite key are escaped.

    A composite primary key (a tuple or a list) is encoded as the
    comma-separated quoted representations of its parts, in declared order. A
    None part is encoded as COMPOSITE_PK_NULL.
    """
    if isinstance(s, str):
        return s.translate(QUOTE_MAP)
    if isinstance(s, (tuple, list)):
        return COMPOSITE_PK_SEPARATOR.join(
            COMPOSITE_PK_NULL if part is None else quote(str(part)) for part in s
        )
    return s


def unquote(s):
    """Undo the effects of quote() on a single primary key value."""
    return UNQUOTE_RE.sub(lambda m: UNQUOTE_MAP[m[0]], s)


def split_parts(object_id):
    """
    Split an encoded composite primary key into its encoded parts. The parts
    are not unquoted, so use unquote() on each of them. A None part appears
    as COMPOSITE_PK_NULL and should be passed through unchanged.
    """
    return object_id.split(COMPOSITE_PK_SEPARATOR)


class AttributeSetter:
    def __init__(self, name, value):
        setattr(self, name, value)


class CompositeAttribute:
    def __init__(self, field):
        self.field = field

    @property
    def attnames(self):
        return [field.attname for field in self.field.fields]

    def __get__(self, instance, cls=None):
        return tuple(getattr(instance, attname) for attname in self.attnames)

    def __set__(self, instance, values):
        attnames = self.attnames
        length = len(attnames)

        if values is None:
            values = (None,) * length

        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{self.field.name!r} must be a list or a tuple.")
        if length != len(values):
            raise ValueError(f"{self.field.name!r} must have {length} elements.")

        for attname, value in zip(attnames, values):
            setattr(instance, attname, value)


class CompositePrimaryKey(Field):
    descriptor_class = CompositeAttribute

    def __init__(self, *args, **kwargs):
        if (
            not args
            or not all(isinstance(field, str) for field in args)
            or len(set(args)) != len(args)
        ):
            raise ValueError("CompositePrimaryKey args must be unique strings.")
        if len(args) == 1:
            raise ValueError("CompositePrimaryKey must include at least two fields.")
        if kwargs.get("default", NOT_PROVIDED) is not NOT_PROVIDED:
            raise ValueError("CompositePrimaryKey cannot have a default.")
        if kwargs.get("db_default", NOT_PROVIDED) is not NOT_PROVIDED:
            raise ValueError("CompositePrimaryKey cannot have a database default.")
        if kwargs.get("db_column", None) is not None:
            raise ValueError("CompositePrimaryKey cannot have a db_column.")
        if kwargs.setdefault("editable", False):
            raise ValueError("CompositePrimaryKey cannot be editable.")
        if not kwargs.setdefault("primary_key", True):
            raise ValueError("CompositePrimaryKey must be a primary key.")
        if not kwargs.setdefault("blank", True):
            raise ValueError("CompositePrimaryKey must be blank.")

        self.field_names = args
        super().__init__(**kwargs)

    def deconstruct(self):
        # args is always [] so it can be ignored.
        name, path, _, kwargs = super().deconstruct()
        return name, path, self.field_names, kwargs

    @cached_property
    def fields(self):
        meta = self.model._meta
        return tuple(meta.get_field(field_name) for field_name in self.field_names)

    @cached_property
    def columns(self):
        return tuple(field.column for field in self.fields)

    def contribute_to_class(self, cls, name, private_only=False):
        super().contribute_to_class(cls, name, private_only=private_only)
        cls._meta.pk = self
        setattr(cls, self.attname, self.descriptor_class(self))

    def get_attname_column(self):
        return self.get_attname(), None

    def __iter__(self):
        return iter(self.fields)

    def __len__(self):
        return len(self.field_names)

    @cached_property
    def cached_col(self):
        return ColPairs(self.model._meta.db_table, self.fields, self.fields, self)

    def get_col(self, alias, output_field=None):
        if alias == self.model._meta.db_table and (
            output_field is None or output_field == self
        ):
            return self.cached_col

        return ColPairs(alias, self.fields, self.fields, output_field)

    def get_pk_value_on_save(self, instance):
        values = []

        for field in self.fields:
            value = field.value_from_object(instance)
            if value is None:
                value = field.get_pk_value_on_save(instance)
            values.append(value)

        return tuple(values)

    def _check_field_name(self):
        if self.name == "pk":
            return []
        return [
            checks.Error(
                "'CompositePrimaryKey' must be named 'pk'.",
                obj=self,
                id="fields.E013",
            )
        ]

    def value_to_string(self, obj):
        values = []
        vals = self.value_from_object(obj)
        for field, value in zip(self.fields, vals):
            if value is None:
                values.append(None)
            else:
                obj = AttributeSetter(field.attname, value)
                values.append(field.value_to_string(obj))
        return json.dumps(values, ensure_ascii=False)

    def to_python(self, value):
        if isinstance(value, str):
            # Assume we're deserializing.
            vals = json.loads(value)
            value = [
                field.to_python(val)
                for field, val in zip(self.fields, vals, strict=True)
            ]
        return value

    def encode_pk(self, value):
        """
        Encode a composite primary key (a tuple of Python values, as exposed
        on ``instance.pk``) as a single string, quoting each part with
        quote(). The order of the parts follows the declaration order.
        """
        return quote(tuple(value))

    def decode_pk(self, value):
        """
        Decode a string produced by encode_pk() (or an iterable of unquoted
        parts) into a tuple of Python values in declared order. Raise
        ValueError for a malformed value, a value with the wrong number of
        parts, or a part that cannot be converted by its component field.
        """
        if isinstance(value, str):
            parts = split_parts(value)
            parts = [
                None if part == COMPOSITE_PK_NULL else unquote(part) for part in parts
            ]
        elif isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            raise ValueError("Composite primary key must be a string or iterable.")
        if len(parts) != len(self.fields):
            raise ValueError(
                f"Composite primary key must have {len(self.fields)} parts."
            )
        converted = []
        for field, part in zip(self.fields, parts):
            if part is None:
                converted.append(None)
            else:
                converted.append(field.to_python(part))
        return tuple(converted)


CompositePrimaryKey.register_lookup(TupleExact)
CompositePrimaryKey.register_lookup(TupleGreaterThan)
CompositePrimaryKey.register_lookup(TupleGreaterThanOrEqual)
CompositePrimaryKey.register_lookup(TupleLessThan)
CompositePrimaryKey.register_lookup(TupleLessThanOrEqual)
CompositePrimaryKey.register_lookup(TupleIn)
CompositePrimaryKey.register_lookup(TupleIsNull)


def unnest(fields):
    result = []

    for field in fields:
        if isinstance(field, CompositePrimaryKey):
            result.extend(field.fields)
        else:
            result.append(field)

    return result
