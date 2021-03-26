# -*- coding: utf-8 -*-
import inspect
import re
from collections import OrderedDict

import six
from django import VERSION as DJANGO_VERSION
from django.apps import apps
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRel
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.db.models import (
    NOT_PROVIDED,
    QuerySet,
    Manager,
    Model,
    ManyToOneRel,
    ManyToManyRel,
    OneToOneRel,
    ForeignKey,
    ManyToManyField,
    OneToOneField,
)
from django.db.models.base import ModelBase
from graphene.utils.str_converters import to_snake_case, to_camel_case
from graphene_django.utils import is_valid_django_model
from graphene.types.scalars import MAX_INT, MIN_INT
from graphene import Dynamic, List
from graphql import GraphQLList, GraphQLNonNull
from graphql.language.ast import (
    FragmentSpread,
    InlineFragment,
    Variable,
    BooleanValue,
    FloatValue,
    IntValue,
    ListValue,
    ObjectValue,
    StringValue,
    EnumValue,
)
from django.conf import settings


def get_reverse_fields(model):
    reverse_fields = {
        f.name: f for f in model._meta.get_fields() if f.auto_created and not f.concrete
    }

    for name, field in reverse_fields.items():
        # Django =>1.9 uses 'rel', django <1.9 uses 'related'
        related = getattr(field, "rel", None) or getattr(field, "related", None)
        if isinstance(related, ManyToOneRel):
            yield (name, related)
        elif isinstance(related, ManyToManyRel) and not related.symmetrical:
            yield (name, related)


def _resolve_model(obj):
    """
    Resolve supplied `obj` to a Django model class.
    `obj` must be a Django model class itself, or a string
    representation of one.  Useful in situations like GH #1225 where
    Django may not have resolved a string-based reference to a model in
    another model's foreign key definition.
    String representations should have the format:
        'appname.ModelName'
    """
    if isinstance(obj, six.string_types) and len(obj.split(".")) == 2:
        app_name, model_name = obj.split(".")
        resolved_model = apps.get_model(app_name, model_name)
        if resolved_model is None:
            msg = "Django did not return a model for {0}.{1}"
            raise ImproperlyConfigured(msg.format(app_name, model_name))
        return resolved_model
    elif inspect.isclass(obj) and issubclass(obj, Model):
        return obj
    raise ValueError("{0} is not a Django model".format(obj))


def get_related_model(field):
    # Backward compatibility patch for Django versions lower than 1.9.x
    if DJANGO_VERSION < (1, 9):
        return _resolve_model(field.rel.to)
    return field.remote_field.model


def get_model_fields(
    model, only_fields="__all__", exclude_fields=(), to_dict=False, for_queryset=False
):
    # Backward compatibility patch for Django versions lower than 1.11.x
    if DJANGO_VERSION >= (1, 11):
        private_fields = model._meta.private_fields
    else:
        private_fields = model._meta.virtual_fields

    all_fields_list = (
        list(model._meta.fields)
        + list(model._meta.local_many_to_many)
        + list(private_fields)
        + list(model._meta.fields_map.values())
    )

    # Make sure we don't duplicate local fields with "reverse" version
    # and get the real reverse django related_name
    reverse_fields = list(get_reverse_fields(model))
    invalid_fields = [field[1] for field in reverse_fields]

    local_fields = []
    for field in all_fields_list:
        if field not in invalid_fields:
            if isinstance(field, OneToOneRel):
                if for_queryset:
                    if field.related_query_name is None:
                        local_fields.append((field.name, field))
                    else:
                        local_fields.append((field.related_query_name, field))
                else:
                    local_fields.append((field.name, field))
            elif isinstance(field, (ManyToManyRel, ManyToOneRel)):
                if for_queryset:
                    if field.related_query_name == None:
                        local_fields.append((field.name, field))
                    else:
                        local_fields.append((field.related_query_name, field))
                else:
                    if field.related_name == None:
                        local_fields.append((field.get_accessor_name(), field))
                    else:
                        local_fields.append((field.related_name, field))

            else:
                local_fields.append((field.name, field))

    all_fields = local_fields + reverse_fields

    if settings.DEBUG:
        all_fields = sorted(all_fields, key=lambda f: f[0])
    if to_dict:
        fields = {}
    else:
        fields = []

    for name, field in all_fields:
        is_include = False
        if str(name).endswith("+"):
            continue

        if only_fields == "__all__" and name not in exclude_fields:
            is_include = True
        elif name in only_fields:
            is_include = True

        if is_include:
            if to_dict:
                fields[name] = field
            else:
                fields.append((name, field))
    return fields


def is_required(field):
    try:
        blank = getattr(field, "blank", getattr(field, "field", None))
        default = getattr(field, "default", getattr(field, "field", None))
        #  null = getattr(field, "null", getattr(field, "field", None))

        if blank is None:
            blank = True
        elif not isinstance(blank, bool):
            blank = getattr(blank, "blank", True)

        if default is None:
            default = NOT_PROVIDED
        elif default != NOT_PROVIDED:
            default = getattr(default, "default", default)

    except AttributeError:
        return False

    return not blank and default == NOT_PROVIDED


def get_type_field(gql_type, gql_name):
    fields = gql_type._meta.fields
    for name, field in fields.items():
        if to_camel_case(gql_name) == to_camel_case(name):
            if isinstance(field, Dynamic):
                a = field.get_type()
                field = field.get_type()
            else:
                field = field
            if isinstance(field, List):
                field_type = field.of_type
            else:
                field_type = field.type
            return name, field_type


def resolve_argument(input_type, argument):
    if isinstance(argument, list):
        ret = []
        for arg in argument:
            ret.append(resolve_argument(input_type, arg))
    elif isinstance(argument, dict):
        ret = {}
        for gql_name, value in argument.items():
            name, field_type = get_type_field(input_type, gql_name)
            if isinstance(value, (dict, list)):
                ret[name] = resolve_argument(field_type, value)
            else:
                ret[name] = value
    else:
        return argument
    return ret


def get_field_ast_by_path(info, path):
    path = path.copy()
    field_ast = info.field_asts[0]
    while len(path) != 0:
        found = False
        iterator = [f for f in field_ast.selection_set.selections]
        for field in iterator:
            if isinstance(field, FragmentSpread):
                iterator.extend(
                    [
                        f
                        for f in info.fragments[
                            field.name.value
                        ].selection_set.selections
                    ]
                )
            if isinstance(field, InlineFragment):
                iterator.extend([f for f in field.selection_set.selections])
            if field.name.value == path[0]:
                field_ast = field
                del path[0]
                found = True
                break
        if not found:
            assert False, "not found"
    return field_ast


def parse_ast(ast, variable_values={}):
    if isinstance(ast, Variable):
        var_name = ast.name.value
        value = variable_values.get(var_name)
        return value
    elif isinstance(ast, (StringValue, BooleanValue)):
        return ast.value
    elif isinstance(ast, IntValue):
        num = int(ast.value)
        if MIN_INT <= num <= MAX_INT:
            return num
    elif isinstance(ast, FloatValue):
        return float(ast.value)
    elif isinstance(ast, EnumValue):
        return ast.value
    elif isinstance(ast, ListValue):
        ret = []
        for ast_value in ast.values:
            value = parse_ast(ast_value, variable_values=variable_values)
            if value is not None:
                ret.append(value)
        return ret
    elif isinstance(ast, ObjectValue):
        ret = {}
        for field in ast.fields:
            value = parse_ast(field.value, variable_values=variable_values)
            if value is not None:
                ret[field.name.value] = value
        return ret
    else:
        return None


def parse_arguments_ast(arguments, variable_values={}):
    ret = {}
    for argument in arguments:
        value = parse_ast(argument.value, variable_values=variable_values)
        if value is not None:
            ret[argument.name.value] = value
    return ret
