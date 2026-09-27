import uuid

from django.db import models


class Tenant(models.Model):
    name = models.CharField(max_length=10, default="", blank=True)


class Token(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "id")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tokens")
    id = models.SmallIntegerField()
    secret = models.CharField(max_length=10, default="", blank=True)


class AbstractUser(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "id")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    email = models.EmailField(unique=True)
    id = models.SmallIntegerField(unique=True)
    tags = models.ManyToManyField("Tag", related_name="users")
    friends = models.ManyToManyField("self")
    posts = models.ManyToManyField("Post", related_name="authors")
    labels = models.ManyToManyField("Label", related_name="users")

    class Meta:
        abstract = True


class User(AbstractUser):
    pass


class Comment(models.Model):
    pk = models.CompositePrimaryKey("tenant", "id")
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    id = models.SmallIntegerField(unique=True, db_column="comment_id")
    user_id = models.SmallIntegerField(null=True)
    user = models.ForeignObject(
        User,
        on_delete=models.CASCADE,
        from_fields=("tenant_id", "user_id"),
        to_fields=("tenant_id", "id"),
        related_name="comments",
        null=True,
    )
    text = models.TextField(default="", blank=True)
    integer = models.IntegerField(default=0)


class Post(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "id")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, default=1)
    id = models.UUIDField(default=uuid.uuid4)


class PostDbDefault(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "id")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    id = models.IntegerField(db_default=1)


class TimeStamped(models.Model):
    pk = models.CompositePrimaryKey("id", "created")
    id = models.SmallIntegerField(unique=True)
    created = models.DateTimeField(auto_now_add=True)
    text = models.TextField(default="", blank=True)


class Tag(models.Model):
    name = models.CharField(max_length=50)


class Label(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "slug")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    slug = models.CharField(max_length=30)


class Board(models.Model):
    name = models.CharField(max_length=50)
    members = models.ManyToManyField("Member", related_name="boards")


class Member(models.Model):
    """
    Composite primary key whose second component is not globally unique:
    objects may share one component and must still be distinguished.
    """

    pk = models.CompositePrimaryKey("tenant_id", "id")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    id = models.SmallIntegerField()
    name = models.CharField(max_length=50, default="")
    tags = models.ManyToManyField(Tag, related_name="tagged_members")
    friends = models.ManyToManyField("self")
    posts = models.ManyToManyField(Post, related_name="member_authors")
    labels = models.ManyToManyField(Label, related_name="members")
