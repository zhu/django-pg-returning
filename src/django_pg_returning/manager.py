from django.db import transaction, models
from django.db.models import sql, Field, QuerySet
from typing import Dict, Any, List, Type, Optional, Tuple

from .compatibility import chain_query, get_model_fields
from .queryset import ReturningQuerySet


class UpdateReturningMixin(object):
    @staticmethod
    def _get_loaded_field_cb(target, model, fields):
        """
        Callback used by get_deferred_field_names().
        """
        target[model] = fields

    def _get_fields(self):  # type: () -> Dict[models.Model: List[models.Field]]
        """
        Gets a dictionary of fields for each model, selected by .only() and .defer() methods
        :return: A dictionary with model as key, fields list as value
        """
        fields = {}
        self.query.deferred_to_data(fields, self._get_loaded_field_cb)

        # No .only() or .defer() operations
        if not fields:
            # Remove all fields without columns in table
            fields = {self.model: get_model_fields(self.model, concrete=True)}

        return fields

    def _get_returning_qs(self, query_type, values=None, **updates):
        # type: (Type[sql.Query], Optional[Any], **Dict[str, Any]) -> ReturningQuerySet
        """
        Partial for update_returning functions
        :param updates: Data to pass to update(**updates) method
        :return: RawQuerySet of results
        :raises AssertionError: If input data is invalid
        """
        assert self.query.can_filter(), "Can not update or delete once a slice has been taken."
        assert getattr(self, '_fields', None) is None,\
            "Can not call delete() or update() after .values() or .values_list()"

        # Returns attname, not column.
        fields = self._get_fields()
        assert len(fields) == 1 and list(fields.keys())[0] == self.model,\
            "You can't fetch relative model fields with returning operation"

        field_str = ', '.join('"%s"' % str(f.column) for f in fields[self.model])

        self._for_write = True

        query = chain_query(self, query_type)

        if updates:
            query.add_update_values(updates)

        if values:
            query.add_update_fields(values)

        # Disable not supported fields.
        query._annotations = None
        query.select_for_update = False
        query.select_related = False
        query.clear_ordering(force_empty=True)

        self._result_cache = None
        query_sql, query_params = query.get_compiler(self.db).as_sql()
        query_sql = query_sql + ' RETURNING %s' % field_str
        with transaction.atomic(using=self.db, savepoint=False):
            return ReturningQuerySet(query_sql, model=self.model, params=query_params, using=self.db,
                                     fields=[f.attname for f in fields[self.model]])

    def update_returning(self, **updates):
        # type: (**Dict[str, Any]) -> ReturningQuerySet
        """
        Gets RawQuerySet of all fields, got with UPDATE ... RETURNING fields
        :return: RawQuerySet
        """
        assert updates, "No updates where provided"
        return self._get_returning_qs(sql.UpdateQuery, **updates)

    def _update_returning(self, values):
        # type: (List[Tuple[Field, Any, Any]]) -> ReturningQuerySet
        """
        A version of update_returning() that accepts field objects instead of field names.
        Used primarily for model saving and not intended for use by general
        code (it requires too much poking around at model internals to be
        useful at that level).
        """
        assert values, "No updates where provided"
        return self._get_returning_qs(sql.UpdateQuery, values=values)

    def delete_returning(self):  # type: () -> ReturningQuerySet
        """
        Gets RawQuerySet of all fields, got with DELETE ... RETURNING
        :return: RawQuerySet
        """
        return self._get_returning_qs(sql.DeleteQuery)


class UpdateReturningQuerySet(UpdateReturningMixin, models.QuerySet):
    @classmethod
    def clone_query_set(cls, qs):  # type: (QuerySet) -> UpdateReturningQuerySet
        """
        Copies standard QuerySet.clone() method, changing base class name
        :param qs: QuerySet to copy from
        :return: An UpdateReturningQuerySet, cloned from qs
        """
        query = chain_query(qs)
        c = cls(model=qs.model, query=query, using=qs._db, hints=qs._hints)
        c._sticky_filter = qs._sticky_filter
        c._for_write = qs._for_write
        c._prefetch_related_lookups = qs._prefetch_related_lookups[:]
        c._known_related_objects = qs._known_related_objects

        # Some fields are absent in earlier django versions
        if hasattr(qs, '_iterable_class'):
            c._iterable_class = qs._iterable_class

        if hasattr(qs, '_fields'):
            c._fields = qs._fields

        return c


class UpdateReturningManager(models.Manager):
    def get_queryset(self):
        return UpdateReturningQuerySet(using=self.db, model=self.model)
