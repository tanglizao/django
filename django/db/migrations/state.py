from __future__ import unicode_literals
from collections import OrderedDict
import copy

from django.apps import AppConfig
from django.apps.registry import Apps, apps as global_apps
from django.db import models
from django.db.models.options import DEFAULT_NAMES, normalize_together
from django.db.models.fields.related import do_pending_lookups
from django.db.models.fields.proxy import OrderWrt
from django.conf import settings
from django.utils import six
from django.utils.encoding import force_text, smart_text
from django.utils.functional import cached_property
from django.utils.module_loading import import_string
from django.utils.version import get_docs_version


class InvalidBasesError(ValueError):
    pass


class ProjectState(object):
    """
    Represents the entire project's overall state.
    This is the item that is passed around - we do it here rather than at the
    app level so that cross-app FKs/etc. resolve properly.
    """

    def __init__(self, models=None, real_apps=None):
        self.models = models or {}
        # Apps to include from main registry, usually unmigrated ones
        self.real_apps = real_apps or []

    def add_model(self, model_state):
        app_label, model_name = model_state.app_label, model_state.name.lower()
        self.models[(app_label, model_name)] = model_state
        if 'apps' in self.__dict__:  # hasattr would cache the property
            self.reload_model(app_label, model_name)

    def remove_model(self, app_label, model_name):
        model_name = model_name.lower()
        del self.models[app_label, model_name]
        if 'apps' in self.__dict__:  # hasattr would cache the property
            self.apps.unregister_model(app_label, model_name)

    def reload_model(self, app_label, model_name):
        if 'apps' in self.__dict__:  # hasattr would cache the property
            # Get relations before reloading the models, as _meta.apps may change
            model_name = model_name.lower()
            try:
                related_old = {
                    f.related_model for f in
                    self.apps.get_model(app_label, model_name)._meta.related_objects
                }
            except LookupError:
                related_old = set()
            self._reload_one_model(app_label, model_name)
            # Reload models if there are relations
            model = self.apps.get_model(app_label, model_name)
            related_m2m = {f.related_model for f in model._meta.many_to_many}
            for rel_model in related_old.union(related_m2m):
                self._reload_one_model(rel_model._meta.app_label, rel_model._meta.model_name)
            if related_m2m:
                # Re-render this model after related models have been reloaded
                self._reload_one_model(app_label, model_name)

    def _reload_one_model(self, app_label, model_name):
        self.apps.unregister_model(app_label, model_name)
        self.models[app_label, model_name].render(self.apps)

    def clone(self):
        "Returns an exact copy of this ProjectState"
        new_state = ProjectState(
            models={k: v.clone() for k, v in self.models.items()},
            real_apps=self.real_apps,
        )
        if 'apps' in self.__dict__:
            new_state.apps = self.apps.clone()
        return new_state

    @cached_property
    def apps(self):
        return StateApps(self.real_apps, self.models)

    @property
    def concrete_apps(self):
        self.apps = StateApps(self.real_apps, self.models, ignore_swappable=True)
        return self.apps

    @classmethod
    def from_apps(cls, apps):
        "Takes in an Apps and returns a ProjectState matching it"
        app_models = {}
        for model in apps.get_models(include_swapped=True):
            model_state = ModelState.from_model(model)
            app_models[(model_state.app_label, model_state.name.lower())] = model_state
        return cls(app_models)

    def __eq__(self, other):
        if set(self.models.keys()) != set(other.models.keys()):
            return False
        if set(self.real_apps) != set(other.real_apps):
            return False
        return all(model == other.models[key] for key, model in self.models.items())

    def __ne__(self, other):
        return not (self == other)


class AppConfigStub(AppConfig):
    """
    Stubs a Django AppConfig. Only provides a label, and a dict of models.
    """
    # Not used, but required by AppConfig.__init__
    path = ''

    def __init__(self, label):
        self.label = label
        # App-label and app-name are not the same thing, so technically passing
        # in the label here is wrong. In practice, migrations don't care about
        # the app name, but we need something unique, and the label works fine.
        super(AppConfigStub, self).__init__(label, None)

    def import_models(self, all_models):
        self.models = all_models


class StateApps(Apps):
    """
    Subclass of the global Apps registry class to better handle dynamic model
    additions and removals.
    """
    def __init__(self, real_apps, models, ignore_swappable=False):
        # Any apps in self.real_apps should have all their models included
        # in the render. We don't use the original model instances as there
        # are some variables that refer to the Apps object.
        # FKs/M2Ms from real apps are also not included as they just
        # mess things up with partial states (due to lack of dependencies)
        real_models = []
        for app_label in real_apps:
            app = global_apps.get_app_config(app_label)
            for model in app.get_models():
                real_models.append(ModelState.from_model(model, exclude_rels=True))
        # Populate the app registry with a stub for each application.
        app_labels = {model_state.app_label for model_state in models.values()}
        app_configs = [AppConfigStub(label) for label in sorted(real_apps + list(app_labels))]
        super(StateApps, self).__init__(app_configs)

        # We keep trying to render the models in a loop, ignoring invalid
        # base errors, until the size of the unrendered models doesn't
        # decrease by at least one, meaning there's a base dependency loop/
        # missing base.
        unrendered_models = list(models.values()) + real_models
        while unrendered_models:
            new_unrendered_models = []
            for model in unrendered_models:
                try:
                    model.render(self)
                except InvalidBasesError:
                    new_unrendered_models.append(model)
            if len(new_unrendered_models) == len(unrendered_models):
                raise InvalidBasesError(
                    "Cannot resolve bases for %r\nThis can happen if you are inheriting models from an "
                    "app with migrations (e.g. contrib.auth)\n in an app with no migrations; see "
                    "https://docs.djangoproject.com/en/%s/topics/migrations/#dependencies "
                    "for more" % (new_unrendered_models, get_docs_version())
                )
            unrendered_models = new_unrendered_models

        # If there are some lookups left, see if we can first resolve them
        # ourselves - sometimes fields are added after class_prepared is sent
        for lookup_model, operations in self._pending_lookups.items():
            try:
                model = self.get_model(lookup_model[0], lookup_model[1])
            except LookupError:
                app_label = "%s.%s" % (lookup_model[0], lookup_model[1])
                if app_label == settings.AUTH_USER_MODEL and ignore_swappable:
                    continue
                # Raise an error with a best-effort helpful message
                # (only for the first issue). Error message should look like:
                # "ValueError: Lookup failed for model referenced by
                # field migrations.Book.author: migrations.Author"
                msg = "Lookup failed for model referenced by field {field}: {model[0]}.{model[1]}"
                raise ValueError(msg.format(field=operations[0][1], model=lookup_model))
            else:
                do_pending_lookups(model)

    def clone(self):
        """
        Return a clone of this registry, mainly used by the migration framework.
        """
        clone = StateApps([], {})
        clone.all_models = copy.deepcopy(self.all_models)
        clone.app_configs = copy.deepcopy(self.app_configs)
        return clone

    def register_model(self, app_label, model):
        self.all_models[app_label][model._meta.model_name] = model
        if app_label not in self.app_configs:
            self.app_configs[app_label] = AppConfigStub(app_label)
            self.app_configs[app_label].models = OrderedDict()
        self.app_configs[app_label].models[model._meta.model_name] = model
        self.clear_cache()

    def unregister_model(self, app_label, model_name):
        try:
            del self.all_models[app_label][model_name]
            del self.app_configs[app_label].models[model_name]
        except KeyError:
            pass
        self.clear_cache()


class ModelState(object):
    """
    Represents a Django Model. We don't use the actual Model class
    as it's not designed to have its options changed - instead, we
    mutate this one and then render it into a Model as required.

    Note that while you are allowed to mutate .fields, you are not allowed
    to mutate the Field instances inside there themselves - you must instead
    assign new ones, as these are not detached during a clone.
    """

    def __init__(self, app_label, name, fields, options=None, bases=None, managers=None):
        self.app_label = app_label
        self.name = force_text(name)
        self.fields = fields
        self.options = options or {}
        self.bases = bases or (models.Model, )
        self.managers = managers or []
        # Sanity-check that fields is NOT a dict. It must be ordered.
        if isinstance(self.fields, dict):
            raise ValueError("ModelState.fields cannot be a dict - it must be a list of 2-tuples.")
        # Sanity-check that fields are NOT already bound to a model.
        for name, field in fields:
            if hasattr(field, 'model'):
                raise ValueError(
                    'ModelState.fields cannot be bound to a model - "%s" is.' % name
                )

    @classmethod
    def from_model(cls, model, exclude_rels=False):
        """
        Feed me a model, get a ModelState representing it out.
        """
        # Deconstruct the fields
        fields = []
        for field in model._meta.local_fields:
            if getattr(field, "rel", None) and exclude_rels:
                continue
            if isinstance(field, OrderWrt):
                continue
            name, path, args, kwargs = field.deconstruct()
            field_class = import_string(path)
            try:
                fields.append((name, field_class(*args, **kwargs)))
            except TypeError as e:
                raise TypeError("Couldn't reconstruct field %s on %s.%s: %s" % (
                    name,
                    model._meta.app_label,
                    model._meta.object_name,
                    e,
                ))
        if not exclude_rels:
            for field in model._meta.local_many_to_many:
                name, path, args, kwargs = field.deconstruct()
                field_class = import_string(path)
                try:
                    fields.append((name, field_class(*args, **kwargs)))
                except TypeError as e:
                    raise TypeError("Couldn't reconstruct m2m field %s on %s: %s" % (
                        name,
                        model._meta.object_name,
                        e,
                    ))
        # Extract the options
        options = {}
        for name in DEFAULT_NAMES:
            # Ignore some special options
            if name in ["apps", "app_label"]:
                continue
            elif name in model._meta.original_attrs:
                if name == "unique_together":
                    ut = model._meta.original_attrs["unique_together"]
                    options[name] = set(normalize_together(ut))
                elif name == "index_together":
                    it = model._meta.original_attrs["index_together"]
                    options[name] = set(normalize_together(it))
                else:
                    options[name] = model._meta.original_attrs[name]
        # Force-convert all options to text_type (#23226)
        options = cls.force_text_recursive(options)
        # If we're ignoring relationships, remove all field-listing model
        # options (that option basically just means "make a stub model")
        if exclude_rels:
            for key in ["unique_together", "index_together", "order_with_respect_to"]:
                if key in options:
                    del options[key]

        def flatten_bases(model):
            bases = []
            for base in model.__bases__:
                if hasattr(base, "_meta") and base._meta.abstract:
                    bases.extend(flatten_bases(base))
                else:
                    bases.append(base)
            return bases

        # We can't rely on __mro__ directly because we only want to flatten
        # abstract models and not the whole tree. However by recursing on
        # __bases__ we may end up with duplicates and ordering issues, we
        # therefore discard any duplicates and reorder the bases according
        # to their index in the MRO.
        flattened_bases = sorted(set(flatten_bases(model)), key=lambda x: model.__mro__.index(x))

        # Make our record
        bases = tuple(
            (
                "%s.%s" % (base._meta.app_label, base._meta.model_name)
                if hasattr(base, "_meta") else
                base
            )
            for base in flattened_bases
        )
        # Ensure at least one base inherits from models.Model
        if not any((isinstance(base, six.string_types) or issubclass(base, models.Model)) for base in bases):
            bases = (models.Model,)

        # Constructs all managers on the model
        managers = {}

        def reconstruct_manager(mgr):
            as_manager, manager_path, qs_path, args, kwargs = mgr.deconstruct()
            if as_manager:
                qs_class = import_string(qs_path)
                instance = qs_class.as_manager()
            else:
                manager_class = import_string(manager_path)
                instance = manager_class(*args, **kwargs)
            # We rely on the ordering of the creation_counter of the original
            # instance
            managers[mgr.name] = (mgr.creation_counter, instance)

        default_manager_name = model._default_manager.name
        # Make sure the default manager is always the first
        if model._default_manager.use_in_migrations:
            reconstruct_manager(model._default_manager)
        else:
            # Force this manager to be the first and thus default
            managers[default_manager_name] = (0, models.Manager())
        # Sort all managers by their creation counter
        for _, manager, _ in sorted(model._meta.managers):
            if manager.name == '_base_manager' or not manager.use_in_migrations:
                continue
            reconstruct_manager(manager)
        # Sort all managers by their creation counter but take only name and
        # instance for further processing
        managers = [
            (name, instance) for name, (cc, instance) in
            sorted(managers.items(), key=lambda v: v[1])
        ]
        if managers == [(default_manager_name, models.Manager())]:
            managers = []

        # Construct the new ModelState
        return cls(
            model._meta.app_label,
            model._meta.object_name,
            fields,
            options,
            bases,
            managers,
        )

    @classmethod
    def force_text_recursive(cls, value):
        if isinstance(value, six.string_types):
            return smart_text(value)
        elif isinstance(value, list):
            return [cls.force_text_recursive(x) for x in value]
        elif isinstance(value, tuple):
            return tuple(cls.force_text_recursive(x) for x in value)
        elif isinstance(value, set):
            return set(cls.force_text_recursive(x) for x in value)
        elif isinstance(value, dict):
            return {
                cls.force_text_recursive(k): cls.force_text_recursive(v)
                for k, v in value.items()
            }
        return value

    def construct_fields(self):
        "Deep-clone the fields using deconstruction"
        for name, field in self.fields:
            _, path, args, kwargs = field.deconstruct()
            field_class = import_string(path)
            yield name, field_class(*args, **kwargs)

    def clone(self):
        "Returns an exact copy of this ModelState"
        return self.__class__(
            app_label=self.app_label,
            name=self.name,
            fields=list(self.construct_fields()),
            options=dict(self.options),
            bases=self.bases,
            managers=self.managers,
        )

    def render(self, apps):
        "Creates a Model object from our current state into the given apps"
        # First, make a Meta object
        meta_contents = {'app_label': self.app_label, "apps": apps}
        meta_contents.update(self.options)
        meta = type(str("Meta"), tuple(), meta_contents)
        # Then, work out our bases
        try:
            bases = tuple(
                (apps.get_model(base) if isinstance(base, six.string_types) else base)
                for base in self.bases
            )
        except LookupError:
            raise InvalidBasesError("Cannot resolve one or more bases from %r" % (self.bases,))
        # Turn fields into a dict for the body, add other bits
        body = dict(self.construct_fields())
        body['Meta'] = meta
        body['__module__'] = "__fake__"

        # Restore managers
        for mgr_name, manager in self.managers:
            body[mgr_name] = manager

        # Then, make a Model object (apps.register_model is called in __new__)
        return type(
            str(self.name),
            bases,
            body,
        )

    def get_field_by_name(self, name):
        for fname, field in self.fields:
            if fname == name:
                return field
        raise ValueError("No field called %s on model %s" % (name, self.name))

    def __repr__(self):
        return "<ModelState: '%s.%s'>" % (self.app_label, self.name)

    def __eq__(self, other):
        return (
            (self.app_label == other.app_label) and
            (self.name == other.name) and
            (len(self.fields) == len(other.fields)) and
            all((k1 == k2 and (f1.deconstruct()[1:] == f2.deconstruct()[1:]))
                for (k1, f1), (k2, f2) in zip(self.fields, other.fields)) and
            (self.options == other.options) and
            (self.bases == other.bases) and
            (self.managers == other.managers)
        )

    def __ne__(self, other):
        return not (self == other)
