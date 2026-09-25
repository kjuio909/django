import functools
import itertools
import json
from collections import defaultdict

from asgiref.sync import sync_to_async

from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.core.exceptions import (
    FieldDoesNotExist,
    ObjectDoesNotExist,
    ValidationError,
)
from django.db import DEFAULT_DB_ALIAS, NotSupportedError, models, router, transaction
from django.db.models import DO_NOTHING, ForeignObject, ForeignObjectRel
from django.db.models.base import ModelBase, make_foreign_order_accessors
from django.db.models.deletion import DatabaseOnDelete
from django.db.models.expressions import Col, Expression, ResolvedOuterRef
from django.db.models.fields import Field
from django.db.models.fields.composite import CompositePrimaryKey
from django.db.models.fields.mixins import FieldCacheMixin
from django.db.models.fields.related import (
    ReverseManyToOneDescriptor,
    lazy_related_operation,
)
from django.db.models.query import prefetch_related_objects
from django.db.models.query_utils import PathInfo
from django.db.models.sql import AND
from django.db.models.sql.where import WhereNode
from django.db.models.utils import AltersData
from django.utils.functional import cached_property

# Sentinel used as part of a prefetch match key for a reference that couldn't
# be decoded. It's an object instance (never produced by to_python()), so the
# key can't collide with a real composite primary key.
INVALID_COMPOSITE_REFERENCE = object()


def _composite_component_json_type(field):
    """
    Return the JSON type (``integer``, ``real`` or ``text``) that a component
    field's values are encoded as by GenericForeignKey.encode_composite_pk().

    A JSON element of another type can never match the component, so joins can
    enforce strict typing the same way decode_composite_pk() does in Python:
    a JSON number never stands in for a text component and vice versa, and the
    JSON booleans true/false are distinct from the integers 1/0.
    """
    if isinstance(field, models.BooleanField):
        return "boolean"
    if isinstance(field, models.IntegerField):
        return "integer"
    if isinstance(field, models.FloatField):
        return "real"
    # Other component kinds (CharField/TextField, date/time fields, UUIDField,
    # ...) are encoded as JSON strings. Components that can't be JSON encoded
    # (e.g. DecimalField) can never have a valid reference stored, so treating
    # them as text means a numeric element is safely rejected rather than
    # coerced into a match.
    return "text"


class CompositeGenericKeyMatch(Expression):
    """
    Predicate matching a GenericForeignKey's JSON-array text reference
    (``object_id``) against the individual columns of a related model's
    CompositePrimaryKey.

    The predicate is defensive about the stored reference:

    * a SQL NULL reference never matches;
    * malformed JSON, a non-array value, or an array with the wrong number of
      components never match and never raise a database error (the element
      checks are only evaluated inside a ``CASE`` once the reference has been
      validated);
    * each element must have exactly the JSON type produced by the component
      field, so an integer can't stand in for a string, the booleans
      ``true``/``false`` can't stand in for 1/0, and a JSON ``null`` only
      matches a column that is itself NULL.

    Two construction modes are supported:

    * :meth:`for_aliases` -- an ordinary join, with both the JSON reference
      column and the composite primary key columns already resolved against
      concrete table aliases;
    * :meth:`correlated` -- the ``split_exclude()`` case, where the composite
      primary key is an unresolved :class:`ColPairs` referencing the outer
      query. It's resolved by resolve_expression() when the subquery predicate
      is resolved against the outer query.
    """

    conditional = True
    allows_composite_expressions = True
    output_field = models.BooleanField()

    def __init__(self, object_id_col, target_cols=(), outer_pk=None):
        super().__init__()
        self.lhs = object_id_col
        # WhereNode._resolve_node() resolves a node's ``rhs`` attribute, so in
        # the correlated case the ResolvedOuterRef is turned into the outer
        # query's ColPairs there. In the join case ``rhs`` is simply the
        # concrete primary key columns.
        self.rhs = outer_pk if outer_pk is not None else tuple(target_cols)

    @classmethod
    def for_aliases(cls, source_alias, object_id_field, target_alias, pk_fields):
        return cls(
            Col(source_alias, object_id_field),
            target_cols=[Col(target_alias, field) for field in pk_fields],
        )

    @classmethod
    def correlated(cls, object_id_col, outer_pk):
        return cls(object_id_col, outer_pk=outer_pk)

    def __repr__(self):
        return "<%s: %s -> %s composite primary key>" % (
            self.__class__.__name__,
            self.lhs,
            self.rhs,
        )

    @property
    def target_cols(self):
        # A resolved ColPairs (correlated case) iterates over its Cols; a
        # tuple already is the sequence of Cols (join case).
        return tuple(self.rhs)

    def get_source_expressions(self):
        rhs = self.rhs
        return [self.lhs, *(rhs if isinstance(rhs, (tuple, list)) else [rhs])]

    def set_source_expressions(self, exprs):
        self.lhs = exprs[0]
        rest = exprs[1:]
        self.rhs = rest[0] if len(rest) == 1 else tuple(rest)

    def relabeled_clone(self, relabels):
        rhs = self.rhs
        if isinstance(rhs, (tuple, list)):
            new_rhs = tuple(col.relabeled_clone(relabels) for col in rhs)
        else:
            new_rhs = rhs.relabeled_clone(relabels)
        clone = self.__class__(self.lhs.relabeled_clone(relabels))
        clone.rhs = new_rhs
        return clone

    def as_sqlite(self, compiler, connection):
        # Col compiles to a quoted table.column with no parameters.
        ref, _ = compiler.compile(self.lhs)
        fragments = [
            f"{ref} IS NOT NULL",
            f"json_valid({ref}) = 1",
            f"json_type({ref}) = 'array'",
            f"json_array_length({ref}) = %s",
        ]
        params = [len(self.target_cols)]
        for index, col in enumerate(self.target_cols):
            col_sql, col_params = compiler.compile(col)
            path = "$[%d]" % index
            json_type_name = _composite_component_json_type(col.target)
            null_type_sql = f"json_type({ref}, %s)"
            value_type_sql = (
                f"{null_type_sql} IN ('true', 'false')"
                if json_type_name == "boolean"
                else f"{null_type_sql} = %s"
            )
            fragments.append(
                "("
                f"({null_type_sql} = 'null' AND {col_sql} IS NULL) OR "
                f"({value_type_sql} AND json_extract({ref}, %s) = {col_sql})"
                ")"
            )
            if json_type_name == "boolean":
                # null-check path; type-check path; extract path
                params.extend([path, path, path])
            else:
                # null-check path; type-check path + JSON type name; extract
                params.extend([path, path, json_type_name, path])
            params.extend(col_params)
        return "CASE WHEN %s THEN 1 END" % " AND ".join(fragments), tuple(params)

    def as_sql(self, compiler, connection):
        raise NotSupportedError(
            "Cross-relation queries on a GenericRelation to a model with a "
            "CompositePrimaryKey are only supported on database backends that "
            "can query the stored JSON object reference."
        )


class GenericForeignKey(FieldCacheMixin, Field):
    """
    Provide a generic many-to-one relation through the ``content_type`` and
    ``object_id`` fields.

    This class also doubles as an accessor to the related object (similar to
    ForwardManyToOneDescriptor) by adding itself as a model attribute.
    """

    many_to_many = False
    many_to_one = True
    one_to_many = False
    one_to_one = False

    def __init__(
        self, ct_field="content_type", fk_field="object_id", for_concrete_model=True
    ):
        super().__init__(editable=False)
        self.ct_field = ct_field
        self.fk_field = fk_field
        self.for_concrete_model = for_concrete_model
        self.is_relation = True

    def contribute_to_class(self, cls, name, **kwargs):
        super().contribute_to_class(cls, name, private_only=True, **kwargs)
        setattr(cls, self.attname, GenericForeignKeyDescriptor(self))

    def get_attname_column(self):
        attname, column = super().get_attname_column()
        return attname, None

    @cached_property
    def ct_field_attname(self):
        return self.model._meta.get_field(self.ct_field).attname

    def get_filter_kwargs_for_object(self, obj):
        """See corresponding method on Field"""
        return {
            self.fk_field: getattr(obj, self.fk_field),
            self.ct_field_attname: getattr(obj, self.ct_field_attname),
        }

    def get_forward_related_filter(self, obj):
        """See corresponding method on RelatedField"""
        fk_value = obj.pk
        pk_field = obj._meta.pk
        if isinstance(pk_field, CompositePrimaryKey):
            fk_value = self.encode_composite_pk(pk_field, fk_value)
        return {
            self.fk_field: fk_value,
            self.ct_field: ContentType.objects.get_for_model(obj).pk,
        }

    def check(self, **kwargs):
        return [
            *self._check_field_name(),
            *self._check_object_id_field(),
            *self._check_content_type_field(),
        ]

    def _check_object_id_field(self):
        try:
            self.model._meta.get_field(self.fk_field)
        except FieldDoesNotExist:
            return [
                checks.Error(
                    "The GenericForeignKey object ID references the "
                    "nonexistent field '%s'." % self.fk_field,
                    obj=self,
                    id="contenttypes.E001",
                )
            ]
        else:
            return []

    def _check_content_type_field(self):
        """
        Check if field named `field_name` in model `model` exists and is a
        valid content_type field (is a ForeignKey to ContentType).
        """
        try:
            field = self.model._meta.get_field(self.ct_field)
        except FieldDoesNotExist:
            return [
                checks.Error(
                    "The GenericForeignKey content type references the "
                    "nonexistent field '%s.%s'."
                    % (self.model._meta.object_name, self.ct_field),
                    obj=self,
                    id="contenttypes.E002",
                )
            ]
        else:
            if not isinstance(field, models.ForeignKey):
                return [
                    checks.Error(
                        "'%s.%s' is not a ForeignKey."
                        % (self.model._meta.object_name, self.ct_field),
                        hint=(
                            "GenericForeignKeys must use a ForeignKey to "
                            "'contenttypes.ContentType' as the 'content_type' field."
                        ),
                        obj=self,
                        id="contenttypes.E003",
                    )
                ]
            elif field.remote_field.model != ContentType:
                return [
                    checks.Error(
                        "'%s.%s' is not a ForeignKey to 'contenttypes.ContentType'."
                        % (self.model._meta.object_name, self.ct_field),
                        hint=(
                            "GenericForeignKeys must use a ForeignKey to "
                            "'contenttypes.ContentType' as the 'content_type' field."
                        ),
                        obj=self,
                        id="contenttypes.E004",
                    )
                ]
            elif isinstance(field.remote_field.on_delete, DatabaseOnDelete):
                return [
                    checks.Error(
                        f"'{self.model._meta.object_name}.{self.ct_field}' cannot use "
                        "the database-level on_delete variant.",
                        hint="Change the on_delete rule to the non-database variant.",
                        obj=self,
                        id="contenttypes.E006",
                    )
                ]
            else:
                return []

    @cached_property
    def cache_name(self):
        return self.name

    def get_content_type(self, obj=None, id=None, using=None, model=None):
        if obj is not None:
            return ContentType.objects.db_manager(obj._state.db).get_for_model(
                obj, for_concrete_model=self.for_concrete_model
            )
        elif id is not None:
            return ContentType.objects.db_manager(using).get_for_id(id)
        elif model is not None:
            return ContentType.objects.db_manager(using).get_for_model(
                model, for_concrete_model=self.for_concrete_model
            )
        else:
            # This should never happen. I love comments like this, don't you?
            raise Exception("Impossible arguments to GFK.get_content_type!")

    @staticmethod
    def encode_composite_pk(pk_field, pk):
        """
        Encode a composite primary key as a JSON array string, using the
        declaration order of the key's fields. Each element is converted with
        the corresponding component field's to_python() and normalized with
        get_prep_value(), so it round-trips losslessly through JSON. None is
        preserved as JSON null and can never be confused with the string
        "None", an empty string, or a missing element.
        """
        values = [
            None if value is None else field.get_prep_value(field.to_python(value))
            for field, value in zip(pk_field.fields, pk)
        ]
        try:
            return json.dumps(values, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Composite primary key has an unencodable component."
            ) from exc

    @staticmethod
    def decode_composite_pk(pk_field, value):
        """
        Decode a JSON array string produced by encode_composite_pk() back into
        a tuple of values converted with the component fields' to_python().

        Each JSON element must already be in the field's canonical form: e.g.
        a JSON number may not stand in for a CharField component (so the
        integer 1 and the string "1" can't be confused), and a JSON string
        may not stand in for an IntegerField component.

        Raise ValueError if the value isn't a JSON array, if its length
        doesn't match the composite key, if an element has the wrong JSON
        type, or if a component can't be converted.
        """
        try:
            raw_values = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Malformed composite primary key reference.") from exc
        if not isinstance(raw_values, list):
            raise ValueError("Composite primary key reference must be a JSON array.")
        fields = pk_field.fields
        if len(raw_values) != len(fields):
            raise ValueError(
                "Composite primary key reference has the wrong number of components."
            )
        values = []
        for field, raw in zip(fields, raw_values):
            if raw is None:
                values.append(None)
                continue
            try:
                converted = field.to_python(raw)
                # The element must already be in canonical form, so a value of
                # the wrong JSON type can't be coerced into a match.
                canonical = field.get_prep_value(converted)
                if json.dumps(canonical, ensure_ascii=False) != json.dumps(
                    raw, ensure_ascii=False
                ):
                    raise ValueError
                values.append(converted)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Composite primary key reference has an invalid component."
                ) from exc
        return tuple(values)

    def composite_pk_filter(self, pk_field, pk_value):
        """
        Return a Q object matching the composite primary key ``pk_value``,
        using IS NULL for components that are None (a tuple comparison against
        NULL would never match).
        """
        conditions = models.Q()
        for field, value in zip(pk_field.fields, pk_value):
            if value is None:
                conditions &= models.Q(**{field.name + "__isnull": True})
            else:
                conditions &= models.Q(**{field.name: value})
        return conditions


class GenericForeignKeyDescriptor:
    def __init__(self, field):
        self.field = field

    def is_cached(self, instance):
        return self.field.is_cached(instance)

    def _decode_reference(self, pk_field, fk_val):
        """
        Decode a stored reference into a hashable, to_python()-normalized key
        for a composite primary key. Return None for a malformed reference.
        """
        try:
            return GenericForeignKey.decode_composite_pk(pk_field, fk_val)
        except (TypeError, ValueError):
            return None

    def get_prefetch_querysets(self, instances, querysets=None):
        custom_queryset_dict = {}
        if querysets is not None:
            for queryset in querysets:
                ct_id = self.field.get_content_type(
                    model=queryset.query.model, using=queryset.db
                ).pk
                if ct_id in custom_queryset_dict:
                    raise ValueError(
                        "Only one queryset is allowed for each content type."
                    )
                custom_queryset_dict[ct_id] = queryset

        # For efficiency, group the instances by content type and then do one
        # query per model
        fk_dict = defaultdict(set)
        # We need one instance for each group in order to get the right db:
        instance_dict = {}
        ct_attname = self.field.model._meta.get_field(self.field.ct_field).attname
        for instance in instances:
            # We avoid looking for values if either ct_id or fkey value is None
            ct_id = getattr(instance, ct_attname)
            if ct_id is not None:
                fk_val = getattr(instance, self.field.fk_field)
                if fk_val is not None:
                    fk_dict[ct_id].add(fk_val)
                    instance_dict[ct_id] = instance

        ret_val = []
        for ct_id, fkeys in fk_dict.items():
            if ct_id in custom_queryset_dict:
                # Return values from the custom queryset, if provided.
                queryset = custom_queryset_dict[ct_id]
                model = queryset.model
            else:
                instance = instance_dict[ct_id]
                ct = self.field.get_content_type(id=ct_id, using=instance._state.db)
                model = ct.model_class()
                queryset = model._base_manager.using(instance._state.db)

            pk_field = model._meta.pk
            if isinstance(pk_field, CompositePrimaryKey):
                # Decode every reference; malformed ones can't match anything.
                conditions = []
                for fkey in fkeys:
                    decoded = self._decode_reference(pk_field, fkey)
                    if decoded is not None:
                        conditions.append(
                            self.field.composite_pk_filter(pk_field, decoded)
                        )
                if conditions:
                    query = conditions[0]
                    for condition in conditions[1:]:
                        query |= condition
                    queryset = queryset.filter(query)
                else:
                    queryset = queryset.none()
            elif ct_id in custom_queryset_dict:
                queryset = queryset.filter(pk__in=fkeys)
            else:
                queryset = ct.get_all_objects_for_this_type(pk__in=fkeys)

            ret_val.extend(queryset.fetch_mode(instances[0]._state.fetch_mode))

        # For doing the join in Python, we have to match both the FK val and
        # the content type, so we use a callable that returns a (fk, class)
        # pair.
        def gfk_key(obj):
            ct_id = getattr(obj, ct_attname)
            if ct_id is None:
                return None
            else:
                ct = self.field.get_content_type(id=ct_id, using=obj._state.db)
                model = ct.model_class()
                fk_val = getattr(obj, self.field.fk_field)
                pk_field = model._meta.pk
                if isinstance(pk_field, CompositePrimaryKey):
                    decoded = self._decode_reference(pk_field, fk_val)
                    if decoded is None:
                        # A malformed reference can never match a real object.
                        return (INVALID_COMPOSITE_REFERENCE, fk_val), model
                    return decoded, model
                return str(fk_val), model

        def rel_obj_key(obj):
            pk_field = obj._meta.pk
            if isinstance(pk_field, CompositePrimaryKey):
                return (
                    tuple(
                        field.to_python(value)
                        for field, value in zip(pk_field.fields, obj.pk)
                    ),
                    obj.__class__,
                )
            return (pk_field.value_to_string(obj), obj.__class__)

        return (
            ret_val,
            rel_obj_key,
            gfk_key,
            True,
            self.field.name,
            False,
        )

    def __get__(self, instance, cls=None):
        if instance is None:
            return self

        # Don't use getattr(instance, self.ct_field) here because that might
        # reload the same ContentType over and over (#5570). Instead, get the
        # content type ID here, and later when the actual instance is needed,
        # use ContentType.objects.get_for_id(), which has a global cache.
        f = self.field.model._meta.get_field(self.field.ct_field)
        ct_id = getattr(instance, f.attname, None)
        pk_val = getattr(instance, self.field.fk_field)

        rel_obj = self.field.get_cached_value(instance, default=None)
        if rel_obj is None and self.field.is_cached(instance):
            return rel_obj
        if rel_obj is not None:
            ct_match = (
                ct_id
                == self.field.get_content_type(obj=rel_obj, using=instance._state.db).id
            )
            if ct_match and self._cached_object_matches(rel_obj, pk_val):
                return rel_obj
            else:
                rel_obj = None

        instance._state.fetch_mode.fetch(self, instance)
        return self.field.get_cached_value(instance)

    def _cached_object_matches(self, rel_obj, pk_val):
        pk_field = rel_obj._meta.pk
        if isinstance(pk_field, CompositePrimaryKey):
            try:
                return GenericForeignKey.decode_composite_pk(pk_field, pk_val) == tuple(
                    rel_obj.pk
                )
            except (TypeError, ValueError):
                return False
        return pk_field.to_python(pk_val) == rel_obj.pk

    def fetch_one(self, instance):
        f = self.field.model._meta.get_field(self.field.ct_field)
        ct_id = getattr(instance, f.attname, None)
        pk_val = getattr(instance, self.field.fk_field)
        rel_obj = None
        if ct_id is not None:
            ct = self.field.get_content_type(id=ct_id, using=instance._state.db)
            model = ct.model_class()
            pk_field = model._meta.pk
            if isinstance(pk_field, CompositePrimaryKey):
                decoded = self._decode_reference(pk_field, pk_val)
                if decoded is not None:
                    try:
                        rel_obj = (
                            model._base_manager.using(instance._state.db)
                            .filter(self.field.composite_pk_filter(pk_field, decoded))
                            .get()
                        )
                    except ObjectDoesNotExist:
                        rel_obj = None
            else:
                try:
                    rel_obj = ct.get_object_for_this_type(
                        using=instance._state.db, pk=pk_val
                    )
                except ObjectDoesNotExist:
                    pass
            if rel_obj is not None:
                rel_obj._state.fetch_mode = instance._state.fetch_mode
        self.field.set_cached_value(instance, rel_obj)

    def fetch_many(self, instances):
        is_cached = self.field.is_cached
        missing_instances = [i for i in instances if not is_cached(i)]
        return prefetch_related_objects(missing_instances, self.field.name)

    def __set__(self, instance, value):
        ct = None
        fk = None
        if value is not None:
            pk_field = value._meta.pk
            if isinstance(pk_field, CompositePrimaryKey):
                if value._state.adding:
                    raise ValueError(
                        "Cannot assign an unsaved object to a GenericForeignKey."
                    )
                # Encode before touching anything else; an unencodable
                # component must leave the instance attributes untouched.
                fk = GenericForeignKey.encode_composite_pk(pk_field, value.pk)
            else:
                fk = value.pk
            ct = self.field.get_content_type(obj=value)

        setattr(instance, self.field.ct_field, ct)
        setattr(instance, self.field.fk_field, fk)
        self.field.set_cached_value(instance, value)


class GenericRel(ForeignObjectRel):
    """
    Used by GenericRelation to store information about the relation.
    """

    def __init__(
        self,
        field,
        to,
        related_name=None,
        related_query_name=None,
        limit_choices_to=None,
    ):
        super().__init__(
            field,
            to,
            related_name=related_query_name or "+",
            related_query_name=related_query_name,
            limit_choices_to=limit_choices_to,
            on_delete=DO_NOTHING,
        )

    def get_joining_fields(self):
        # When the related model has a CompositePrimaryKey the object id isn't
        # compared against a column at all -- the reference is a JSON array.
        # The whole matching predicate is produced by get_extra_restriction(),
        # so there are no ordinary column-to-column join conditions.
        if isinstance(self.field.model._meta.pk, CompositePrimaryKey):
            return ()
        return super().get_joining_fields()

    def get_split_exclude_lookup(self, selected_col, trimmed_prefix):
        # For a CompositePrimaryKey target the subquery selects the JSON
        # reference column; correlate it with the outer target's composite
        # primary key instead of doing a single-column equality comparison.
        if isinstance(self.field.model._meta.pk, CompositePrimaryKey):
            return CompositeGenericKeyMatch.correlated(
                selected_col, ResolvedOuterRef(trimmed_prefix)
            )
        return None


class GenericRelation(ForeignObject):
    """
    Provide a reverse to a relation created by a GenericForeignKey.
    """

    # Field flags
    auto_created = False
    empty_strings_allowed = False

    many_to_many = False
    many_to_one = False
    one_to_many = True
    one_to_one = False

    rel_class = GenericRel

    mti_inherited = False

    def __init__(
        self,
        to,
        object_id_field="object_id",
        content_type_field="content_type",
        for_concrete_model=True,
        related_query_name=None,
        limit_choices_to=None,
        **kwargs,
    ):
        kwargs["rel"] = self.rel_class(
            self,
            to,
            related_query_name=related_query_name,
            limit_choices_to=limit_choices_to,
        )

        # Reverse relations are always nullable (Django can't enforce that a
        # foreign key on the related model points to this model).
        kwargs["null"] = True
        kwargs["blank"] = True
        kwargs["on_delete"] = models.CASCADE
        kwargs["editable"] = False
        kwargs["serialize"] = False

        # This construct is somewhat of an abuse of ForeignObject. This field
        # represents a relation from pk to object_id field. But, this relation
        # isn't direct, the join is generated reverse along foreign key. So,
        # the from_field is object_id field, to_field is pk because of the
        # reverse join.
        super().__init__(to, from_fields=[object_id_field], to_fields=[], **kwargs)

        self.object_id_field_name = object_id_field
        self.content_type_field_name = content_type_field
        self.for_concrete_model = for_concrete_model

    def check(self, **kwargs):
        return [
            *super().check(**kwargs),
            *self._check_generic_foreign_key_existence(),
        ]

    def _is_matching_generic_foreign_key(self, field):
        """
        Return True if field is a GenericForeignKey whose content type and
        object id fields correspond to the equivalent attributes on this
        GenericRelation.
        """
        return (
            isinstance(field, GenericForeignKey)
            and field.ct_field == self.content_type_field_name
            and field.fk_field == self.object_id_field_name
        )

    def _check_generic_foreign_key_existence(self):
        target = self.remote_field.model
        if isinstance(target, ModelBase):
            fields = target._meta.private_fields
            if any(self._is_matching_generic_foreign_key(field) for field in fields):
                return []
            else:
                return [
                    checks.Error(
                        "The GenericRelation defines a relation with the model "
                        "'%s', but that model does not have a GenericForeignKey."
                        % target._meta.label,
                        obj=self,
                        id="contenttypes.E004",
                    )
                ]
        else:
            return []

    def resolve_related_fields(self):
        self.to_fields = [self.model._meta.pk.name]
        return [
            (
                self.remote_field.model._meta.get_field(self.object_id_field_name),
                self.model._meta.pk,
            )
        ]

    @property
    def related_model_has_composite_pk(self):
        return isinstance(self.model._meta.pk, CompositePrimaryKey)

    def _composite_pk_fields(self):
        return self.model._meta.pk.fields

    def get_local_related_value(self, instance):
        return self.get_instance_value_for_fields(instance, self.foreign_related_fields)

    def get_foreign_related_value(self, instance):
        # We (possibly) need to convert object IDs to the type of the
        # instances' PK in order to match up instances during prefetching.
        return tuple(
            foreign_field.to_python(val)
            for foreign_field, val in zip(
                self.foreign_related_fields,
                self.get_instance_value_for_fields(instance, self.local_related_fields),
            )
        )

    def _get_path_info_with_parent(self, filtered_relation):
        """
        Return the path that joins the current model through any parent models.
        The idea is that if you have a GFK defined on a parent model then we
        need to join the parent model first, then the child model.
        """
        # With an inheritance chain ChildTag -> Tag and Tag defines the
        # GenericForeignKey, and a TaggedItem model has a GenericRelation to
        # ChildTag, then we need to generate a join from TaggedItem to Tag
        # (as Tag.object_id == TaggedItem.pk), and another join from Tag to
        # ChildTag (as that is where the relation is to). Do this by first
        # generating a join to the parent model, then generating joins to the
        # child models.
        path = []
        opts = self.remote_field.model._meta.concrete_model._meta
        parent_opts = opts.get_field(self.object_id_field_name).model._meta
        target = parent_opts.pk
        path.append(
            PathInfo(
                from_opts=self.model._meta,
                to_opts=parent_opts,
                target_fields=(target,),
                join_field=self.remote_field,
                m2m=True,
                direct=False,
                filtered_relation=filtered_relation,
                distinct=self.related_model_has_composite_pk,
            )
        )
        # Collect joins needed for the parent -> child chain. This is easiest
        # to do if we collect joins for the child -> parent chain and then
        # reverse the direction (call to reverse() and use of
        # field.remote_field.get_path_info()).
        parent_field_chain = []
        while parent_opts != opts:
            field = opts.get_ancestor_link(parent_opts.model)
            parent_field_chain.append(field)
            opts = field.remote_field.model._meta
        parent_field_chain.reverse()
        for field in parent_field_chain:
            path.extend(field.remote_field.path_infos)
        return path

    def get_path_info(self, filtered_relation=None):
        opts = self.remote_field.model._meta
        object_id_field = opts.get_field(self.object_id_field_name)
        if object_id_field.model != opts.model:
            return self._get_path_info_with_parent(filtered_relation)
        else:
            target = opts.pk
            return [
                PathInfo(
                    from_opts=self.model._meta,
                    to_opts=opts,
                    target_fields=(target,),
                    join_field=self.remote_field,
                    m2m=True,
                    direct=False,
                    filtered_relation=filtered_relation,
                    distinct=self.related_model_has_composite_pk,
                )
            ]

    def get_reverse_path_info(self, filtered_relation=None):
        opts = self.model._meta
        from_opts = self.remote_field.model._meta
        return [
            PathInfo(
                from_opts=from_opts,
                to_opts=opts,
                target_fields=(opts.pk,),
                join_field=self,
                m2m=False,
                direct=False,
                filtered_relation=filtered_relation,
            )
        ]

    def value_to_string(self, obj):
        qs = getattr(obj, self.name).all()
        return str([instance.pk for instance in qs])

    def contribute_to_class(self, cls, name, **kwargs):
        kwargs["private_only"] = True
        super().contribute_to_class(cls, name, **kwargs)
        self.model = cls
        # Disable the reverse relation for fields inherited by subclasses of a
        # model in multi-table inheritance. The reverse relation points to the
        # field of the base model.
        if self.mti_inherited:
            self.remote_field.related_name = "+"
            self.remote_field.related_query_name = None
        setattr(cls, self.name, ReverseGenericManyToOneDescriptor(self.remote_field))

        # Add get_RELATED_order() and set_RELATED_order() to the model this
        # field belongs to, if the model on the other end of this relation
        # is ordered with respect to its corresponding GenericForeignKey.
        if not cls._meta.abstract:

            def make_generic_foreign_order_accessors(related_model, model):
                if self._is_matching_generic_foreign_key(
                    model._meta.order_with_respect_to
                ):
                    make_foreign_order_accessors(model, related_model)

            lazy_related_operation(
                make_generic_foreign_order_accessors,
                self.model,
                self.remote_field.model,
            )

    def set_attributes_from_rel(self):
        pass

    def get_internal_type(self):
        return "ManyToManyField"

    def get_content_type(self):
        """
        Return the content type associated with this field's model.
        """
        return ContentType.objects.get_for_model(
            self.model, for_concrete_model=self.for_concrete_model
        )

    def get_extra_restriction(self, alias, remote_alias):
        # ``remote_alias`` is the table holding the GenericForeignKey columns
        # (the source); ``alias`` is this relation's model (the target).
        field = self.remote_field.model._meta.get_field(self.content_type_field_name)
        contenttype_pk = self.get_content_type().pk
        lookup = field.get_lookup("exact")(field.get_col(remote_alias), contenttype_pk)
        where = WhereNode([lookup], connector=AND)
        # During split_exclude() the leading join is trimmed away and ``alias``
        # is None; the composite-predicate correlation is added separately by
        # GenericRel.get_split_exclude_lookup() there.
        if alias is not None and self.related_model_has_composite_pk:
            object_id_field = self.remote_field.model._meta.get_field(
                self.object_id_field_name
            )
            where.children.append(
                CompositeGenericKeyMatch.for_aliases(
                    source_alias=remote_alias,
                    object_id_field=object_id_field,
                    target_alias=alias,
                    pk_fields=self._composite_pk_fields(),
                )
            )
        return where

    def bulk_related_objects(self, objs, using=DEFAULT_DB_ALIAS):
        """
        Return all objects related to ``objs`` via this ``GenericRelation``.
        """
        pks = []
        for obj in objs:
            pk = obj.pk
            if isinstance(obj._meta.pk, CompositePrimaryKey):
                pk = GenericForeignKey.encode_composite_pk(obj._meta.pk, pk)
            pks.append(pk)
        return self.remote_field.model._base_manager.db_manager(using).filter(
            **{
                "%s__pk"
                % self.content_type_field_name: ContentType.objects.db_manager(using)
                .get_for_model(self.model, for_concrete_model=self.for_concrete_model)
                .pk,
                "%s__in" % self.object_id_field_name: pks,
            }
        )


class ReverseGenericManyToOneDescriptor(ReverseManyToOneDescriptor):
    """
    Accessor to the related objects manager on the one-to-many relation created
    by GenericRelation.

    In the example::

        class Post(Model):
            comments = GenericRelation(Comment)

    ``post.comments`` is a ReverseGenericManyToOneDescriptor instance.
    """

    @cached_property
    def related_manager_cls(self):
        return create_generic_related_manager(
            self.rel.model._default_manager.__class__,
            self.rel,
        )


def create_generic_related_manager(superclass, rel):
    """
    Factory function to create a manager that subclasses another manager
    (generally the default manager of a given model) and adds behaviors
    specific to generic relations.
    """

    class GenericRelatedObjectManager(superclass, AltersData):
        def __init__(self, instance=None):
            super().__init__()

            self.instance = instance

            self.model = rel.model
            self.get_content_type = functools.partial(
                ContentType.objects.db_manager(instance._state.db).get_for_model,
                for_concrete_model=rel.field.for_concrete_model,
            )
            self.content_type = self.get_content_type(instance)
            self.content_type_field_name = rel.field.content_type_field_name
            self.object_id_field_name = rel.field.object_id_field_name
            self.prefetch_cache_name = rel.field.attname
            self.pk_val = instance.pk
            if isinstance(instance._meta.pk, CompositePrimaryKey):
                # The reference is stored as a JSON array text.
                self.pk_val = GenericForeignKey.encode_composite_pk(
                    instance._meta.pk, instance.pk
                )

            self.core_filters = {
                "%s__pk" % self.content_type_field_name: self.content_type.id,
                self.object_id_field_name: self.pk_val,
            }

        def __call__(self, *, manager):
            manager = getattr(self.model, manager)
            manager_class = create_generic_related_manager(manager.__class__, rel)
            return manager_class(instance=self.instance)

        do_not_call_in_templates = True

        def __str__(self):
            return repr(self)

        def _apply_rel_filters(self, queryset):
            """
            Filter the queryset for the instance this manager is bound to.
            """
            db = self._db or router.db_for_read(self.model, instance=self.instance)
            with queryset._avoid_cloning():
                return (
                    queryset.using(db)
                    .fetch_mode(self.instance._state.fetch_mode)
                    .filter(**self.core_filters)
                )

        def _remove_prefetched_objects(self):
            try:
                self.instance._prefetched_objects_cache.pop(self.prefetch_cache_name)
            except (AttributeError, KeyError):
                pass  # nothing to clear from cache

        def get_queryset(self):
            try:
                return self.instance._prefetched_objects_cache[self.prefetch_cache_name]
            except (AttributeError, KeyError):
                queryset = super().get_queryset()
                return self._apply_rel_filters(queryset)

        def get_prefetch_querysets(self, instances, querysets=None):
            _cloning_disabled = False
            if querysets:
                if len(querysets) != 1:
                    raise ValueError(
                        "querysets argument of get_prefetch_querysets() should have a "
                        "length of 1."
                    )
                queryset = querysets[0]
            else:
                _cloning_disabled = True
                queryset = super().get_queryset()._disable_cloning()
            queryset._add_hints(instance=instances[0])
            queryset = queryset.using(queryset._db or self._db)
            pk_field = instances[0]._meta.pk
            is_composite = isinstance(pk_field, CompositePrimaryKey)

            def instance_ref(obj):
                pk = obj.pk
                if is_composite:
                    return GenericForeignKey.encode_composite_pk(obj._meta.pk, pk)
                return pk

            # Group instances by content types.
            content_type_queries = [
                models.Q.create(
                    [
                        (f"{self.content_type_field_name}__pk", content_type_id),
                        (
                            f"{self.object_id_field_name}__in",
                            {instance_ref(obj) for obj in objs},
                        ),
                    ]
                )
                for content_type_id, objs in itertools.groupby(
                    sorted(instances, key=lambda obj: self.get_content_type(obj).pk),
                    lambda obj: self.get_content_type(obj).pk,
                )
            ]
            query = models.Q.create(content_type_queries, connector=models.Q.OR)

            def object_id_converter(value):
                if is_composite:
                    try:
                        return GenericForeignKey.decode_composite_pk(pk_field, value)
                    except (TypeError, ValueError):
                        return value
                return pk_field.to_python(value)

            content_type_id_field_name = "%s_id" % self.content_type_field_name
            queryset = queryset.filter(query)
            # Restore subsequent cloning operations.
            if _cloning_disabled:
                queryset._enable_cloning()
            return (
                queryset,
                lambda relobj: (
                    object_id_converter(getattr(relobj, self.object_id_field_name)),
                    getattr(relobj, content_type_id_field_name),
                ),
                lambda obj: (
                    object_id_converter(instance_ref(obj)),
                    self.get_content_type(obj).pk,
                ),
                False,
                self.prefetch_cache_name,
                False,
            )

        def add(self, *objs, bulk=True):
            self._remove_prefetched_objects()
            db = router.db_for_write(self.model, instance=self.instance)

            def check_and_update_obj(obj):
                if not isinstance(obj, self.model):
                    raise TypeError(
                        "'%s' instance expected, got %r"
                        % (self.model._meta.object_name, obj)
                    )
                setattr(obj, self.content_type_field_name, self.content_type)
                setattr(obj, self.object_id_field_name, self.pk_val)

            if bulk:
                pks = []
                for obj in objs:
                    if obj._state.adding or obj._state.db != db:
                        raise ValueError(
                            "%r instance isn't saved. Use bulk=False or save "
                            "the object first." % obj
                        )
                    check_and_update_obj(obj)
                    pks.append(obj.pk)

                self.model._base_manager.using(db).filter(pk__in=pks).update(
                    **{
                        self.content_type_field_name: self.content_type,
                        self.object_id_field_name: self.pk_val,
                    }
                )
            else:
                with transaction.atomic(using=db, savepoint=False):
                    for obj in objs:
                        check_and_update_obj(obj)
                        obj.save()

        add.alters_data = True

        async def aadd(self, *objs, bulk=True):
            return await sync_to_async(self.add)(*objs, bulk=bulk)

        aadd.alters_data = True

        def remove(self, *objs, bulk=True):
            if not objs:
                return
            self._clear(self.filter(pk__in=[o.pk for o in objs]), bulk)

        remove.alters_data = True

        async def aremove(self, *objs, bulk=True):
            return await sync_to_async(self.remove)(*objs, bulk=bulk)

        aremove.alters_data = True

        def clear(self, *, bulk=True):
            self._clear(self, bulk)

        clear.alters_data = True

        async def aclear(self, *, bulk=True):
            return await sync_to_async(self.clear)(bulk=bulk)

        aclear.alters_data = True

        def _clear(self, queryset, bulk):
            self._remove_prefetched_objects()
            db = router.db_for_write(self.model, instance=self.instance)
            queryset = queryset.using(db)
            if bulk:
                # `QuerySet.delete()` creates its own atomic block which
                # contains the `pre_delete` and `post_delete` signal handlers.
                queryset.delete()
            else:
                with transaction.atomic(using=db, savepoint=False):
                    for obj in queryset:
                        obj.delete()

        _clear.alters_data = True

        def set(self, objs, *, bulk=True, clear=False):
            # Force evaluation of `objs` in case it's a queryset whose value
            # could be affected by `manager.clear()`. Refs #19816.
            objs = tuple(objs)

            db = router.db_for_write(self.model, instance=self.instance)
            with transaction.atomic(using=db, savepoint=False):
                if clear:
                    self.clear()
                    self.add(*objs, bulk=bulk)
                else:
                    old_objs = set(self.using(db).all())
                    new_objs = []
                    for obj in objs:
                        if obj in old_objs:
                            old_objs.remove(obj)
                        else:
                            new_objs.append(obj)

                    self.remove(*old_objs)
                    self.add(*new_objs, bulk=bulk)

        set.alters_data = True

        async def aset(self, objs, *, bulk=True, clear=False):
            return await sync_to_async(self.set)(objs, bulk=bulk, clear=clear)

        aset.alters_data = True

        def create(self, **kwargs):
            self._remove_prefetched_objects()
            kwargs[self.content_type_field_name] = self.content_type
            kwargs[self.object_id_field_name] = self.pk_val
            db = router.db_for_write(self.model, instance=self.instance)
            return super().using(db).create(**kwargs)

        create.alters_data = True

        async def acreate(self, **kwargs):
            return await sync_to_async(self.create)(**kwargs)

        acreate.alters_data = True

        def get_or_create(self, **kwargs):
            kwargs[self.content_type_field_name] = self.content_type
            kwargs[self.object_id_field_name] = self.pk_val
            db = router.db_for_write(self.model, instance=self.instance)
            return super().using(db).get_or_create(**kwargs)

        get_or_create.alters_data = True

        async def aget_or_create(self, **kwargs):
            return await sync_to_async(self.get_or_create)(**kwargs)

        aget_or_create.alters_data = True

        def update_or_create(self, **kwargs):
            kwargs[self.content_type_field_name] = self.content_type
            kwargs[self.object_id_field_name] = self.pk_val
            db = router.db_for_write(self.model, instance=self.instance)
            return super().using(db).update_or_create(**kwargs)

        update_or_create.alters_data = True

        async def aupdate_or_create(self, **kwargs):
            return await sync_to_async(self.update_or_create)(**kwargs)

        aupdate_or_create.alters_data = True

    return GenericRelatedObjectManager
