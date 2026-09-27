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

    class Meta:
        abstract = True


class User(AbstractUser):
    # A many-to-many relation declared on a model with a composite primary
    # key to a single-primary-key model.
    tags = models.ManyToManyField("Tag", related_name="tagged_users")
    # A non-symmetrical self-referential many-to-many relation on a model
    # with a composite primary key.
    followers = models.ManyToManyField(
        "self", symmetrical=False, related_name="following"
    )


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
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, default=1)
    id = models.IntegerField(db_default=1)


class TimeStamped(models.Model):
    pk = models.CompositePrimaryKey("id", "created")
    id = models.SmallIntegerField(unique=True)
    created = models.DateTimeField(auto_now_add=True)
    text = models.TextField(default="", blank=True)


class Tag(models.Model):
    title = models.CharField(max_length=50)


class PostTag(models.Model):
    # The auto-generated intermediary model of a many-to-many relation to a
    # model with a composite primary key stores every component of the key.
    posts = models.ManyToManyField(Post, related_name="post_tags")
    users = models.ManyToManyField(User, related_name="post_tag_set")


class Role(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "code")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    code = models.SlugField(max_length=20)
    users = models.ManyToManyField(User, related_name="roles")


class Country(models.Model):
    # A composite primary key made of free-text components, used to verify
    # that unusual key values round-trip through a many-to-many relation.
    pk = models.CompositePrimaryKey("region", "code")
    region = models.CharField(max_length=30)
    code = models.CharField(max_length=30)
    teams = models.ManyToManyField("Team", related_name="countries")


class Team(models.Model):
    name = models.CharField(max_length=30)
