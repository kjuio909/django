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
from django.db import DEFAULT_DB_ALIAS, models, router, transaction
from django.db.models import DO_NOTHING, ForeignObject, ForeignObjectRel
from django.db.models.base import ModelBase, make_foreign_order_accessors
from django.db.models.constants import LOOKUP_SEP
from django.db.models.deletion import DatabaseOnDelete
from django.db.models.expressions import Func, ResolvedOuterRef, Value
from django.db.models.fields import Field, TextField
from django.db.models.fields.composite import CompositePrimaryKey
from django.db.models.fields.mixins import FieldCacheMixin
from django.db.models.fields.related import (
    ReverseManyToOneDescriptor,
    lazy_related_operation,
)
from django.db.models.functions import Concat
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


class JSONQuote(Func):
    """
    Return the JSON text representation of a single SQL value, matching
    json.dumps() for that value (e.g. 1 -> 1, "x" -> "\"x\"").
    """

    function = "JSON_QUOTE"
    arity = 1
    output_field = TextField()

    def as_mysql(self, compiler, connection, **extra_context):
        # MySQL's JSON_QUOTE() quotes its argument as a string, while
        # CAST(... AS JSON) preserves the JSON type of the value.
        return self.as_sql(
            compiler, connection, template="CAST(%(expressions)s AS JSON)",
            **extra_context,
        )

    def as_postgresql(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler, connection, function="TO_JSON", **extra_context
        )


def encode_composite_reference(expressions):
    """
    Return an expression evaluating to the JSON array text produced by
    GenericForeignKey.encode_composite_pk() for the given SQL expressions,
    so that a stored reference can be matched with a database-level join.
    """
    parts = [Value("[")]
    for index, expression in enumerate(expressions):
        if index:
            parts.append(Value(", "))
        parts.append(JSONQuote(expression))
    parts.append(Value("]"))
    return Concat(*parts, output_field=TextField())


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

    def get_joining_fields(self, reverse_join=False):
        if isinstance(self.model._meta.pk, CompositePrimaryKey):
            # A reference to a composite primary key is stored as JSON array
            # text in the single object_id column, so there are no column
            # pairs to equate; get_extra_restriction() provides the join
            # condition instead.
            return ()
        return super().get_joining_fields(reverse_join=reverse_join)

    def get_exclude_correlation_lookup(self, select_field, col, trimmed_prefix):
        pk = self.model._meta.pk
        object_id_field = self.remote_field.model._meta.get_field(
            self.object_id_field_name
        )
        is_stored_reference = (
            isinstance(pk, CompositePrimaryKey) and select_field is object_id_field
        )
        if not is_stored_reference:
            return super().get_exclude_correlation_lookup(
                select_field, col, trimmed_prefix
            )
        # The trimmed subquery selects the stored JSON array text reference;
        # correlate it with the encoding of the outer query's primary key
        # components, resolved along the same path as trimmed_prefix.
        prefix, _, _ = trimmed_prefix.rpartition(LOOKUP_SEP)
        reference = encode_composite_reference(
            [
                ResolvedOuterRef(
                    f"{prefix}{LOOKUP_SEP}{field.name}" if prefix else field.name
                )
                for field in pk.fields
            ]
        )
        return select_field.get_lookup("exact")(col, reference)

    def get_extra_restriction(self, alias, remote_alias):
        field = self.remote_field.model._meta.get_field(self.content_type_field_name)
        contenttype_pk = self.get_content_type().pk
        lookup = field.get_lookup("exact")(field.get_col(remote_alias), contenttype_pk)
        conditions = [lookup]
        pk = self.model._meta.pk
        if isinstance(pk, CompositePrimaryKey) and alias is not None:
            # Match the stored JSON array text reference against the encoding
            # of this model's primary key columns. A reference that isn't a
            # well-formed encoding simply never compares equal.
            object_id_field = self.remote_field.model._meta.get_field(
                self.object_id_field_name
            )
            reference = encode_composite_reference(
                [component.get_col(alias) for component in pk.fields]
            )
            conditions.append(
                object_id_field.get_lookup("exact")(
                    object_id_field.get_col(remote_alias), reference
                )
            )
        return WhereNode(conditions, connector=AND)

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
            pk_field = instance._meta.pk
            self.is_composite_pk = isinstance(pk_field, CompositePrimaryKey)
            if self.is_composite_pk:
                # The reference is stored as JSON array text. Encode it while
                # building the manager: an unencodable component must prevent
                # the relationship from being read or mutated.
                self.pk_val = GenericForeignKey.encode_composite_pk(
                    pk_field, instance.pk
                )
            else:
                self.pk_val = instance.pk
            self.content_type_attname = rel.model._meta.get_field(
                rel.field.content_type_field_name
            ).attname

            self.core_filters = {
                "%s__pk" % self.content_type_field_name: self.content_type.id,
                self.object_id_field_name: self.pk_val,
            }

        def _check_target(self):
            """
            Ensure the composite-primary-key target instance has been saved and
            still exists. Mutating the relationship of an unsaved or deleted
            target would store a reference that can never resolve.
            """
            if not self.is_composite_pk:
                return
            if self.instance._state.adding or not self.instance._is_pk_set():
                raise ValueError(
                    f"{self.instance._meta.object_name!r} instance needs to have "
                    "a primary key value before this relationship can be used."
                )
            db = router.db_for_read(
                self.instance.__class__, instance=self.instance
            )
            if not self.instance.__class__._base_manager.using(db).filter(
                pk=self.instance.pk
            ).exists():
                raise self.instance.DoesNotExist(
                    "%r with the composite primary key %r does not exist."
                    % (self.instance, self.pk_val)
                )

        def _source_rows(self, objs, db):
            """
            Return a mapping of source primary key to the
            (content type id, object id) pair currently stored for each of
            ``objs`` that exists. Sources are read from their own database,
            which matters for bulk=False where related objects may live on a
            different database than the target.
            """
            objs_by_db = defaultdict(list)
            for obj in objs:
                objs_by_db[obj._state.db or db].append(obj)
            current = {}
            for using, using_objs in objs_by_db.items():
                pks = {obj.pk for obj in using_objs}
                rows = (
                    self.model._base_manager.using(using)
                    .filter(pk__in=pks)
                    .only(self.content_type_field_name, self.object_id_field_name)
                )
                for row in rows:
                    current[row.pk] = (
                        getattr(row, self.content_type_attname),
                        getattr(row, self.object_id_field_name),
                    )
            return current

        def _check_source_instances(self, objs, db, *, saved_only):
            """Validate the source instances before any database write."""
            for obj in objs:
                if not isinstance(obj, self.model):
                    raise TypeError(
                        "'%s' instance expected, got %r"
                        % (self.model._meta.object_name, obj)
                    )
                if saved_only and (obj._state.adding or obj._state.db != db):
                    raise ValueError(
                        "%r instance isn't saved. Use bulk=False or save "
                        "the object first." % obj
                    )

        def _decode_source_reference(self, object_id):
            """
            Decode a source's stored reference to this composite target.
            Return None for a missing reference; raise ValueError if the
            content type matches but the object identifier isn't a valid
            reference to this model.
            """
            if object_id is None:
                return None
            try:
                return GenericForeignKey.decode_composite_pk(
                    self.instance._meta.pk, object_id
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "%r has an invalid composite primary key reference %r."
                    % (self.model._meta.object_name, object_id)
                ) from exc

        def _validate_sources_for_add(self, objs, db):
            """
            Validate all sources for an add before anything is written.

            Return the set of primary keys that must be updated. A source is
            skipped (add stays idempotent) when it already points at this
            target. A source pointing at another object of the same content
            type is reassigned (standard ``add()`` semantics); a source with
            another content type or an unreadable reference is rejected.

            Saved-state and database validity are the caller's responsibility
            (bulk=False may receive unsaved or cross-database objects).
            """
            self._check_source_instances(objs, db, saved_only=False)
            current = self._source_rows(objs, db)
            update_pks = set()
            for obj in objs:
                if obj.pk not in current:
                    raise ValueError(
                        "%r instance isn't saved. Use bulk=False or save the "
                        "object first." % obj
                    )
                content_type_id, object_id = current[obj.pk]
                if content_type_id == self.content_type.id and object_id == self.pk_val:
                    continue
                if content_type_id is None and object_id is None:
                    update_pks.add(obj.pk)
                    continue
                if content_type_id is None or content_type_id != self.content_type.id:
                    raise ValueError(
                        "%r has a different content type and cannot be assigned "
                        "to %r." % (obj, self.instance)
                    )
                if object_id is None:
                    # A content type without an object identifier is a dangling
                    # (unresolved) reference; assigning the source repairs it.
                    update_pks.add(obj.pk)
                    continue
                # Validate the stored reference: same content type with an
                # unreadable object identifier is an invalid source and fails
                # the whole operation. A valid reference to another object of
                # this content type is reassigned, which is the documented
                # add() behavior.
                self._decode_source_reference(object_id)
                update_pks.add(obj.pk)
            return update_pks

        def _validate_sources_for_remove(self, objs, db):
            """
            Validate all sources for a remove before anything is deleted.

            Only sources currently related to this target (full composite key
            match) are returned for deletion. Sources related to another
            target are ignored (removing a source that isn't part of the
            relation is a no-op); unsaved sources, sources with another
            content type or an object identifier that isn't a valid JSON
            reference are rejected so they can never be modified.
            """
            self._check_source_instances(objs, db, saved_only=True)
            current = self._source_rows(objs, db)
            remove_pks = set()
            for obj in objs:
                if obj.pk not in current:
                    # The row no longer exists (e.g. it was already removed):
                    # removing a saved source that isn't there is a no-op.
                    continue
                content_type_id, object_id = current[obj.pk]
                if content_type_id is None:
                    continue
                if content_type_id != self.content_type.id:
                    raise ValueError(
                        "%r has a different content type and cannot be removed "
                        "from %r." % (obj, self.instance)
                    )
                if object_id == self.pk_val:
                    remove_pks.add(obj.pk)
                else:
                    # A well-formed reference to another target simply isn't
                    # part of this relation; a malformed reference is invalid
                    # input and must fail the whole operation.
                    self._decode_source_reference(object_id)
            return remove_pks

        def _assign_to_target(self, objs):
            """Point the given in-memory sources at this target."""
            for obj in objs:
                setattr(obj, self.content_type_field_name, self.content_type)
                setattr(obj, self.object_id_field_name, self.pk_val)

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

            if self.is_composite_pk:
                self._composite_add(objs, db, bulk)
                return

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

        def _composite_write_add(self, objs, db, bulk):
            """
            Point already-validated sources at this target.

            With bulk=True the sources are reassigned with one bulk update.
            With bulk=False every source is saved individually (inserting
            unsaved ones and updating saved ones), matching the historical
            bulk=False behavior, including for objects on another database.
            """
            if not objs:
                return
            if bulk:
                update_pks = {obj.pk for obj in objs}
                self.model._base_manager.using(db).filter(pk__in=update_pks).update(
                    **{
                        self.content_type_field_name: self.content_type,
                        self.object_id_field_name: self.pk_val,
                    }
                )
            else:
                for obj in objs:
                    self._assign_to_target([obj])
                    obj.save()

        def _composite_add(self, objs, db, bulk):
            """
            Validate every source before writing, then point all of them at
            this target in a single atomic operation. Re-adding a source that
            already belongs to this target is a no-op; unsaved sources (with
            bulk=True), sources with another content type or an unreadable
            object identifier fail the operation before a single row is
            touched.
            """
            self._check_target()
            if not objs:
                return
            self._check_source_instances(objs, db, saved_only=bulk)
            unsaved_objs = tuple(obj for obj in objs if obj._state.adding)
            saved_objs = tuple(obj for obj in objs if not obj._state.adding)
            # Read-only validation of every saved source first.
            update_pks = self._validate_sources_for_add(saved_objs, db)
            write_saved = tuple(obj for obj in saved_objs if obj.pk in update_pks)
            write_objs = write_saved if bulk else (*write_saved, *unsaved_objs)
            if write_objs:
                if bulk:
                    # A single bulk update is atomic on its own.
                    self._composite_write_add(write_objs, db, bulk)
                else:
                    with transaction.atomic(using=db, savepoint=False):
                        self._composite_write_add(write_objs, db, bulk)
            # The sources are mutated in memory after the write, once it is
            # known the whole operation succeeded.
            self._assign_to_target(write_saved if bulk else objs)

        add.alters_data = True

        async def aadd(self, *objs, bulk=True):
            return await sync_to_async(self.add)(*objs, bulk=bulk)

        aadd.alters_data = True

        def remove(self, *objs, bulk=True):
            if not objs:
                return
            if self.is_composite_pk:
                db = router.db_for_write(self.model, instance=self.instance)
                self._check_target()
                # Validate every source before deleting anything. QuerySet
                # delete() (or the per-object loop below) is atomic itself.
                remove_pks = self._validate_sources_for_remove(objs, db)
                if remove_pks:
                    self._clear(self.using(db).filter(pk__in=remove_pks), bulk)
                return
            self._clear(self.filter(pk__in=[o.pk for o in objs]), bulk)

        remove.alters_data = True

        async def aremove(self, *objs, bulk=True):
            return await sync_to_async(self.remove)(*objs, bulk=bulk)

        aremove.alters_data = True

        def clear(self, *, bulk=True):
            if self.is_composite_pk:
                self._check_target()
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

        def _composite_set(self, objs, db, bulk, clear):
            """
            Validate the whole replacement before writing anything, then
            delete the sources that leave the relation and point the new
            sources at this target in a single transaction.
            """
            self._check_target()
            self._check_source_instances(objs, db, saved_only=bulk)
            unsaved_objs = tuple(obj for obj in objs if obj._state.adding)
            saved_objs = tuple(obj for obj in objs if not obj._state.adding)
            # Read-only validation of the new sources happens before any row
            # is deleted, so a failure can never leave a partially replaced
            # relation.
            update_pks = self._validate_sources_for_add(saved_objs, db)
            write_saved = tuple(obj for obj in saved_objs if obj.pk in update_pks)
            with transaction.atomic(using=db, savepoint=False):
                if clear:
                    # Remove every current source except those that are part
                    # of the replacement set; deleting those first would make
                    # it impossible to re-add them in the same operation.
                    keep_pks = {obj.pk for obj in saved_objs}
                    stale = self.using(db)
                    if keep_pks:
                        stale = stale.exclude(pk__in=keep_pks)
                    self._clear(stale, bulk)
                else:
                    old_objs = set(self.using(db).all())
                    for obj in objs:
                        # Unsaved objects can't be part of the current
                        # relation and have no hashable primary key yet.
                        if not obj._state.adding and obj in old_objs:
                            old_objs.remove(obj)
                    # Every object in old_objs was read through this
                    # manager's core_filters (exact content type and object
                    # identifier), so each one is related to this target.
                    if old_objs:
                        self._clear(
                            self.using(db).filter(pk__in=[obj.pk for obj in old_objs]),
                            bulk,
                        )
                self._composite_write_add(
                    write_saved if bulk else (*write_saved, *unsaved_objs),
                    db,
                    bulk,
                )
            self._assign_to_target(write_saved if bulk else objs)

        def set(self, objs, *, bulk=True, clear=False):
            # Force evaluation of `objs` in case it's a queryset whose value
            # could be affected by `manager.clear()`. Refs #19816.
            objs = tuple(objs)

            db = router.db_for_write(self.model, instance=self.instance)
            if self.is_composite_pk:
                self._remove_prefetched_objects()
                self._composite_set(objs, db, bulk, clear)
                return
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
            if self.is_composite_pk:
                self._check_target()
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
            if self.is_composite_pk:
                self._check_target()
            kwargs[self.content_type_field_name] = self.content_type
            kwargs[self.object_id_field_name] = self.pk_val
            db = router.db_for_write(self.model, instance=self.instance)
            return super().using(db).get_or_create(**kwargs)

        get_or_create.alters_data = True

        async def aget_or_create(self, **kwargs):
            return await sync_to_async(self.get_or_create)(**kwargs)

        aget_or_create.alters_data = True

        def update_or_create(self, **kwargs):
            if self.is_composite_pk:
                self._check_target()
            kwargs[self.content_type_field_name] = self.content_type
            kwargs[self.object_id_field_name] = self.pk_val
            db = router.db_for_write(self.model, instance=self.instance)
            return super().using(db).update_or_create(**kwargs)

        update_or_create.alters_data = True

        async def aupdate_or_create(self, **kwargs):
            return await sync_to_async(self.update_or_create)(**kwargs)

        aupdate_or_create.alters_data = True

    return GenericRelatedObjectManager
