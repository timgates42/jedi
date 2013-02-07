"""
If you know what an abstract syntax tree (ast) is, you'll see that this module
is pretty much that. The classes represent syntax elements: ``Import``,
``Function``.

A very central class is ``Scope``. It is not used directly by the parser, but
inherited. It's used by ``Function``, ``Class``, ``Flow``, etc. A ``Scope`` may
have ``subscopes``, ``imports`` and ``statements``. The entire parser is based
on scopes, because they also stand for indentation.

One strange thing about the parser is that ``Array`` is two dimensional.  This
has been caused by the fact that each array element can be defined by
operations: ``[1, 2+33]``. So I chose to use a second dimension for ``2+33``,
where each element would lie in the array like this: ``[2, '+', 33]``. In the
future it might be useful to use Statements there, too (remove those crappy two
dimensional arrays). This is also how ``Param`` works. Every single ``Param``
is a ``Statement``.


.. todo:: remove docstr params from Scope.__init__()
"""

import os
import re
import tokenize

from _compatibility import next, literal_eval, cleandoc, Python3Method, \
                            property
import common
import debug


class Base(object):
    """
    This is just here to have an isinstance check, which is also used on
    evaluate classes. But since they have sometimes a special type of
    delegation, it is important for those classes to override this method.

    I know that there is a chance to do such things with __instancecheck__, but
    since Python 2.5 doesn't support it, I decided to do it this way.
    """
    __slots__ = ()

    def isinstance(self, *cls):
        return isinstance(self, cls)


class Simple(Base):
    """
    The super class for Scope, Import, Name and Statement. Every object in
    the parser tree inherits from this class.
    """
    __slots__ = ('parent', 'module', '_start_pos', 'use_as_parent', '_end_pos')

    def __init__(self, module, start_pos, end_pos=(None, None)):
        self._start_pos = start_pos
        self._end_pos = end_pos
        self.parent = None
        # use this attribute if parent should be something else than self.
        self.use_as_parent = self
        self.module = module

    @Python3Method
    def get_parent_until(self, classes=(), reverse=False,
                                                    include_current=True):
        """ Takes always the parent, until one class (not a Class) """
        if type(classes) not in (tuple, list):
            classes = (classes,)
        scope = self if include_current else self.parent
        while scope.parent is not None:
            if classes and reverse != scope.isinstance(*classes):
                break
            scope = scope.parent
        return scope

    @property
    def start_pos(self):
        return self.module.line_offset + self._start_pos[0], self._start_pos[1]

    @start_pos.setter
    def start_pos(self, value):
        self._start_pos = value

    @property
    def end_pos(self):
        if None in self._end_pos:
            return self._end_pos
        return self.module.line_offset + self._end_pos[0], self._end_pos[1]

    @end_pos.setter
    def end_pos(self, value):
        self._end_pos = value

    def __repr__(self):
        code = self.get_code().replace('\n', ' ')
        return "<%s: %s@%s>" % \
                (type(self).__name__, code, self.start_pos[0])


class Scope(Simple):
    """
    Super class for the parser tree, which represents the state of a python
    text file.
    A Scope manages and owns its subscopes, which are classes and functions, as
    well as variables and imports. It is used to access the structure of python
    files.

    :param start_pos: The position (line and column) of the scope.
    :type start_pos: tuple(int, int)
    :param docstr: The docstring for the current Scope.
    :type docstr: str
    """
    def __init__(self, module, start_pos):
        super(Scope, self).__init__(module, start_pos)
        self.subscopes = []
        self.imports = []
        self.statements = []
        self.docstr = ''
        self.asserts = []

    def add_scope(self, sub, decorators):
        sub.parent = self.use_as_parent
        sub.decorators = decorators
        for d in decorators:
            # the parent is the same, because the decorator has not the scope
            # of the function
            d.parent = self.use_as_parent
        self.subscopes.append(sub)
        return sub

    def add_statement(self, stmt):
        """
        Used to add a Statement or a Scope.
        A statement would be a normal command (Statement) or a Scope (Flow).
        """
        stmt.parent = self.use_as_parent
        self.statements.append(stmt)
        return stmt

    def add_docstr(self, string):
        """ Clean up a docstring """
        self.docstr = cleandoc(literal_eval(string))

    def add_import(self, imp):
        self.imports.append(imp)
        imp.parent = self.use_as_parent

    def get_imports(self):
        """ Gets also the imports within flow statements """
        i = [] + self.imports
        for s in self.statements:
            if isinstance(s, Scope):
                i += s.get_imports()
        return i

    def get_code(self, first_indent=False, indention='    '):
        """
        :return: Returns the code of the current scope.
        :rtype: str
        """
        string = ""
        if len(self.docstr) > 0:
            string += '"""' + self.docstr + '"""\n'
        for i in self.imports:
            string += i.get_code()
        for sub in self.subscopes:
            string += sub.get_code(first_indent=True, indention=indention)

        returns = self.returns if hasattr(self, 'returns') else []
        ret_str = '' if isinstance(self, Lambda) else 'return '
        for stmt in self.statements + returns:
            string += (ret_str if stmt in returns else '') + stmt.get_code()

        if first_indent:
            string = common.indent_block(string, indention=indention)
        return string

    @Python3Method
    def get_set_vars(self):
        """
        Get all the names, that are active and accessible in the current
        scope.

        :return: list of Name
        :rtype: list
        """
        n = []
        for stmt in self.statements:
            try:
                n += stmt.get_set_vars(True)
            except TypeError:
                n += stmt.get_set_vars()

        # function and class names
        n += [s.name for s in self.subscopes]

        for i in self.imports:
            if not i.star:
                n += i.get_defined_names()
        return n

    def get_defined_names(self):
        return [n for n in self.get_set_vars()
                  if isinstance(n, Import) or len(n) == 1]

    def is_empty(self):
        """
        :return: True if there are no subscopes, imports and statements.
        :rtype: bool
        """
        return not (self.imports or self.subscopes or self.statements)

    @Python3Method
    def get_statement_for_position(self, pos, include_imports=False):
        checks = self.statements + self.asserts
        if include_imports:
            checks += self.imports
        if self.isinstance(Function):
            checks += self.params + self.decorators
            checks += [r for r in self.returns if r is not None]

        for s in checks:
            if isinstance(s, Flow):
                p = s.get_statement_for_position(pos, include_imports)
                while s.next and not p:
                    s = s.next
                    p = s.get_statement_for_position(pos, include_imports)
                if p:
                    return p
            elif s.start_pos <= pos < s.end_pos:
                return s

        for s in self.subscopes:
            if s.start_pos <= pos <= s.end_pos:
                p = s.get_statement_for_position(pos, include_imports)
                if p:
                    return p

    def __repr__(self):
        try:
            name = self.path
        except AttributeError:
            try:
                name = self.name
            except AttributeError:
                name = self.command

        return "<%s: %s@%s-%s>" % (type(self).__name__, name,
                                    self.start_pos[0], self.end_pos[0])


class Module(object):
    """ For isinstance checks. fast_parser.Module also inherits from this. """
    pass


class SubModule(Scope, Module):
    """
    The top scope, which is always a module.
    Depending on the underlying parser this may be a full module or just a part
    of a module.
    """
    def __init__(self, path, start_pos=(1, 0), top_module=None):
        super(SubModule, self).__init__(self, start_pos)
        self.path = path
        self.global_vars = []
        self._name = None
        self.used_names = {}
        self.temp_used_names = []
        # this may be changed depending on fast_parser
        self.line_offset = 0

        self.use_as_parent = top_module or self

    def add_global(self, name):
        """
        Global means in these context a function (subscope) which has a global
        statement.
        This is only relevant for the top scope.

        :param name: The name of the global.
        :type name: Name
        """
        self.global_vars.append(name)
        # set no parent here, because globals are not defined in this scope.

    def get_set_vars(self):
        n = super(SubModule, self).get_set_vars()
        n += self.global_vars
        return n

    @property
    def name(self):
        """ This is used for the goto function. """
        if self._name is not None:
            return self._name
        if self.path is None:
            string = ''  # no path -> empty name
        else:
            sep = (re.escape(os.path.sep),) * 2
            r = re.search(r'([^%s]*?)(%s__init__)?(\.py|\.so)?$' % sep,
                                                                self.path)
            string = r.group(1)
        names = [(string, (0, 0))]
        self._name = Name(self, names, self.start_pos, self.end_pos,
                                                            self.use_as_parent)
        return self._name

    def is_builtin(self):
        return not (self.path is None or self.path.endswith('.py'))


class Class(Scope):
    """
    Used to store the parsed contents of a python class.

    :param name: The Class name.
    :type name: str
    :param supers: The super classes of a Class.
    :type supers: list
    :param start_pos: The start position (line, column) of the class.
    :type start_pos: tuple(int, int)
    """
    def __init__(self, module, name, supers, start_pos):
        super(Class, self).__init__(module, start_pos)
        self.name = name
        name.parent = self.use_as_parent
        self.supers = supers
        for s in self.supers:
            s.parent = self.use_as_parent
        self.decorators = []

    def get_code(self, first_indent=False, indention='    '):
        string = "\n".join('@' + stmt.get_code() for stmt in self.decorators)
        string += 'class %s' % (self.name)
        if len(self.supers) > 0:
            sup = ','.join(stmt.code for stmt in self.supers)
            string += '(%s)' % sup
        string += ':\n'
        string += super(Class, self).get_code(True, indention)
        if self.is_empty():
            string += "pass\n"
        return string


class Function(Scope):
    """
    Used to store the parsed contents of a python function.

    :param name: The Function name.
    :type name: str
    :param params: The parameters (Statement) of a Function.
    :type params: list
    :param start_pos: The start position (line, column) the Function.
    :type start_pos: tuple(int, int)
    :param docstr: The docstring for the current Scope.
    :type docstr: str
    """
    def __init__(self, module, name, params, start_pos, annotation):
        super(Function, self).__init__(module, start_pos)
        self.name = name
        if name is not None:
            name.parent = self.use_as_parent
        self.params = params
        for p in params:
            p.parent = self.use_as_parent
            p.parent_function = self.use_as_parent
        self.decorators = []
        self.returns = []
        self.is_generator = False
        self.listeners = set()  # not used here, but in evaluation.

        if annotation is not None:
            annotation.parent = self.use_as_parent
            self.annotation = annotation

    def get_code(self, first_indent=False, indention='    '):
        string = "\n".join('@' + stmt.get_code() for stmt in self.decorators)
        params = ','.join([stmt.code for stmt in self.params])
        string += "def %s(%s):\n" % (self.name, params)
        string += super(Function, self).get_code(True, indention)
        if self.is_empty():
            string += "pass\n"
        return string

    def get_set_vars(self):
        n = super(Function, self).get_set_vars()
        for p in self.params:
            try:
                n.append(p.get_name())
            except IndexError:
                debug.warning("multiple names in param %s" % n)
        return n

    def get_call_signature(self, width=72):
        """
        Generate call signature of this function.

        :param width: Fold lines if a line is longer than this value.
        :type width: int

        :rtype: str
        """
        l = self.name.names[-1] + '('
        lines = []
        for (i, p) in enumerate(self.params):
            code = p.get_code(False)
            if i != len(self.params) - 1:
                code += ', '
            if len(l + code) > width:
                lines.append(l[:-1] if l[-1] == ' ' else l)
                l = code
            else:
                l += code
        if l:
            lines.append(l)
        lines[-1] += ')'
        return '\n'.join(lines)

    @property
    def doc(self):
        """ Return a document string including call signature. """
        return '%s\n\n%s' % (self.get_call_signature(), self.docstr)


class Lambda(Function):
    def __init__(self, module, params, start_pos):
        super(Lambda, self).__init__(module, None, params, start_pos, None)

    def get_code(self, first_indent=False, indention='    '):
        params = ','.join([stmt.code for stmt in self.params])
        string = "lambda %s:" % params
        return string + super(Function, self).get_code(indention=indention)

    def __repr__(self):
        return "<%s @%s (%s-%s)>" % (type(self).__name__, self.start_pos[0],
                                        self.start_pos[1], self.end_pos[1])


class Flow(Scope):
    """
    Used to describe programming structure - flow statements,
    which indent code, but are not classes or functions:

    - for
    - while
    - if
    - try
    - with

    Therefore statements like else, except and finally are also here,
    they are now saved in the root flow elements, but in the next variable.

    :param command: The flow command, if, while, else, etc.
    :type command: str
    :param inits: The initializations of a flow -> while 'statement'.
    :type inits: list(Statement)
    :param start_pos: Position (line, column) of the Flow statement.
    :type start_pos: tuple(int, int)
    :param set_vars: Local variables used in the for loop (only there).
    :type set_vars: list
    """
    def __init__(self, module, command, inits, start_pos, set_vars=None):
        self.next = None
        self.command = command
        super(Flow, self).__init__(module, start_pos)
        self._parent = None
        # These have to be statements, because of with, which takes multiple.
        self.inits = inits
        for s in inits:
            s.parent = self.use_as_parent
        if set_vars is None:
            self.set_vars = []
        else:
            self.set_vars = set_vars
            for s in self.set_vars:
                s.parent.parent = self.use_as_parent
                s.parent = self.use_as_parent

    @property
    def parent(self):
        return self._parent

    @parent.setter
    def parent(self, value):
        self._parent = value
        if self.next:
            self.next.parent = value

    def get_code(self, first_indent=False, indention='    '):
        stmts = []
        for s in self.inits:
            stmts.append(s.get_code(new_line=False))
        stmt = ', '.join(stmts)
        string = "%s %s:\n" % (self.command, stmt)
        string += super(Flow, self).get_code(True, indention)
        if self.next:
            string += self.next.get_code()
        return string

    def get_set_vars(self, is_internal_call=False):
        """
        Get the names for the flow. This includes also a call to the super
        class.
        :param is_internal_call: defines an option for internal files to crawl\
        through this class. Normally it will just call its superiors, to\
        generate the output.
        """
        if is_internal_call:
            n = list(self.set_vars)
            for s in self.inits:
                n += s.set_vars
            if self.next:
                n += self.next.get_set_vars(is_internal_call)
            n += super(Flow, self).get_set_vars()
            return n
        else:
            return self.get_parent_until((Class, Function)).get_set_vars()

    def get_imports(self):
        i = super(Flow, self).get_imports()
        if self.next:
            i += self.next.get_imports()
        return i

    def set_next(self, next):
        """ Set the next element in the flow, those are else, except, etc. """
        if self.next:
            return self.next.set_next(next)
        else:
            self.next = next
            self.next.parent = self.parent
            return next


class ForFlow(Flow):
    """
    Used for the for loop, because there are two statement parts.
    """
    def __init__(self, module, inits, start_pos, set_stmt, is_list_comp=False):
        super(ForFlow, self).__init__(module, 'for', inits, start_pos,
                                        set_stmt.used_vars)
        self.set_stmt = set_stmt
        self.is_list_comp = is_list_comp

    def get_code(self, first_indent=False, indention=" " * 4):
        vars = ",".join(x.get_code() for x in self.set_vars)
        stmts = []
        for s in self.inits:
            stmts.append(s.get_code(new_line=False))
        stmt = ', '.join(stmts)
        s = "for %s in %s:\n" % (vars, stmt)
        return s + super(Flow, self).get_code(True, indention)


class Import(Simple):
    """
    Stores the imports of any Scopes.

    >>> 1+1
    2

    :param start_pos: Position (line, column) of the Import.
    :type start_pos: tuple(int, int)
    :param namespace: The import, can be empty if a star is given
    :type namespace: Name
    :param alias: The alias of a namespace(valid in the current namespace).
    :type alias: Name
    :param from_ns: Like the namespace, can be equally used.
    :type from_ns: Name
    :param star: If a star is used -> from time import *.
    :type star: bool
    :param defunct: An Import is valid or not.
    :type defunct: bool
    """
    def __init__(self, module, start_pos, end_pos, namespace, alias=None,
                 from_ns=None, star=False, relative_count=0, defunct=False):
        super(Import, self).__init__(module, start_pos, end_pos)

        self.namespace = namespace
        self.alias = alias
        self.from_ns = from_ns
        for n in [namespace, alias, from_ns]:
            if n:
                n.parent = self.use_as_parent

        self.star = star
        self.relative_count = relative_count
        self.defunct = defunct

    def get_code(self, new_line=True):
        # in case one of the names is None
        alias = self.alias or ''
        namespace = self.namespace or ''
        from_ns = self.from_ns or ''

        if self.alias:
            ns_str = "%s as %s" % (namespace, alias)
        else:
            ns_str = str(namespace)

        nl = '\n' if new_line else ''
        if self.from_ns or self.relative_count:
            if self.star:
                ns_str = '*'
            dots = '.' * self.relative_count
            return "from %s%s import %s%s" % (dots, from_ns, ns_str, nl)
        else:
            return "import %s%s" % (ns_str, nl)

    def get_defined_names(self):
        if self.defunct:
            return []
        if self.star:
            return [self]
        if self.alias:
            return [self.alias]
        if len(self.namespace) > 1:
            o = self.namespace
            n = Name(self.module, [(o.names[0], o.start_pos)], o.start_pos,
                                                o.end_pos, parent=o.parent)
            return [n]
        else:
            return [self.namespace]

    def get_set_vars(self):
        return self.get_defined_names()

    def get_all_import_names(self):
        n = []
        if self.from_ns:
            n.append(self.from_ns)
        if self.namespace:
            n.append(self.namespace)
        if self.alias:
            n.append(self.alias)
        return n


class Statement(Simple):
    """
    This is the class for all the possible statements. Which means, this class
    stores pretty much all the Python code, except functions, classes, imports,
    and flow functions like if, for, etc.

    :param code: The full code of a statement. This is import, if one wants \
    to execute the code at some level.
    :param code: str
    :param set_vars: The variables which are defined by the statement.
    :param set_vars: str
    :param used_funcs: The functions which are used by the statement.
    :param used_funcs: str
    :param used_vars: The variables which are used by the statement.
    :param used_vars: str
    :param token_list: Token list which is also peppered with Name.
    :param token_list: list
    :param start_pos: Position (line, column) of the Statement.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('used_funcs', 'code', 'token_list', 'used_vars',
                 'set_vars', '_assignment_calls', '_assignment_details',
                 '_assignment_calls_calculated')

    def __init__(self, module, code, set_vars, used_funcs, used_vars,
                                token_list, start_pos, end_pos, parent=None):
        super(Statement, self).__init__(module, start_pos, end_pos)
        # TODO remove code -> much cleaner
        self.code = code
        self.used_funcs = used_funcs
        self.used_vars = used_vars
        self.token_list = token_list
        for s in set_vars + used_funcs + used_vars:
            s.parent = self.use_as_parent
        self.set_vars = self._remove_executions_from_set_vars(set_vars)
        self.parent = parent

        # cache
        self._assignment_calls = None
        self._assignment_details = None
        # this is important for other scripts
        self._assignment_calls_calculated = False

    def _remove_executions_from_set_vars(self, set_vars):
        """
        Important mainly for assosiative arrays:

        >>> a = 3
        >>> b = {}
        >>> b[a] = 3

        `a` is in this case not a set_var, it is used to index the dict.
        """

        if not set_vars:
            return set_vars
        result = set(set_vars)
        last = None
        in_execution = 0
        for tok in self.token_list:
            if isinstance(tok, Name):
                if tok not in result:
                    break
                if in_execution:
                    result.remove(tok)
            elif isinstance(tok, tuple):
                tok = tok[1]
            if tok in ['(', '['] and isinstance(last, Name):
                in_execution += 1
            elif tok in [')', ']'] and in_execution > 0:
                in_execution -= 1
            last = tok
        return list(result)

    def get_code(self, new_line=True):
        if new_line:
            return self.code + '\n'
        else:
            return self.code

    def get_set_vars(self):
        """ Get the names for the statement. """
        return list(self.set_vars)

    @property
    def assignment_details(self):
        if self._assignment_details is None:
            # normally, this calls sets this variable
            self.get_assignment_calls()
        # it may not have been set by get_assignment_calls -> just use an empty
        # array
        return self._assignment_details or []

    def is_global(self):
        # first keyword of the first token is global -> must be a global
        return str(self.token_list[0]) == "global"

    def get_assignment_calls(self):
        """
        This is not done in the main parser, because it might be slow and
        most of the statements won't need this data anyway. This is something
        'like' a lazy execution.

        This is not really nice written, sorry for that. If you plan to replace
        it and make it nicer, that would be cool :-)
        """
        brackets = {'(': Array.TUPLE, '[': Array.LIST, '{': Array.SET}
        closing_brackets = [')', '}', ']']

        def parse_array(token_iterator, array_type, start_pos):
            arr = Array(start_pos, array_type)
            maybe_dict = array_type == Array.SET
            while True:
                statement, break_tok = parse_statement(token_iterator,
                                                        start_pos, maybe_dict)
                if statement is not None:
                    is_key = maybe_dict and break_tok == ':'
                    arr.add_statement(statement, is_key)
            return arr

        def parse_statement(token_iterator, start_pos, maybe_dict=False):
            token_list = []
            level = 0
            tok = None
            for i, tok_temp in token_iterator:
                try:
                    token_type, tok, start_tok_pos = tok_temp
                except TypeError:
                    # the token is a Name, which has already been parsed
                    tok = tok_temp
                    end_pos = tok.end_pos
                else:
                    if tok in closing_brackets:
                        level -= 1
                    elif tok in brackets.keys():
                        level += 1

                    if level == 0 and tok in closing_brackets + (',',):
                        break
                token_list.append(tok_temp)

            if not token_list:
                return None, tok

            statement = Statement(self.module, "XXX" + self.code, [], [], [],
                                            token_list, start_pos, end_pos)
            statement.parent = self.parent
            return statement

        if self._assignment_calls_calculated:
            return self._assignment_calls
        self._assignment_details = []
        top = result = Array(self.start_pos, Array.NOARRAY, self)
        level = 0
        is_chain = False
        close_brackets = False

        is_call = lambda: type(result) == Call
        is_call_or_close = lambda: is_call() or close_brackets

        token_iterator = enumerate(self.token_list)
        for i, tok_temp in token_iterator:
            #print 'tok', tok_temp, result
            if isinstance(tok_temp, ListComprehension):
                result.add_to_current_field(tok_temp)
                continue
            try:
                token_type, tok, start_pos = tok_temp
            except TypeError:
                # the token is a Name, which has already been parsed
                tok = tok_temp
                token_type = None
                start_pos = tok.start_pos
            except ValueError:
                debug.warning("unkown value, shouldn't happen",
                                tok_temp, type(tok_temp))
                raise
            else:
                if level == 0 and tok.endswith('=') \
                                and not tok in ['>=', '<=', '==', '!=']:
                    # This means, there is an assignment here.

                    # Add assignments, which can be more than one
                    self._assignment_details.append((tok, top))
                    # All these calls wouldn't be important if nonlocal would
                    # exist. -> Initialize the first items again.
                    end_pos = start_pos[0], start_pos[1] + len(tok)
                    top = result = Array(end_pos, Array.NOARRAY, self)
                    level = 0
                    close_brackets = False
                    is_chain = False
                    continue
                elif tok == 'as':
                    next(token_iterator, None)
                    continue

            if level >= 0:
                if tok in brackets.keys():  # brackets
                    level += 1
                elif tok in closing_brackets:
                    level -= 1
                if level == 0:
                    pass

            # here starts the statement creation madness!
            is_literal = token_type in [tokenize.STRING, tokenize.NUMBER]
            if isinstance(tok, Name) or is_literal:
                _tok = tok
                c_type = Call.NAME
                if is_literal:
                    tok = literal_eval(tok)
                    if token_type == tokenize.STRING:
                        c_type = Call.STRING
                    elif token_type == tokenize.NUMBER:
                        c_type = Call.NUMBER

                if is_chain:
                    call = Call(tok, c_type, start_pos, parent=result)
                    result = result.set_next_chain_call(call)
                    is_chain = False
                    close_brackets = False
                else:
                    if close_brackets:
                        result = result.parent
                        close_brackets = False
                    if type(result) == Call:
                        result = result.parent
                    call = Call(tok, c_type, start_pos, parent=result)
                    result.add_to_current_field(call)
                    result = call
                tok = _tok
            elif tok in brackets.keys():  # brackets
                level += 1
                if is_call_or_close():
                    result = Array(start_pos, brackets[tok], parent=result)
                    result = result.parent.add_execution(result)
                    close_brackets = False
                else:
                    result = Array(start_pos, brackets[tok], parent=result)
                    result.parent.add_to_current_field(result)
            elif tok == ':':
                while is_call_or_close():
                    result = result.parent
                    close_brackets = False
                if result.type == Array.LIST:  # [:] lookups
                    result.add_to_current_field(tok)
                else:
                    result.add_dictionary_key()
            elif tok == '.':
                if close_brackets and result.parent != top:
                    # only get out of the array, if it is a array execution
                    result = result.parent
                    close_brackets = False
                is_chain = True
            elif tok == ',':
                while is_call_or_close():
                    result = result.parent
                    close_brackets = False
                result.add_field((start_pos[0], start_pos[1] + 1))
                # important - it cannot be empty anymore
                if result.type == Array.NOARRAY:
                    result.type = Array.TUPLE
            elif tok in closing_brackets:
                while is_call_or_close():
                    result = result.parent
                    close_brackets = False
                if tok == '}' and not len(result):
                    # this is a really special case - empty brackets {} are
                    # always dictionaries and not sets.
                    result.type = Array.DICT
                level -= 1
                result.end_pos = start_pos[0], start_pos[1] + 1
                close_brackets = True
            else:
                while is_call_or_close():
                    result = result.parent
                    close_brackets = False
                if tok != '\n':
                    result.add_to_current_field(tok)

        if level != 0:
            debug.warning("Brackets don't match: %s."
                          "This is not normal behaviour." % level)

        if self.token_list:
            while result is not None:
                try:
                    result.end_pos = start_pos[0], start_pos[1] + len(tok)
                except TypeError:
                    result.end_pos = tok.end_pos
                result = result.parent
        else:
            result.end_pos = self.end_pos

        self._assignment_calls_calculated = True
        self._assignment_calls = top
        return top


class Param(Statement):
    """
    The class which shows definitions of params of classes and functions.
    But this is not to define function calls.
    """
    __slots__ = ('position_nr', 'is_generated', 'annotation_stmt',
                 'parent_function')

    def __init__(self, module, code, set_vars, used_funcs, used_vars,
                 token_list, start_pos, end_pos):
        super(Param, self).__init__(module, code, set_vars, used_funcs,
                                used_vars, token_list, start_pos, end_pos)

        # this is defined by the parser later on, not at the initialization
        # it is the position in the call (first argument, second...)
        self.position_nr = None
        self.is_generated = False
        self.annotation_stmt = None
        self.parent_function = None

    def add_annotation(self, annotation_stmt):
        annotation_stmt.parent = self.use_as_parent
        self.annotation_stmt = annotation_stmt

    def get_name(self):
        """ get the name of the param """
        n = self.set_vars or self.used_vars
        if len(n) > 1:
            debug.warning("Multiple param names (%s)." % n)
        return n[0]


class Call(Base):
    """
    `Call` contains a call, e.g. `foo.bar` and owns the executions of those
    calls, which are `Array`s.
    """
    NAME = 1
    NUMBER = 2
    STRING = 3

    def __init__(self, name, type, start_pos, parent_stmt=None, parent=None):
        self.name = name
        # parent is not the oposite of next. The parent of c: a = [b.c] would
        # be an array.
        self.parent = parent
        self.type = type
        self.start_pos = start_pos

        self.next = None
        self.execution = None
        self._parent_stmt = parent_stmt

    @property
    def start_pos(self):
        offset = self.parent_stmt.module.line_offset
        return offset + self._start_pos[0], self._start_pos[1]

    @start_pos.setter
    def start_pos(self, value):
        self._start_pos = value

    @property
    def parent_stmt(self):
        if self._parent_stmt is not None:
            return self._parent_stmt
        elif self.parent:
            return self.parent.parent_stmt
        else:
            return None

    @parent_stmt.setter
    def parent_stmt(self, value):
        self._parent_stmt = value

    def set_next_chain_call(self, call):
        """ Adds another part of the statement"""
        self.next = call
        call.parent = self.parent
        return call

    def add_execution(self, call):
        """
        An execution is nothing else than brackets, with params in them, which
        shows access on the internals of this name.
        """
        self.execution = call
        # there might be multiple executions, like a()[0], in that case, they
        # have the same parent. Otherwise it's not possible to parse proper.
        if self.parent.execution == self:
            call.parent = self.parent
        else:
            call.parent = self
        return call

    def generate_call_path(self):
        """ Helps to get the order in which statements are executed. """
        # TODO include previous nodes? As an option?
        try:
            for name_part in self.name.names:
                yield name_part
        except AttributeError:
            yield self
        if self.execution is not None:
            for y in self.execution.generate_call_path():
                yield y
        if self.next is not None:
            for y in self.next.generate_call_path():
                yield y

    def get_code(self):
        if self.type == Call.NAME:
            s = self.name.get_code()
        else:
            s = repr(self.name)
        if self.execution is not None:
            s += '(%s)' % self.execution.get_code()
        if self.next is not None:
            s += self.next.get_code()
        return s

    def __repr__(self):
        return "<%s: %s>" % \
                (type(self).__name__, self.name)


class Array(Call):
    """
    Describes the different python types for an array, but also empty
    statements. In the Python syntax definitions this type is named 'atom'.
    http://docs.python.org/py3k/reference/grammar.html
    Array saves sub-arrays as well as normal operators and calls to methods.

    :param array_type: The type of an array, which can be one of the constants\
    below.
    :type array_type: int
    """
    NOARRAY = None  # just brackets, like `1 * (3 + 2)`
    TUPLE = 'tuple'
    LIST = 'list'
    DICT = 'dict'
    SET = 'set'

    def __init__(self,  start_pos, arr_type=NOARRAY, parent_stmt=None,
                                                   parent=None, values=None):
        super(Array, self).__init__(None, arr_type, start_pos, parent_stmt,
                                                                    parent)

        self.values = values if values else []
        self.arr_el_pos = []
        self.keys = []
        self._end_pos = None, None

    @property
    def end_pos(self):
        if None in self._end_pos:
            return self._end_pos
        offset = self.parent_stmt.module.line_offset
        return offset + self._end_pos[0], self._end_pos[1]

    @end_pos.setter
    def end_pos(self, value):
        self._end_pos = value

    def add_field(self, start_pos):
        """
        Just add a new field to the values.

        Each value has a sub-array, because there may be different tokens in
        one array.
        """
        self.arr_el_pos.append(start_pos)
        self.statements.append([])

    def add_statement(self, statement, is_key=False):
        """Just add a new statement"""
        statement.parent = self
        if is_key:
            self.keys.append(statement)
        else:
            self.values.append(statement)

    def add_to_current_field(self, tok):
        """ Adds a token to the latest field (in content). """
        if not self.values:
            # An empty round brace is just a tuple, filled it is unnecessary.
            if self.type == Array.TUPLE:
                self.type = Array.NOARRAY
            # Add the first field, this is done here, because if nothing
            # gets added, the list is empty, which is also needed sometimes.
            self.values.append([])
        self.values[-1].append(tok)

    def add_dictionary_key(self):
        """
        Only used for dictionaries, automatically adds the tokens added by now
        from the values to keys, because the parser works this way.
        """
        if self.type in (Array.LIST, Array.TUPLE):
            return  # these are basically code errors, just ignore
        self.keys.append(self.values.pop())
        if self.type == Array.SET:
            self.type = Array.DICT
        self.values.append([])

    def get_only_subelement(self):
        """
        Returns the only element that an array contains. If it contains
        more than one element, raise an exception.
        """
        if len(self.values) != 1 or len(self.values[0]) != 1:
            raise AttributeError("More than one value found")
        return self.values[0][0]

    @staticmethod
    def is_type(instance, *types):
        """
        This is not only used for calls on the actual object, but for
        ducktyping, to invoke this function with anything as `self`.
        """
        if isinstance(instance, Array):
            if instance.type in types:
                return True
        return False

    def __len__(self):
        return len(self.values)

    def __getitem__(self, key):
        return self.values[key]

    def __iter__(self):
        if self.type == self.DICT:
            return iter(zip(self.keys, self.values))
        else:
            return iter(self.values)

    def get_code(self):
        def to_str(el):
            try:
                return el.get_code()
            except AttributeError:
                return str(el)

        map = {Array.NOARRAY: '%s',
               Array.TUPLE: '(%s)',
               Array.LIST: '[%s]',
               Array.DICT: '{%s}',
               Array.SET: '{%s}'
              }
        inner = []
        for i, value in enumerate(self.values):
            s = ''
            try:
                key = self.keys[i]
            except IndexError:
                pass
            else:
                for el in key[i]:
                    s += to_str(el)
            for el in value:
                s += to_str(el)
            inner.append(s)
        return map[self.type] % ', '.join(inner)

    def __repr__(self):
        if self.type == self.NOARRAY:
            typ = 'noarray'
        else:
            typ = self.type
        return "<%s: %s%s>" % (type(self).__name__, typ, self.values)


class NamePart(str):
    """
    A string. Sometimes it is important to know if the string belongs to a name
    or not.
    """
    # Unfortunately there's no way to use slots for str (non-zero __itemsize__)
    # -> http://utcc.utoronto.ca/~cks/space/blog/python/IntSlotsPython3k
    #__slots__ = ('_start_pos', 'parent')
    def __new__(cls, s, parent, start_pos):
        self = super(NamePart, cls).__new__(cls, s)
        self._start_pos = start_pos
        self.parent = parent
        return self

    @property
    def start_pos(self):
        offset = self.parent.module.line_offset
        return offset + self._start_pos[0], self._start_pos[1]

    @property
    def end_pos(self):
        return self.start_pos[0], self.start_pos[1] + len(self)

    def __getnewargs__(self):
        return str(self), self.parent, self._start_pos


class Name(Simple):
    """
    Used to define names in python.
    Which means the whole namespace/class/function stuff.
    So a name like "module.class.function"
    would result in an array of [module, class, function]
    """
    __slots__ = ('names',)

    def __init__(self, module, names, start_pos, end_pos, parent=None):
        super(Name, self).__init__(module, start_pos, end_pos)
        self.names = tuple(n if isinstance(n, NamePart) else
                                NamePart(n[0], self, n[1]) for n in names)
        if parent is not None:
            self.parent = parent

    def get_code(self):
        """ Returns the names in a full string format """
        return ".".join(self.names)

    def __str__(self):
        return self.get_code()

    def __len__(self):
        return len(self.names)


class ListComprehension(object):
    """ Helper class for list comprehensions """
    def __init__(self, stmt, middle, input):
        self.stmt = stmt
        self.middle = middle
        self.input = input

    def __repr__(self):
        return "<%s: %s>" % \
                (type(self).__name__, self.get_code())

    def get_code(self):
        statements = self.stmt, self.middle, self.input
        code = [s.get_code().replace('\n', '') for s in statements]
        return "%s for %s in %s" % tuple(code)
