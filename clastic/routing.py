# -*- coding: utf-8 -*-

import re

from .sinter import inject, get_arg_names
from ._errors import NotFound
from .middleware import (check_middlewares,
                         merge_middlewares,
                         make_middleware_chain)


RESERVED_ARGS = ('request', 'next', 'context', '_application', '_route')


class InvalidEndpoint(TypeError):
    pass


BINDING = re.compile(r'<'
                     r'(?P<name>[A-Za-z_]\w*)'
                     r'(?P<op>[?+:*]*)'
                     r'(?P<type>\w+)*'
                     r'>')
TYPE_CONV_MAP = {'int': int,
                 'float': float,
                 'unicode': unicode,
                 'str': unicode}
_PATH_SEG_TMPL = '(?P<%s>(/[\w%%\d])%s)'
_OP_ARITY_MAP = {'': False,  # whether or not an op is "multi"
                 '?': False,
                 ':': False,
                 '+': True,
                 '*': True}


def build_converter(converter, optional=False, multi=False):
    if multi:
        def multi_converter(value):
            if not value and optional:
                return []
            return [converter(v) for v in value.split('/')[1:]]
        return multi_converter

    def single_converter(value):
        if not value and optional:
            return None
        return converter(value.replace('/', ''))
    return single_converter


def collapse_token(text, token=None, sub=None):
    "Collapses whitespace to spaces by default"
    if token is None:
        sub = sub or ' '
        return ' '.join(text.split())
    else:
        sub = sub or token
        return sub.join([s for s in text.split(token) if s])


class BaseRoute(object):
    def __init__(self, pattern, endpoint=None, methods=None):
        self.pattern = pattern
        self.endpoint = endpoint
        self._execute = endpoint
        # TODO: crosscheck methods with known HTTP methods
        self.methods = methods and set([m.upper() for m in methods])
        self.regex, self.converters = self._compile(pattern)

    def match_path(self, path):
        ret = {}
        match = self.regex.match(path)
        if not match:
            return None
        groups = match.groupdict()
        try:
            for conv_name, conv in self.converters.items():
                ret[conv_name] = conv(groups[conv_name])
        except (KeyError, TypeError, ValueError):
            return None
        return ret

    def match_method(self, method):
        if method and self.methods:
            if method.upper() not in self.methods:
                return False
        return True

    def execute(self, request, **kwargs):
        if not self._execute:
            raise InvalidEndpoint('no endpoint function set on %r' % self)
        kwargs['_route'] = self
        kwargs['request'] = request
        return inject(self._execute, kwargs)

    def iter_routes(self, application):
        yield self

    def bind(self, application, *a, **kw):
        return

    def __repr__(self):
        cn = self.__class__.__name__
        ep = self.endpoint
        try:
            ep_name = '%s.%s' % (ep.__module__, ep.func_name)
        except:
            ep_name = repr(ep)
        args = (cn, self.pattern, ep_name)
        tmpl = '<%s pattern=%r endpoint=%s>'
        if self.methods:
            tmpl = '<%s pattern=%r endpoint=%s methods=%r>'
            args += (self.methods,)
        return tmpl % args

    def _compile(self, pattern):
        processed = []
        var_converter_map = {}

        for part in pattern.split('/'):
            match = BINDING.match(part)
            if not match:
                processed.append(part)
                continue
            parsed = match.groupdict()
            name, type_name, op = parsed['name'], parsed['type'], parsed['op']
            if name in var_converter_map:
                raise ValueError('duplicate path binding %s' % name)
            if op:
                if op == ':':
                    op = ''
                if not type_name:
                    type_name = 'unicode'
                    #raise ValueError('%s expected a type specifier' % part)
                try:
                    converter = TYPE_CONV_MAP[type_name]
                except KeyError:
                    raise ValueError('unknown type specifier %s' % type_name)
            else:
                converter = unicode

            try:
                multi = _OP_ARITY_MAP[op]
            except KeyError:
                _tmpl = 'unknown arity operator %r, expected one of %r'
                raise ValueError(_tmpl % (op, _OP_ARITY_MAP.keys()))
            var_converter_map[name] = build_converter(converter, multi=multi)

            path_seg_pattern = _PATH_SEG_TMPL % (name, op)
            processed[-1] += path_seg_pattern

        regex = re.compile('^' + '/'.join(processed) + '$')
        return regex, var_converter_map


class Route(BaseRoute):
    def __init__(self, pattern, endpoint, render_arg=None, *a, **kw):
        super(Route, self).__init__(pattern, endpoint, *a, **kw)
        self._middlewares = list(kw.pop('middlewares', []))
        self._resources = dict(kw.pop('resources', []))
        self._bound_apps = []
        self.endpoint_args = get_arg_names(endpoint)

        self._execute = None
        self._render = None
        self._render_factory = None
        self.render_arg = render_arg
        if callable(render_arg):
            self._render = render_arg

    def execute(self, request, **kwargs):
        injectables = {'_route': self,
                       'request': request,
                       '_application': self._bound_apps[-1]}
        injectables.update(self._resources)
        injectables.update(kwargs)
        return inject(self._execute, injectables)

    def empty(self):
        # more like a copy
        self_type = type(self)
        ret = self_type(self.pattern, self.endpoint, self.render_arg)
        ret.__dict__.update(self.__dict__)
        ret._middlewares = list(self._middlewares)
        ret._resources = dict(self._resources)
        ret._bound_apps = list(self._bound_apps)
        return ret

    def bind(self, app, rebind_render=True):
        resources = app.__dict__.get('resources', {})
        middlewares = app.__dict__.get('middlewares', [])
        if rebind_render:
            render_factory = app.__dict__.get('render_factory')
        else:
            render_factory = self._render_factory

        merged_resources = dict(self._resources)
        merged_resources.update(resources)
        merged_mw = merge_middlewares(self._middlewares, middlewares)
        r_copy = self.empty()
        try:
            r_copy._bind_args(app, merged_resources, merged_mw, render_factory)
        except:
            raise
        self._bind_args(app,
                        merged_resources,
                        merged_mw,
                        render_factory)
        self._bound_apps += (app,)
        return self

    def _bind_args(self, url_map, resources, middlewares, render_factory):
        url_args = set(self.converters.keys())
        builtin_args = set(RESERVED_ARGS)
        resource_args = set(resources.keys())

        tmp_avail_args = {'url': url_args,
                          'builtins': builtin_args,
                          'resources': resource_args}
        check_middlewares(middlewares, tmp_avail_args)
        provided = resource_args | builtin_args | url_args
        if callable(render_factory) and self.render_arg is not None \
                and not callable(self.render_arg):
            _render = render_factory(self.render_arg)
        elif callable(self._render):
            _render = self._render
        else:
            _render = lambda context: context
        _execute = make_middleware_chain(middlewares, self.endpoint, _render, provided)

        self._resources.update(resources)
        self._middlewares = middlewares
        self._render_factory = render_factory
        self._render = _render
        self._execute = _execute


class NullRoute(Route):
    def __init__(self, *a, **kw):
        super(NullRoute, self).__init__('/<_ignored*>', self.not_found)

    def not_found(self, request):
        raise NotFound(is_breaking=False)


"""
Routing notes
-------------

After being betrayed by Werkzeug routing in too many fashions, and
after reviewing many designs, a new routing scheme has been designed.

Clastic's existing pattern (inherited from Werkzeug) does have some
nice things going for it. Django routes with regexes, which can be
semantically confusing, bug-prone, and unspecialized for
URLs. Clastic/Werkzeug offer a constrained syntax/grammar that is
specialized to URL pattern generation. It aims to be:

 * Clear
 * Correct
 * Validatable

The last item is of course the most important. (Lookin at you Werkzeug.)

Werkzeug's constraints on syntax led to a better system, so
Clastic's routing took it a step further. Take a look at some examples:

 1. '/about/'
 2. '/blog/{post_id?int}'
 3. '/api/{service}/{path+}'
 4. '/polish_maths/{operation:str}/{numbers+float}'

1. Static patterns work as expected.

2. The '?' indicates "zero or one", like regex. The post_id will be
converted to an integer. Invalid or missing values yield a value of
None into the 0-or-1 binding.

3. Bindings are of type 'str' (i.e., string/text/unicode object) by
default, so here we have a single-segment, string 'service'
binding. We also accept a 'path' binding. '+' means 1-or-more, and the
type is string.

4. Here we do some Polish-notation math. The operation comes
first. Using an explicit 'str' is ok. Numbers is a repeating path of
floats.


Besides correctness, there are a couple improvements over
Werkzeug. The system does not mix type and arity (Werkzeug's "path"
converter was special because it consumed more than one path
segment). There are just a few built-in converters, for the
convenience of easy type conversion, not full-blown validation. It's
always confusing to get a vague 404 when better error messages could
have been produced (there are middlewares available for this).

(Also, in Werkzeug I found the commonly-used '<path:path>' to be
confusing. Which is the variable, which is the converter? {path+} is
better ;))


# TODO: should slashes be optional? _shouldn't they_?
# TODO: detect invalid URL pattern
# TODO: ugly corollary? unicode characters. (maybe)
# TODO: optional segments shouldn't appear anywhere but the tail of the URL
# TODO: slash redirect stuff (bunch of twiddling necessary to get
# absolute path for Location header)

# TODO: could methods be specified in the leading bit of the pattern?
# probably getting too fancy

"""

"""
Recently chopped "error handler" logic executed on uncaught exceptions
(within the except block in dispatch())::

                code = getattr(e, 'code', None)
                if code in self.error_handlers:
                    handler = self.error_handlers[code]
                else:
                    handler = self.error_handlers.get(None)

                if handler:
                    err_injectables = {'error': e,
                                       'request': request,
                                       '_application': self}
                    return inject(handler, err_injectables)
                else:
                    if code and callable(getattr(e, 'get_response', None)):
                        return e.get_response(request)
                    else:
                        raise

The reason this logic was not very clasticky was mostly because it was
purely Application-oriented, not Route-oriented, and did not translate
well on re-embeds/SubApplication usage.

Some form of error_render or errback should come into existence at
some point, but design is pending.

"""
