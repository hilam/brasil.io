import hashlib
import random
import string
from collections import OrderedDict
from textwrap import dedent
from urllib.parse import urlparse

import django.contrib.postgres.indexes as pg_indexes
import django.db.models.indexes as django_indexes
from cachalot.api import invalidate
from django.contrib.postgres.fields import ArrayField, JSONField
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVectorField
from django.db import connection, models, transaction
from django.db.models import F
from django.db.models.signals import post_delete, pre_delete
from django.db.utils import ProgrammingError
from django.urls import reverse
from markdownx.models import MarkdownxField
from rows import fields as rows_fields

from core import data_models, dynamic_models
from core.filters import DynamicModelFilterProcessor

DYNAMIC_MODEL_REGISTRY = {}


def make_index_name(tablename, index_type, fields):
    idx_hash = hashlib.md5(f'{tablename} {index_type} {", ".join(sorted(fields))}'.encode("ascii")).hexdigest()
    tablename = tablename.replace("data_", "").replace("-", "")[:12]
    return f"idx_{tablename}_{index_type[0]}{idx_hash[-12:]}"


class DatasetTableModelMixin:
    """Brasil.IO-specific methods for dataset tables' dynamic Models"""

    @classmethod
    def tablename(cls):
        return cls._meta.db_table

    @classmethod
    def analyse_table(cls):
        with connection.cursor() as cursor:
            cursor.execute("VACUUM ANALYSE {}".format(cls.tablename()))

    @classmethod
    def get_trigger_name(cls):
        return f"tgr_tsv_{cls.tablename()}"

    @classmethod
    def create_triggers(cls):
        trigger_name = cls.get_trigger_name()
        fieldnames = ", ".join(cls.extra["search"])
        query = dedent(
            f"""
            CREATE TRIGGER {trigger_name}
                BEFORE INSERT OR UPDATE
                ON {cls.tablename()}
                FOR EACH ROW EXECUTE PROCEDURE
                tsvector_update_trigger(search_data, 'pg_catalog.portuguese', {fieldnames})
        """
        ).strip()
        # TODO: replace pg_catalog.portuguese with dataset language
        with connection.cursor() as cursor:
            cursor.execute(query)


class DatasetTableModelQuerySet(models.QuerySet):
    def search(self, search_query):
        qs = self
        search_fields = self.model.extra["search"]
        if search_query and search_fields:
            words = search_query.split()
            config = "pg_catalog.portuguese"  # TODO: get from self.model.extra
            query = None
            for word in set(words):
                if not word:
                    continue
                if query is None:
                    query = SearchQuery(word, config=config)
                else:
                    query = query & SearchQuery(word, config=config)
            qs = qs.annotate(search_rank=SearchRank(F("search_data"), query)).filter(search_data=query)
            # Using `qs.query.add_ordering` will APPEND ordering fields instead
            # of OVERWRITTING (as in `qs.order_by`).
            qs.query.add_ordering("-search_rank")
        return qs

    def apply_filters(self, filtering):
        # TODO: filtering must be based on field's settings, not on models
        # settings.
        model_filtering = self.model.extra["filtering"]
        processor = DynamicModelFilterProcessor(filtering, model_filtering)
        return self.filter(**processor.filters)

    def apply_ordering(self, query):
        qs = self
        # TODO: may use Model's meta "ordering" instead of extra["ordering"]
        model_ordering = self.model.extra["ordering"]
        model_filtering = self.model.extra["filtering"]
        allowed_fields = set(model_ordering + model_filtering)
        clean_allowed = [field.replace("-", "").strip().lower() for field in allowed_fields]
        ordering_query = [field for field in query if field.replace("-", "") in clean_allowed]
        # Using `qs.query.add_ordering` will APPEND ordering fields instead of
        # OVERWRITTING (as in `qs.order_by`).
        if ordering_query:
            qs.query.add_ordering(*ordering_query)
        elif model_ordering:
            qs.query.add_ordering(*model_ordering)

        return qs

    def filter_by_querystring(self, querystring):
        query, search_query, order_by = self.parse_querystring(querystring)
        return self.composed_query(query, search_query, order_by)

    def parse_querystring(self, querystring):
        query = querystring.copy()
        order_by = query.pop("order-by", [""])
        order_by = [field.strip().lower() for field in order_by[0].split(",") if field.strip()]
        search_query = query.pop("search", [""])[0]
        query = {key: value for key, value in query.items() if value}
        return query, search_query, order_by

    def composed_query(self, filter_query=None, search_query=None, order_by=None):
        qs = self
        if search_query:
            qs = qs.search(search_query)
        if filter_query:
            qs = qs.apply_filters(filter_query)
        return qs.apply_ordering(order_by or [])

    def count(self):
        if getattr(self, "_count", None) is not None:
            return self._count

        query = self.query
        if not query.where:  # TODO: check groupby etc.
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT reltuples FROM pg_class WHERE relname = %s", [query.model._meta.db_table],
                    )
                    self._count = int(cursor.fetchone()[0])
            except Exception:
                self._count = super().count()
        else:
            self._count = super().count()

        return self._count


class Dataset(models.Model):
    author_name = models.CharField(max_length=255, null=False, blank=False)
    author_url = models.URLField(max_length=2000, null=True, blank=True)
    code_url = models.URLField(max_length=2000, null=False, blank=False)
    description = models.TextField(null=False, blank=False)
    icon = models.CharField(max_length=31, null=False, blank=False)
    license_name = models.CharField(max_length=255, null=False, blank=False)
    license_url = models.URLField(max_length=2000, null=False, blank=False)
    name = models.CharField(max_length=255, null=False, blank=False)
    show = models.BooleanField(null=False, blank=False, default=False)
    slug = models.SlugField(max_length=50, null=False, blank=False)
    source_name = models.CharField(max_length=255, null=False, blank=False)
    source_url = models.URLField(max_length=2000, null=False, blank=False)

    @property
    def tables(self):
        # By now we're ignoring version - just take the last one
        version = self.get_last_version()
        return self.table_set.filter(version=version).order_by("name")

    @property
    def last_version(self):
        return self.get_last_version()

    def get_table(self, tablename, allow_hidden=False):
        if allow_hidden:
            return Table.with_hidden.for_dataset(self).named(tablename)
        else:
            return Table.objects.for_dataset(self).named(tablename)

    def get_default_table(self):
        return Table.objects.for_dataset(self).default()

    def __str__(self):
        return "{} (by {}, source: {})".format(self.name, self.author_name, self.source_name)

    def get_model_declaration(self):
        version = self.version_set.order_by("order").last()
        table = self.table_set.get(version=version, default=True)
        return table.get_model_declaration()

    def get_last_version(self):
        return self.version_set.order_by("order").last()


class Link(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, null=False, blank=False)
    title = models.CharField(max_length=255, null=False, blank=False)
    url = models.URLField(max_length=2000, null=False, blank=False)

    def __str__(self):
        domain = urlparse(self.url).netloc
        return "{} ({})".format(self.title, domain)


class Version(models.Model):
    collected_at = models.DateField(null=False, blank=False)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, null=False, blank=False)
    download_url = models.URLField(max_length=2000, null=False, blank=False)
    name = models.CharField(max_length=255, null=False, blank=False)
    order = models.PositiveIntegerField(null=False, blank=False)

    def __str__(self):
        return "{}.{} (order: {})".format(self.dataset.slug, self.name, self.order)


class TableQuerySet(models.QuerySet):
    def for_dataset(self, dataset):
        if isinstance(dataset, str):
            kwargs = {"dataset__slug": dataset}
        else:
            kwargs = {"dataset": dataset}
        return self.filter(**kwargs)

    def default(self):
        return self.get(default=True)

    def named(self, name):
        return self.get(name=name)


class ActiveTableManager(models.Manager):
    """
    This manager is the main one for the Table model and it excludes hidden tables by default
    """

    def get_queryset(self):
        return super().get_queryset().filter(hidden=False)


class AllTablesManager(models.Manager):
    """
    This manager is used to fetch all tables in the database, including the hidden ones
    """


class Table(models.Model):
    objects = ActiveTableManager.from_queryset(TableQuerySet)()
    with_hidden = AllTablesManager.from_queryset(TableQuerySet)()

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, null=False, blank=False)
    default = models.BooleanField(null=False, blank=False)
    name = models.CharField(max_length=255, null=False, blank=False)
    options = JSONField(null=True, blank=True)
    ordering = ArrayField(models.CharField(max_length=63), null=False, blank=False)
    filtering = ArrayField(models.CharField(max_length=63), null=True, blank=True)
    search = ArrayField(models.CharField(max_length=63), null=True, blank=True)
    version = models.ForeignKey(Version, on_delete=models.CASCADE, null=False, blank=False)
    import_date = models.DateTimeField(null=True, blank=True)
    description = MarkdownxField(null=True, blank=True)
    hidden = models.BooleanField(default=False)

    def __str__(self):
        return "{}.{}.{}".format(self.dataset.slug, self.version.name, self.name)

    @property
    def data_table(self):
        return self.data_tables.get_current_active()

    @property
    def db_table(self):
        return self.data_table.db_table_name

    @property
    def fields(self):
        return self.field_set.all()

    @property
    def enabled(self):
        return not self.hidden

    @property
    def schema(self):
        db_fields_to_rows_fields = {
            "binary": rows_fields.BinaryField,
            "bool": rows_fields.BoolField,
            "date": rows_fields.DateField,
            "datetime": rows_fields.DatetimeField,
            "decimal": rows_fields.DecimalField,
            "email": rows_fields.EmailField,
            "float": rows_fields.FloatField,
            "integer": rows_fields.IntegerField,
            "json": rows_fields.JSONField,
            "string": rows_fields.TextField,
            "text": rows_fields.TextField,
        }
        return OrderedDict(
            [
                (n, db_fields_to_rows_fields.get(t, rows_fields.Field))
                for n, t in self.fields.values_list("name", "type")
            ]
        )

    @property
    def model_name(self):
        full_name = self.dataset.slug + "-" + self.name
        parts = full_name.replace("_", "-").replace(" ", "-").split("-")
        return "".join([word.capitalize() for word in parts])

    def get_model(self, cache=True, data_table=None):
        # TODO: the current dynamic model registry is handled by Brasil.IO's
        # code but it needs to be delegated to dynamic_models.

        data_table = data_table or self.data_table
        db_table = data_table.db_table_name

        # TODO: limit the max number of items in DYNAMIC_MODEL_REGISTRY
        cache_key = (self.id, db_table)
        if cache and cache_key in DYNAMIC_MODEL_REGISTRY:
            return DYNAMIC_MODEL_REGISTRY[cache_key]

        # TODO: unregister the model in Django if already registered (cache_key
        # in DYNAMIC_MODEL_REGISTRY and not cache)
        fields = {field.name: field.field_class for field in self.fields}
        fields["search_data"] = SearchVectorField(null=True)
        ordering = self.ordering or []
        filtering = self.filtering or []
        search = self.search or []
        indexes = []
        # TODO: add has_choices fields also
        if ordering:
            indexes.append(django_indexes.Index(name=make_index_name(db_table, "order", ordering), fields=ordering,))
        if filtering:
            for field_name in filtering:
                if ordering == [field_name]:
                    continue
                indexes.append(
                    django_indexes.Index(name=make_index_name(db_table, "filter", [field_name]), fields=[field_name])
                )
        if search:
            indexes.append(
                pg_indexes.GinIndex(name=make_index_name(db_table, "search", ["search_data"]), fields=["search_data"])
            )

        managers = {"objects": DatasetTableModelQuerySet.as_manager()}
        mixins = [DatasetTableModelMixin]
        meta = {"ordering": ordering, "indexes": indexes, "db_table": db_table}

        # TODO: move this hard-coded mixin/manager injections to maybe a model
        # proxy
        dataset_slug = self.dataset.slug
        name = self.name
        if dataset_slug == "socios-brasil" and name == "empresa":
            mixins.insert(0, data_models.SociosBrasilEmpresaMixin)
            managers["objects"] = data_models.SociosBrasilEmpresaQuerySet.as_manager()
        elif dataset_slug == "covid19":
            from covid19 import qs

            if name == "boletim":
                managers["objects"] = qs.Covid19BoletimQuerySet.as_manager()
            elif name == "caso":
                managers["objects"] = qs.Covid19CasoQuerySet.as_manager()

        Model = dynamic_models.create_model_class(
            name=self.model_name, module="core.models", fields=fields, mixins=mixins, meta=meta, managers=managers,
        )
        Model.extra = {
            "filtering": filtering,
            "ordering": ordering,
            "search": search,
        }
        DYNAMIC_MODEL_REGISTRY[cache_key] = Model
        return Model

    def get_model_declaration(self):
        Model = self.get_model()
        return dynamic_models.model_source_code(Model)

    def invalidate_cache(self):
        invalidate(self.db_table)


class FieldQuerySet(models.QuerySet):
    def for_table(self, table):
        return self.filter(table=table)

    def choiceables(self):
        return self.filter(has_choices=True, frontend_filter=True)


class Field(models.Model):
    objects = FieldQuerySet.as_manager()

    TYPE_CHOICES = [(value, value) for value in dynamic_models.FIELD_TYPES.keys()]

    choices = JSONField(null=True, blank=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, null=False, blank=False)
    description = models.TextField(null=True, blank=True)
    frontend_filter = models.BooleanField(null=False, blank=True, default=False)
    has_choices = models.BooleanField(null=False, blank=True, default=False)
    link_template = models.TextField(max_length=2000, null=True, blank=True)
    order = models.PositiveIntegerField(null=False, blank=False)
    null = models.BooleanField(null=False, blank=True, default=True)
    name = models.CharField(max_length=63)
    options = JSONField(null=True, blank=True)
    obfuscate = models.BooleanField(null=False, blank=True, default=False)
    show = models.BooleanField(null=False, blank=True, default=True)
    show_on_frontend = models.BooleanField(null=False, blank=True, default=False)
    table = models.ForeignKey(Table, on_delete=models.CASCADE, null=False, blank=False)
    title = models.CharField(max_length=63)
    type = models.CharField(max_length=15, choices=TYPE_CHOICES, null=False, blank=False)
    version = models.ForeignKey(Version, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        options = self.options or {}
        options_str = ", ".join("{}={}".format(key, repr(value)) for key, value in options.items())
        return "{}.{}({})".format(self.table.name, self.name, options_str)

    @property
    def field_class(self):
        kwargs = self.options or {}
        kwargs["null"] = self.null
        return dynamic_models.FIELD_TYPES[self.type](**kwargs)

    def options_text(self):
        if not self.options:
            return ""

        return ", ".join(["{}={}".format(key, repr(value)) for key, value in self.options.items()])

    def update_choices(self):
        Model = self.table.get_model()
        choices = Model.objects.order_by(self.name).distinct(self.name).values_list(self.name, flat=True)
        self.choices = {"data": [str(value) for value in choices]}


def get_table(dataset_slug, tablename, allow_hidden=False):
    qs = Table.objects
    if allow_hidden:
        qs = Table.with_hidden
    return qs.for_dataset(dataset_slug).named(tablename)


def get_table_model(dataset_slug, tablename):
    # TODO: this function is just a shortcut and should be removed
    table = get_table(dataset_slug, tablename, allow_hidden=True)
    ModelClass = table.get_model(cache=True)

    return ModelClass


class DataTableQuerySet(models.QuerySet):
    def get_current_active(self):
        return self.active().most_recent()

    def most_recent(self):
        return self.order_by("-created_at").first()

    def inactive(self):
        return self.filter(active=False)

    def active(self):
        return self.filter(active=True)

    def for_dataset(self, dataset_slug):
        return self.filter(table__dataset__slug=dataset_slug)


class DataTable(models.Model):
    objects = DataTableQuerySet.as_manager()

    created_at = models.DateTimeField(auto_now_add=True)
    table = models.ForeignKey(Table, related_name="data_tables", on_delete=models.SET_NULL, null=True)
    db_table_name = models.TextField()
    active = models.BooleanField(default=False)

    def __str__(self):
        return f"DataTable: {self.db_table_name}"

    @property
    def admin_url(self):
        return reverse("admin:core_datatable_change", args=[self.id])

    @classmethod
    def new_data_table(cls, table, suffix_size=8):
        db_table_suffix = "".join(random.choice(string.ascii_lowercase) for i in range(suffix_size))
        db_table_name = "data_{}_{}".format(table.dataset.slug.replace("-", ""), table.name.replace("_", ""))
        if db_table_suffix:
            db_table_name += f"_{db_table_suffix}"
        return cls(table=table, db_table_name=db_table_name)

    def activate(self, drop_inactive_table=False):
        with transaction.atomic():
            prev_data_table = self.table.data_table
            if prev_data_table:
                prev_data_table.deactivate(drop_table=drop_inactive_table)
            self.active = True
            self.save()

    def deactivate(self, drop_table=False, activate_most_recent=False):
        with transaction.atomic():
            if activate_most_recent and self.active:
                most_recent = self.table.data_tables.exclude(id=self.id).inactive().most_recent()
                if most_recent:
                    most_recent.activate(drop_inactive_table=drop_table)
                    return

            self.active = False
            self.save()
            if drop_table:
                self.delete_data_table()

    def delete_data_table(self):
        Model = self.table.get_model(cache=False, data_table=self)
        try:
            Model.delete_table()
        except ProgrammingError:  # model does not exist
            pass


def prevent_active_data_table_deletion(sender, instance, **kwargs):
    if instance.active:
        msg = f"{instance} is active and can not be deleted. Deactivate it first."
        raise RuntimeError(msg)


def clean_associated_data_base_table(sender, instance, **kwargs):
    instance.delete_data_table()


pre_delete.connect(prevent_active_data_table_deletion, sender=DataTable)
post_delete.connect(clean_associated_data_base_table, sender=DataTable)
