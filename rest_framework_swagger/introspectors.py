# -*- coding: utf-8 -*-

"""Handles the instrospection of REST Framework Views and ViewSets."""

import itertools
import re
import logging

from .compat import strip_tags, get_pagination_attribures
from .yamlparser import YAMLDocstringParser
from .constants import INTROSPECTOR_ENUMS, INTROSPECTOR_PRIMITIVES
from .utils import (normalize_data_format, get_view_description,
                    do_markdown, get_serializer_name, get_default_value)
from abc import ABCMeta, abstractmethod

from django.http import HttpRequest
from django.contrib.admindocs.utils import trim_docstring
from django.utils.encoding import smart_text

import rest_framework
from rest_framework import viewsets
from rest_framework.utils import formatting
from rest_framework.mixins import ListModelMixin
try:
    import django_filters
except ImportError:
    django_filters = None

logger = logging.getLogger()


class IntrospectorHelper(object):
    __metaclass__ = ABCMeta

    @staticmethod
    def strip_yaml_from_docstring(docstring):
        """
        Strips YAML from the docstring.
        """
        split_lines = trim_docstring(docstring).split('\n')

        cut_off = None
        for index in range(len(split_lines) - 1, -1, -1):
            line = split_lines[index]
            line = line.strip()
            if line == '---':
                cut_off = index
                break
        if cut_off is not None:
            split_lines = split_lines[0:cut_off]

        return "\n".join(split_lines)

    @staticmethod
    def strip_params_from_docstring(docstring):
        """
        Strips the params from the docstring (ie. myparam -- Some param) will
        not be removed from the text body
        """
        params_pattern = re.compile(r' -- ')
        split_lines = trim_docstring(docstring).split('\n')

        cut_off = None
        for index, line in enumerate(split_lines):
            line = line.strip()
            if params_pattern.search(line):
                cut_off = index
                break
        if cut_off is not None:
            split_lines = split_lines[0:cut_off]

        return "\n".join(split_lines)

    @staticmethod
    def get_summary(callback, docstring=None):
        """
        Returns the first sentence of the first line of the class docstring
        """
        description = get_view_description(
            callback, html=False, docstring=docstring) \
            .split("\n")[0].split(".")[0]
        description = IntrospectorHelper.strip_yaml_from_docstring(
            description)
        description = IntrospectorHelper.strip_params_from_docstring(
            description)
        description = strip_tags(get_view_description(
            callback, html=True, docstring=description))
        return description


class BaseViewIntrospector(object):
    __metaclass__ = ABCMeta

    def __init__(self, callback, path, pattern, user):
        self.callback = callback
        self.path = path
        self.pattern = pattern
        self.user = user

    def get_yaml_parser(self):
        parser = YAMLDocstringParser(self)
        return parser

    @abstractmethod
    def __iter__(self):
        pass

    def get_iterator(self):
        return self.__iter__()

    def get_description(self):
        """
        Returns the first sentence of the first line of the class docstring
        """
        return IntrospectorHelper.get_summary(self.callback)

    def get_docs(self):
        return get_view_description(self.callback)


class BaseMethodIntrospector(object):
    __metaclass__ = ABCMeta

    ENUMS = INTROSPECTOR_ENUMS
    PRIMITIVES = INTROSPECTOR_PRIMITIVES

    def __init__(self, view_introspector, method):
        self.method = method
        self.parent = view_introspector
        self.callback = view_introspector.callback
        self.path = view_introspector.path
        self.user = view_introspector.user

    def get_module(self):
        return self.callback.__module__

    def check_yaml_methods(self, yaml_methods):
        missing_set = set()
        for key in yaml_methods:
            if key != "*" and key not in self.parent.methods():
                missing_set.add(key)
        if missing_set:
            raise Exception(
                "methods %s in class docstring are not in view methods %s"
                % (list(missing_set), list(self.parent.methods())))

    def get_yaml_parser(self):
        parser = YAMLDocstringParser(self)
        parent_parser = YAMLDocstringParser(self.parent)
        self.check_yaml_methods(parent_parser.object.keys())
        new_object = {}
        new_object.update(parent_parser.object.get("*", {}))
        new_object.update(parent_parser.object.get(self.method, {}))
        new_object.update(parser.object)
        parser.object = new_object
        return parser

    def get_extra_serializer_classes(self):
        return self.get_yaml_parser().get_extra_serializer_classes(
            self.callback)

    def ask_for_serializer_class(self):
        if hasattr(self.callback, 'get_serializer_class'):
            view = self.create_view()
            parser = self.get_yaml_parser()
            mock_view = parser.get_view_mocker(self.callback)
            view = mock_view(view)
            if view is not None:
                return view.get_serializer_class()

    def create_view(self):
        view = self.callback()
        if not hasattr(view, 'kwargs'):
            view.kwargs = dict()
        if hasattr(self.parent.pattern, 'default_args'):
            view.kwargs.update(self.parent.pattern.default_args)
        view.request = HttpRequest()
        view.request.user = self.user
        view.request.method = self.method
        return view

    def get_serializer_class(self):
        parser = self.get_yaml_parser()
        serializer = parser.get_serializer_class(self.callback)
        if serializer is None:
            serializer = self.ask_for_serializer_class()
        return serializer

    def get_response_serializer_class(self):
        parser = self.get_yaml_parser()
        serializer = parser.get_yaml_response_serializer_class(self.callback)
        if serializer is None:
            serializer = self.get_serializer_class()
        return serializer

    def get_request_serializer_class(self):
        parser = self.get_yaml_parser()
        serializer = parser.get_yaml_request_serializer_class(self.callback)
        if serializer is None:
            serializer = self.get_serializer_class()
        return serializer

    def get_summary(self):
        # If there is no docstring on the method, get class docs
        return IntrospectorHelper.get_summary(
            self.callback,
            self.get_docs() or self.parent.get_description())

    def get_operation_id(self):
        """
        Returns the APIView's operationId. Defaults to generating an ID based on
        the method and path.
        """
        parser = self.get_yaml_parser()
        operation_id = parser.object.get('operationId', None)
        if not operation_id:
            operation_id = self.method + "-" + self.path.strip("/").replace("/", "-")

        return operation_id

    def get_consumes(self):
        if not hasattr(self.callback, 'get_parsers'):
            return []
        return {r.media_type for r in self.callback().get_parsers()}

    def get_produces(self):
        if not hasattr(self.callback, 'get_renderers'):
            return []
        return {r.media_type for r in self.callback().get_renderers()}

    def get_description(self, use_markdown=False):
        """
        Returns the body of the docstring trimmed before any parameters are
        listed. First, get the class docstring and then get the method's. The
        methods will always inherit the class comments.
        """
        docstring = ""

        class_docs = get_view_description(self.callback)
        class_docs = IntrospectorHelper.strip_yaml_from_docstring(class_docs)
        class_docs = IntrospectorHelper.strip_params_from_docstring(class_docs)
        method_docs = self.get_docs()

        if class_docs is not None:
            docstring += class_docs + "  \n"
        if method_docs is not None:
            method_docs = formatting.dedent(smart_text(method_docs))
            method_docs = IntrospectorHelper.strip_yaml_from_docstring(
                method_docs
            )
            method_docs = IntrospectorHelper.strip_params_from_docstring(
                method_docs
            )
            docstring += '\n' + method_docs
        docstring = docstring.strip()

        return do_markdown(docstring) if use_markdown else docstring.replace("\n", " ")

    def get_parameters(self):
        """
        Returns parameters for an API. Parameters are a combination of HTTP
        query parameters as well as HTTP body parameters that are defined by
        the DRF serializer fields
        """
        params = []
        query_params = self.build_query_parameters()
        pagination_params = self.build_pagination_parameters()
        if django_filters is not None:
            query_params.extend(self.build_query_parameters_from_django_filters())

        if query_params:
            params += query_params

        if pagination_params and self.get_http_method() == "GET":
            params += pagination_params

        return params

    def get_http_method(self):
        return self.method

    @abstractmethod
    def get_docs(self):
        return ''

    def retrieve_docstring(self):
        """
        Attempts to fetch the docs for a class method. Returns None
        if the method does not exist
        """
        method = str(self.method).lower()
        if not hasattr(self.callback, method):
            return None

        return get_view_description(getattr(self.callback, method))

    def build_body_parameters(self):
        serializer = self.get_request_serializer_class()
        serializer_name = get_serializer_name(serializer)

        if serializer_name is None:
            return

        return {
            'name': serializer_name,
            'in': 'body',
            'schema': {
                "$ref": "#/definitions/{}".format(serializer_name)
            }
        }

    def get_form_parameters(self):
        serializer = self.get_request_serializer_class()

        fields = []
        for field in extract_serializer_fields(serializer):
            if field['read_only']:
                continue
            if field['type'] not in {"string", "number", "integer", "boolean", "array"}:
                continue
            parameter = {
                'in': 'formData',
                'name': field['name'],
            }
            normalize_data_format(field['type'], field['format'], parameter)
            for key in ['description', 'required', 'enum']:
                if field[key]:
                    parameter[key] = field[key]
            fields.append(parameter)

        return fields

    def build_path_parameters(self):
        """
        Gets the parameters from the URL
        """
        url_params = re.findall('/{([^}]*)}', self.path)
        params = []

        for param in url_params:
            params.append({
                'name': param,
                'type': 'string',
                'in': 'path',
                'required': True
            })

        return params

    def build_query_parameters(self):
        params = []

        docstring = self.retrieve_docstring() or ''
        docstring += "\n" + get_view_description(self.callback)

        if docstring is None:
            return params

        split_lines = docstring.split('\n')

        for line in split_lines:
            param = line.split(' -- ')
            if len(param) == 2:
                params.append({'in': 'query',
                               'name': param[0].strip(),
                               'description': param[1].strip(),
                               'type': 'string'})

        return params

    def build_pagination_parameters(self):
        paginator = self.callback.pagination_class if hasattr(self.callback, 'pagination_class') else None
        if paginator:
            page = paginator.page_query_param
            size = paginator.page_size_query_param
            if not page:
                logger.error("paginator {} on view {} does not have a page query param".format(
                    paginator, self.callback
                ))

            params = [{
                'in': 'query',
                'name': page,
                'description': "Page Number",
                'type': 'integer'
            }]

            if size:
                params.append({
                    'in': 'query',
                    'name': size,
                    'description': "Page Size",
                    'type': 'integer'
                })
            return params
        return None

    def build_query_parameters_from_django_filters(self):
        """
        introspect ``django_filters.FilterSet`` instances.
        """
        params = []

        # Default to showing filter params only for 'list' operation, but allow overriding this
        if self.method not in self.get_yaml_parser().get_param('filter_methods', ['list']):
            return params

        filter_class = getattr(self.callback, 'filter_class', None)
        if (filter_class is not None and
                issubclass(filter_class, django_filters.FilterSet)):
            for name, filter_ in filter_class.base_filters.items():
                data_type = 'string'
                parameter = {
                    'in': 'query',
                    'name': name,
                }
                help_text = getattr(filter_.field, 'help_text', None)
                if filter_.label:
                    parameter['description'] = filter_.label
                elif help_text:
                    parameter['description'] = help_text

                normalize_data_format(data_type, None, parameter)
                multiple_choices = filter_.extra.get('choices', {})
                if multiple_choices:
                    parameter['enum'] = [choice[0] for choice
                                         in itertools.chain(multiple_choices)]
                params.append(parameter)

        return params


def get_data_type(field):
    # (in swagger 2.0 we might get to use the descriptive types..
    from rest_framework import fields
    if isinstance(field, fields.BooleanField):
        return 'boolean', 'boolean'
    elif field.__class__.__name__ == "JSONField":
        return 'object', 'object'
    elif isinstance(field, fields.ModelField) and field.model_field.__class__.__name__ == "JSONField":
        return 'object', 'object'
    elif isinstance(field, fields.DictField):
        return 'object', 'object'
    elif isinstance(field, fields.ListField):
        return 'array', "array"
    elif hasattr(fields, 'NullBooleanField') and isinstance(field, fields.NullBooleanField):
        return 'boolean', 'boolean'
    # elif isinstance(field, fields.URLField):
        # return 'string', 'string' #  'url'
    # elif isinstance(field, fields.SlugField):
        # return 'string', 'string', # 'slug'
    elif isinstance(field, fields.ChoiceField):
        first_key = field.choices.keys()[0]
        if isinstance(first_key, int):
            return 'integer', 'int64'
        return 'string', 'string'
    # elif isinstance(field, fields.EmailField):
        # return 'string', 'string' #  'email'
    # elif isinstance(field, fields.RegexField):
        # return 'string', 'string' # 'regex'
    elif isinstance(field, fields.DateField):
        return 'string', 'date'
    elif isinstance(field, fields.DateTimeField):
        return 'string', 'date-time'  # 'datetime'
    # elif isinstance(field, fields.TimeField):
        # return 'string', 'string' # 'time'
    elif isinstance(field, fields.IntegerField):
        return 'integer', 'int64'  # 'integer'
    elif isinstance(field, fields.FloatField):
        return 'number', 'float'  # 'float'
    # elif isinstance(field, fields.DecimalField):
        # return 'string', 'string' #'decimal'
    # elif isinstance(field, fields.ImageField):
        # return 'string', 'string' # 'image upload'
    # elif isinstance(field, fields.FileField):
        # return 'string', 'string' # 'file upload'
    # elif isinstance(field, fields.CharField):
        # return 'string', 'string'
    elif getattr(field, 'style', {}).get('input_type') == 'password':
        return 'string', 'password'

    elif rest_framework.VERSION >= '3.0.0' and isinstance(field, fields.HiddenField):
        return 'hidden', 'hidden'
    else:
        return 'string', 'string'


class APIViewIntrospector(BaseViewIntrospector):
    def __iter__(self):
        for method in self.methods():
            yield APIViewMethodIntrospector(self, method)

    def methods(self):
        return self.callback().allowed_methods


class GenericViewIntrospector(BaseViewIntrospector):
    """
    Instead of retrieving the information from the 'get', 'post', 'put', 'delete'
    methods, we'll use (as we should) the 'list', 'retrieve', 'create', 'update' and
    'destroy' methods of the view
    """

    method_actions = {
        'post': 'create',
        'put': 'update',
        'patch': 'partial_update',
        'delete': 'destroy'
    }

    def __iter__(self):
        for http_method, action in self.methods().iteritems():
            yield GenericViewMethodIntrospector(self, action, http_method)

    def _get_action_from_http_method(self, http_method):
        """
        Gets the corresponding action name for the http_method.
        Since a "GET" method can be a "list" or "retrieve" we'll check
        if the view extends ListModelMixin to convert it
        """
        http_method = http_method.lower()
        if http_method == 'get':
            return 'list' if issubclass(self.callback, ListModelMixin) else 'retrieve'
        if http_method not in self.method_actions:
            return http_method
        return self.method_actions[http_method]

    def methods(self):
        """
        returns a map containing all available http methods for the view and
        their corresponding view method name (action)
        i.e.:
            {
                "post": "create",
                "get": "list"
            }
        """
        methods = {}
        for http_method in self.callback().allowed_methods:
            methods[http_method] = self._get_action_from_http_method(http_method)
        return methods


class WrappedAPIViewIntrospector(BaseViewIntrospector):
    def __iter__(self):
        for method in self.methods():
            yield WrappedAPIViewMethodIntrospector(self, method)

    def methods(self):
        return self.callback().allowed_methods

    def get_notes(self):
        class_docs = get_view_description(self.callback)
        class_docs = IntrospectorHelper.strip_yaml_from_docstring(
            class_docs)
        class_docs = IntrospectorHelper.strip_params_from_docstring(
            class_docs)
        return get_view_description(
            self.callback, html=True, docstring=class_docs)


class APIViewMethodIntrospector(BaseMethodIntrospector):
    def get_docs(self):
        """
        Attempts to retrieve method specific docs for an
        endpoint. If none are available, the class docstring
        will be used
        """
        return self.retrieve_docstring()


class GenericViewMethodIntrospector(BaseMethodIntrospector):

    def __init__(self, view_introspector, action, http_method):
        super(GenericViewMethodIntrospector, self).__init__(view_introspector, action)
        self.http_method = http_method.upper()

    def get_http_method(self):
        return self.http_method

    def get_docs(self):
        """
        Attempts to retrieve method specific docs for an
        endpoint. If none are available, the class docstring
        will be used
        """
        return self.retrieve_docstring()


class WrappedAPIViewMethodIntrospector(BaseMethodIntrospector):
    def get_docs(self):
        """
        Attempts to retrieve method specific docs for an
        endpoint. If none are available, the class docstring
        will be used
        """
        return get_view_description(self.callback)

    def get_module(self):
        from rest_framework_swagger.decorators import wrapper_to_func
        func = wrapper_to_func(self.callback)
        return func.__module__

    def get_notes(self):
        return self.parent.get_notes()

    def get_yaml_parser(self):
        parser = YAMLDocstringParser(self)
        return parser


class ViewSetIntrospector(BaseViewIntrospector):
    """Handle ViewSet introspection."""

    def __init__(self, callback, path, pattern, user, patterns=None):
        super(ViewSetIntrospector, self).__init__(callback, path, pattern, user)
        if not issubclass(callback, viewsets.ViewSetMixin):
            raise Exception("wrong callback passed to ViewSetIntrospector")
        self.patterns = patterns or [pattern]

    def __iter__(self):
        methods = self._resolve_methods()
        for method in methods:
            yield ViewSetMethodIntrospector(self, methods[method], method)

    def methods(self):
        stuff = []
        for pattern in self.patterns:
            if pattern.callback:
                stuff.extend(self._resolve_methods(pattern).values())
        return stuff

    def _resolve_methods(self, pattern=None):
        from .decorators import closure_n_code, get_closure_var
        if pattern is None:
            pattern = self.pattern
        callback = pattern.callback

        try:
            x = closure_n_code(callback)

            while getattr(x.code, 'co_name') != 'view':
                # lets unwrap!
                callback = get_closure_var(callback)
                x = closure_n_code(callback)

            freevars = x.code.co_freevars
        except (AttributeError, IndexError):
            raise RuntimeError(
                'Unable to use callback invalid closure/function ' +
                'specified.')
        else:
            return x.closure[freevars.index('actions')].cell_contents


class ViewSetMethodIntrospector(BaseMethodIntrospector):
    def __init__(self, view_introspector, method, http_method):
        super(ViewSetMethodIntrospector, self) \
            .__init__(view_introspector, method)
        self.http_method = http_method.upper()

    def get_http_method(self):
        return self.http_method

    def get_docs(self):
        """
        Attempts to retrieve method specific docs for an
        endpoint. If none are available, the class docstring
        will be used
        """
        return self.retrieve_docstring()

    def create_view(self):
        view = super(ViewSetMethodIntrospector, self).create_view()
        if not hasattr(view, 'action'):
            setattr(view, 'action', self.method)
        view.request.method = self.http_method
        return view

    def build_query_parameters(self):
        parameters = super(ViewSetMethodIntrospector, self) \
            .build_query_parameters()
        view = self.create_view()
        page_size, page_query_param, page_size_query_param = get_pagination_attribures(view)
        if self.method == 'list' and page_size:
            data_type = 'integer'
            if page_query_param:
                parameters.append({
                    'in': 'query',
                    'name': page_query_param,
                    'description': None,
                })
                normalize_data_format(data_type, None, parameters[-1])
            if page_size_query_param:
                parameters.append({
                    'in': 'query',
                    'name': page_size_query_param,
                    'description': None,
                })
                normalize_data_format(data_type, None, parameters[-1])
        return parameters


def extract_serializer_fields(serializer):
    if serializer is None:
        return []

    if hasattr(serializer, '__call__'):
        fields = serializer().get_fields()
    else:
        fields = serializer.get_fields()

    serializer_data = []
    for name, field in fields.items():
        data_type, data_format = get_data_type(field) or ('string', 'string')

        if data_type == 'hidden':
            continue

        field_data = {
            'minimum': None,
            'maximum': None,
            'enum': None,
            'items': None,
            '$ref': None,
            'name': name,
            'type': data_type,
            'format': data_format,
            'write_only': getattr(field, 'write_only', False),
            'read_only': getattr(field, 'read_only', False),
            'required': getattr(field, 'required', False),
            'default': get_default_value(field),
        }

        help_text = getattr(field, 'help_text', '')
        field_data['description'] = help_text.strip() if help_text else ''

        # guess format
        # data_format = 'string'
        # if data_type in BaseMethodIntrospector.PRIMITIVES:
        # data_format = BaseMethodIntrospector.PRIMITIVES.get(data_type)[0]


        # Min/Max values
        max_value = getattr(field, 'max_value', None)
        min_value = getattr(field, 'min_value', None)

        if data_type == 'integer':
            field_data['minimum'] = min_value
            field_data['maximum'] = max_value

        # ENUM options
        if data_type in BaseMethodIntrospector.ENUMS:
            if isinstance(field.choices, list):
                field_data['enum'] = [k for k, v in field.choices]
            elif isinstance(field.choices, dict):
                field_data['enum'] = [k for k, v in field.choices.items()]

        # Support for complex types
        if rest_framework.VERSION < '3.0.0':
            has_many = hasattr(field, 'many') and field.many
        else:
            from rest_framework.serializers import ListSerializer, ManyRelatedField
            has_many = isinstance(field, (ListSerializer, ManyRelatedField))

        if isinstance(field, rest_framework.serializers.BaseSerializer) or has_many:
            if hasattr(field, 'is_documented') and not field.is_documented:
                field_data['type'] = 'object'
            elif isinstance(field, rest_framework.serializers.BaseSerializer):
                field_serializer = get_serializer_name(field)

                if getattr(field, 'write_only', False):
                    field_serializer = "Write{}".format(field_serializer)

                if not has_many:
                    #del field_data['type']
                    field_data['$ref'] = '#/definitions/' + field_serializer
            else:
                field_serializer = None
                data_type = 'string'

            if has_many:
                field_data['type'] = 'array'
                if field_serializer:
                    field_data['items'] = {'$ref': '#/definitions/' + field_serializer}
                elif data_type in BaseMethodIntrospector.PRIMITIVES:
                    field_data['items'] = {'type': data_type}
        elif isinstance(field, rest_framework.serializers.ListField):
            field_data['type'] = 'array'
            if not field.child:
                field_data['items'] = {'type': 'string'}
            child_type, child_format = get_data_type(field.child) or ('string', 'string')
            field_data['items'] = {'type': child_type}
        serializer_data.append(field_data)
    return serializer_data
