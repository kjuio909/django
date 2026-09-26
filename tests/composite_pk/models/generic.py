from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models

from .tenant import Tenant


class Entry(models.Model):
    """
    A source ("report") row that generically points at any target.

    The reference is stored as text: a plain value for single-field primary
    keys, and a JSON array for composite primary keys.
    """

    label = models.SlugField()
    content_type = models.ForeignKey(ContentType, models.CASCADE)
    object_id = models.TextField(null=True)
    content_object = GenericForeignKey()

    class Meta:
        ordering = ["id"]


class Widget(models.Model):
    """A composite-primary-key target exposing its sources generically."""

    pk = models.CompositePrimaryKey("tenant_id", "id")
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="widgets"
    )
    id = models.CharField(max_length=40)
    name = models.CharField(max_length=40)

    entries = GenericRelation(Entry, related_query_name="widget")


class Gadget(models.Model):
    """A single-field-primary-key target sharing the same source model."""

    name = models.CharField(max_length=40)

    entries = GenericRelation(Entry, related_query_name="gadget")
