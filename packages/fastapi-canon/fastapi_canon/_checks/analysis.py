"""Explicit-exception propagation for the supported, statically resolved subset."""

import ast
from dataclasses import dataclass, field

from .source import _Definition, _Environment, _Source


@dataclass(frozen=True)
class _Escape:
    exception: str
    trace: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class _Unknown:
    message: str
    function: str
    line: int


@dataclass
class _Flow:
    escapes: list[_Escape] = field(default_factory=list)
    unknowns: list[_Unknown] = field(default_factory=list)

    def extend(self, other: "_Flow") -> None:
        self.escapes.extend(other.escapes)
        self.unknowns.extend(other.unknowns)


class _Analyzer:
    def __init__(self, source: _Source) -> None:
        self.source = source
        self._summaries: dict[
            str, tuple[tuple[_Escape, ...], tuple[_Unknown, ...], frozenset[str]]
        ] = {}

    def function(self, function: _Definition, stack: tuple[str, ...] = ()) -> _Flow:
        if function.name in stack:
            return _Flow(
                unknowns=[
                    _Unknown("recursive call", function.name, function.node.lineno)
                ]
            )
        if function.name in self._summaries:
            escapes, unknowns, modules = self._summaries[function.name]
            self.source.touched.update(modules)
            return _Flow(list(escapes), list(unknowns))
        previous = self.source.touched
        self.source.touched = set()
        try:
            flow = self._analyze_function(function, stack)
            # The report retains the first path per exception. Propagation and
            # catching depend on exception types, not duplicate paths to them.
            first: dict[str, _Escape] = {}
            for item in flow.escapes:
                first.setdefault(item.exception, item)
            flow.escapes = list(first.values())
            flow.unknowns = list(dict.fromkeys(flow.unknowns))
            if not any(item.message == "recursive call" for item in flow.unknowns):
                self._summaries[function.name] = (
                    tuple(flow.escapes),
                    tuple(flow.unknowns),
                    frozenset(self.source.touched),
                )
            return flow
        finally:
            previous.update(self.source.touched)
            self.source.touched = previous

    def _analyze_function(self, function: _Definition, stack: tuple[str, ...]) -> _Flow:
        node = function.node
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        flow = _Flow()
        for decorator in node.decorator_list:
            name = self.source.resolve(decorator, function.module)
            if name == "abc.abstractmethod":
                return _Flow(
                    unknowns=[
                        _Unknown("abstract implementation", function.name, node.lineno)
                    ]
                )
            if name not in {
                "dishka.integrations.fastapi.inject",
                "builtins.staticmethod",
                "builtins.classmethod",
                "builtins.property",
            } and not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in _ROUTE_METHODS
            ):
                flow.unknowns.append(
                    _Unknown("unresolved decorator", function.name, decorator.lineno)
                )
        if any(
            isinstance(child, ast.Yield | ast.YieldFrom)
            for child in self._executed_nodes(node.body)
        ):
            flow.unknowns.append(
                _Unknown(
                    "generator execution is outside the HTTP response boundary",
                    function.name,
                    node.lineno,
                )
            )
            return flow
        env = self.source.environment(function)
        flow.extend(self.block(node.body, function, env, (*stack, function.name)))
        return flow

    @staticmethod
    def _executed_nodes(body: list[ast.stmt]) -> list[ast.AST]:
        result: list[ast.AST] = []

        def visit(node: ast.AST) -> None:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
            ):
                return
            result.append(node)
            for child in ast.iter_child_nodes(node):
                visit(child)

        for node in body:
            visit(node)
        return result

    def expression(
        self,
        node: ast.expr,
        function: _Definition,
        env: _Environment,
        stack: tuple[str, ...],
        *,
        awaited: bool = False,
    ) -> _Flow:
        flow = _Flow()
        if isinstance(node, ast.Await):
            return self.expression(node.value, function, env, stack, awaited=True)
        if isinstance(node, ast.Lambda):
            return flow
        if isinstance(
            node,
            ast.GeneratorExp
            | ast.ListComp
            | ast.SetComp
            | ast.DictComp
            | ast.NamedExpr,
        ):
            flow.unknowns.append(
                _Unknown(
                    f"unsupported expression: {type(node).__name__}",
                    function.name,
                    node.lineno,
                )
            )
        if isinstance(node, ast.Call):
            for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                flow.extend(self.expression(argument, function, env, stack))
            name = self.source.resolve(node.func, function.module, env)
            definition = self.source.definitions.get(name or "")
            if name and name.startswith("builtins.") and self.source.exception(name):
                return flow
            if definition and isinstance(definition.node, ast.ClassDef):
                if definition.node.decorator_list or definition.node.keywords:
                    flow.unknowns.append(
                        _Unknown(
                            "class decorators or metaclasses may change construction",
                            function.name,
                            node.lineno,
                        )
                    )
                constructor = self.source.method(definition.name, "__init__")
                if constructor:
                    called = self.function(constructor, stack)
                    flow.escapes.extend(
                        _Escape(item.exception, (function.name, *item.trace), item.line)
                        for item in called.escapes
                    )
                    flow.unknowns.extend(called.unknowns)
                elif self.source.mro(definition.name) is None:
                    flow.unknowns.append(
                        _Unknown(
                            f"unresolved constructor: {name}",
                            function.name,
                            node.lineno,
                        )
                    )
            elif definition:
                if definition.owner and isinstance(node.func, ast.Attribute):
                    receiver = self.source.resolve(
                        node.func.value, function.module, env
                    )
                    if receiver and receiver != definition.owner:
                        flow.unknowns.append(
                            _Unknown(
                                "inherited method dispatch may use subclass overrides",
                                function.name,
                                node.lineno,
                            )
                        )
                if isinstance(definition.node, ast.AsyncFunctionDef) and not awaited:
                    flow.unknowns.append(
                        _Unknown(
                            "coroutine execution is deferred until awaited",
                            function.name,
                            node.lineno,
                        )
                    )
                    return flow
                called = self.function(definition, stack)
                flow.escapes.extend(
                    _Escape(item.exception, (function.name, *item.trace), item.line)
                    for item in called.escapes
                )
                flow.unknowns.extend(called.unknowns)
            else:
                flow.unknowns.append(
                    _Unknown(
                        f"unresolved call: {ast.unparse(node.func)}",
                        function.name,
                        node.lineno,
                    )
                )
            return flow
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                flow.extend(self.expression(child, function, env, stack))
        if isinstance(node, ast.Attribute):
            name = self.source.resolve(node, function.module, env)
            member = self.source.definitions.get(name or "")
            if (
                member
                and isinstance(member.node, ast.FunctionDef)
                and any(
                    self.source.resolve(decorator, member.module) == "builtins.property"
                    for decorator in member.node.decorator_list
                )
            ):
                flow.extend(self.function(member, stack))
        return flow

    def block(
        self,
        body: list[ast.stmt],
        function: _Definition,
        env: _Environment,
        stack: tuple[str, ...],
        reraised: tuple[_Escape, ...] = (),
    ) -> _Flow:
        flow = _Flow()
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                # Nested definitions are not executed, but their bodies cannot be resolved.
                env[node.name] = ""
                for expression in node.decorator_list:
                    flow.extend(self.expression(expression, function, env, stack))
                if isinstance(node, ast.ClassDef):
                    flow.unknowns.append(
                        _Unknown(
                            "nested class construction", function.name, node.lineno
                        )
                    )
                else:
                    for expression in [
                        *node.args.defaults,
                        *(value for value in node.args.kw_defaults if value),
                    ]:
                        flow.extend(self.expression(expression, function, env, stack))
                continue
            if isinstance(node, ast.Raise):
                if node.exc is None:
                    if reraised:
                        flow.escapes.extend(reraised)
                    else:
                        flow.unknowns.append(
                            _Unknown("unresolved re-raise", function.name, node.lineno)
                        )
                else:
                    flow.extend(self.expression(node.exc, function, env, stack))
                    if node.cause:
                        flow.extend(self.expression(node.cause, function, env, stack))
                    target = (
                        node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                    )
                    name = self.source.resolve(target, function.module, env)
                    if name and self.source.exception(name):
                        flow.escapes.append(
                            _Escape(name, (function.name,), node.lineno)
                        )
                    else:
                        flow.unknowns.append(
                            _Unknown(
                                f"unresolved raised exception: {ast.unparse(node.exc)}",
                                function.name,
                                node.lineno,
                            )
                        )
                break
            if isinstance(node, ast.Try):
                attempted = self.block(node.body, function, env.copy(), stack, reraised)
                flow.unknowns.extend(attempted.unknowns)
                remaining = attempted.escapes
                for handler in node.handlers:
                    types = (
                        handler.type.elts
                        if isinstance(handler.type, ast.Tuple)
                        else [handler.type]
                    )
                    names = [
                        self.source.resolve(item, function.module, env)
                        for item in types
                        if item is not None
                    ]
                    unknown_catch = handler.type is not None and (
                        not names
                        or any(
                            name is None or self.source.mro(name) is None
                            for name in names
                        )
                    )
                    if unknown_catch:
                        flow.unknowns.append(
                            _Unknown(
                                "unresolved exception handler",
                                function.name,
                                handler.lineno,
                            )
                        )
                    caught = [
                        item
                        for item in remaining
                        if handler.type is None
                        or any(
                            name in (self.source.mro(item.exception) or [])
                            for name in names
                            if name
                        )
                    ]
                    remaining = [item for item in remaining if item not in caught]
                    handler_env = env.copy()
                    caught_types = {item.exception for item in caught}
                    if handler.name:
                        handler_env[handler.name] = (
                            next(iter(caught_types)) if len(caught_types) == 1 else ""
                        )
                    # Implicit failures (e.g. int(value)) can reach handlers too.
                    flow.extend(
                        self.block(
                            handler.body, function, handler_env, stack, tuple(caught)
                        )
                    )
                flow.escapes.extend(remaining)
                flow.extend(
                    self.block(node.orelse, function, env.copy(), stack, reraised)
                )
                flow.extend(
                    self.block(node.finalbody, function, env.copy(), stack, reraised)
                )
                if any(
                    isinstance(child, ast.Return | ast.Raise)
                    for child in self._executed_nodes(node.finalbody)
                ):
                    flow.unknowns.append(
                        _Unknown(
                            "finally may replace an escaping exception",
                            function.name,
                            node.lineno,
                        )
                    )
                self._invalidate(node, env)
                continue
            if isinstance(node, ast.If):
                flow.extend(self.expression(node.test, function, env, stack))
                flow.extend(
                    self.block(node.body, function, env.copy(), stack, reraised)
                )
                flow.extend(
                    self.block(node.orelse, function, env.copy(), stack, reraised)
                )
                self._invalidate(node, env)
                continue
            if isinstance(node, ast.Assign | ast.AnnAssign):
                if node.value is not None:
                    flow.extend(self.expression(node.value, function, env, stack))
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                resolved = None
                if isinstance(node, ast.AnnAssign):
                    resolved = self.source.annotation(node.annotation, function.module)
                if not resolved and node.value is not None:
                    resolved = self.source.resolve(node.value, function.module, env)
                    if isinstance(node.value, ast.Call):
                        called = self.source.resolve(
                            node.value.func, function.module, env
                        )
                        definition = self.source.definitions.get(called or "")
                        if definition and isinstance(definition.node, ast.ClassDef):
                            resolved = definition.name
                        elif (
                            definition
                            and isinstance(
                                definition.node, ast.FunctionDef | ast.AsyncFunctionDef
                            )
                            and definition.node.returns
                        ):
                            resolved = self.source.annotation(
                                definition.node.returns, definition.module
                            )
                    elif isinstance(node.value, ast.Await) and isinstance(
                        node.value.value, ast.Call
                    ):
                        called = self.source.resolve(
                            node.value.value.func, function.module, env
                        )
                        definition = self.source.definitions.get(called or "")
                        if (
                            definition
                            and isinstance(
                                definition.node, ast.FunctionDef | ast.AsyncFunctionDef
                            )
                            and definition.node.returns
                        ):
                            resolved = self.source.annotation(
                                definition.node.returns, definition.module
                            )
                for target in targets:
                    key = self.source.key(target)
                    env[key] = resolved or ""
                continue
            if isinstance(node, ast.Return):
                if node.value:
                    flow.extend(self.expression(node.value, function, env, stack))
                break
            if isinstance(node, ast.Expr):
                flow.extend(self.expression(node.value, function, env, stack))
                continue
            if isinstance(node, ast.Pass | ast.Break | ast.Continue):
                continue
            # Context managers, loops, match, try*, etc. need more control-flow knowledge.
            flow.unknowns.append(
                _Unknown(
                    f"unsupported statement: {type(node).__name__}",
                    function.name,
                    node.lineno,
                )
            )
            for child in self._executed_nodes([node]):
                if isinstance(child, ast.Raise) and child.exc:
                    target = (
                        child.exc.func if isinstance(child.exc, ast.Call) else child.exc
                    )
                    name = self.source.resolve(target, function.module, env)
                    if name and self.source.exception(name):
                        flow.escapes.append(
                            _Escape(name, (function.name,), child.lineno)
                        )
            self._invalidate(node, env)
        return flow

    def _invalidate(self, node: ast.AST, env: _Environment) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Name | ast.Attribute) and isinstance(
                child.ctx, ast.Store
            ):
                env[self.source.key(child)] = ""


_ROUTE_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}
)
