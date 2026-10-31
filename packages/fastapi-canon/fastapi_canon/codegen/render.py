from __future__ import annotations

import ast
import json
import posixpath
import re
from dataclasses import fields, is_dataclass, replace
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from fastapi_canon.codegen.config import TargetConfig
from fastapi_canon.codegen.ir import GenerationError, Operation, Plan
from fastapi_canon.codegen.names import camel, model_field, pascal, snake
from fastapi_canon.codegen.problems import problem_runtime


def render(plan: Plan, target: TargetConfig) -> dict[str, str]:
    from fastapi_canon.integrations.sse.codegen.enums import with_enums
    from fastapi_canon.integrations.sse.codegen.ir import Contract, Declaration, _Types
    from fastapi_canon.integrations.sse.codegen.python import _Models
    from fastapi_canon.integrations.sse.codegen.python import (
        runtime_source as sse_runtime,
    )
    from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript

    if target.language == "python" and any(
        source.kind == "webrtc" for source in plan.sources
    ):
        raise GenerationError(
            "WebRTC sessions require a TypeScript target; no Python media engine is generated"
        )
    if target.client_name in {model.name for model in plan.models} | {
        source.name + "Client" for source in plan.sources
    }:
        raise GenerationError("client-name collides with a model or binding name")
    schemas = {model.name: model.schema for model in plan.models}
    types = _Types({"components": {"schemas": schemas}})
    declarations = []
    for name in schemas:
        types.parse({"$ref": f"#/components/schemas/{name}"})
    for operation in plan.operations:
        for parameter in operation.parameters:
            declarations.append(
                Declaration(
                    _alias(operation, parameter.name), types.parse(parameter.schema)
                )
            )
        for direction, schema in operation.binding.metadata.get("messages", {}).items():
            declarations.append(
                Declaration(_alias(operation, direction), types.parse(schema))
            )
        if operation.interaction != "session":
            declarations.append(
                Declaration(_alias(operation, "Result"), types.parse(operation.result))
            )
    declarations = [*types.declarations(), *declarations]
    if len({declaration.name for declaration in declarations}) != len(declarations):
        raise GenerationError(
            "An operation type name collides with a model; rename the model or operation"
        )
    contract = with_enums(Contract((), tuple(declarations), ()))
    python = target.language == "python"
    models = (
        _Models(contract.declarations, field_name=model_field).render()
        if python
        else generate_typescript(contract)["types.ts"]
    )
    output = {
        "models.py" if python else "models.ts": models,
        "errors.py" if python else "errors.ts": _errors(plan, target),
    }
    canonical = {model.name for model in plan.models}
    for source in plan.sources:
        if source.kind == "openapi":
            if not any(
                operation.binding.protocol == "sse" for operation in source.operations
            ):
                continue
            from fastapi_canon.integrations.sse.codegen.ir import build_ir

            ir = build_ir(source.document, target=target.language)
            # Only an actual stream creates an SSE binding/runtime.
            if not ir.operations:
                continue
            backend = (
                {
                    "runtime.py": sse_runtime(http_client=target.http_client),
                    "__init__.py": "",
                }
                if python
                else generate_typescript(ir)
            )
            prefix = f"internal/{source.name}_sse"
        elif source.kind == "openrpc":
            from fastapi_canon.integrations.websockets.codegen.ir import (
                build_ir as rpc_ir,
            )
            from fastapi_canon.integrations.websockets.codegen.options import (
                PythonClientOptions,
                TypeScriptClientOptions,
            )
            from fastapi_canon.integrations.websockets.codegen.python import (
                render_files as rpc_python,
            )
            from fastapi_canon.integrations.websockets.codegen.typescript import (
                render_files as rpc_typescript,
            )

            prefix = f"internal/{source.name}_jsonrpc"
            if python:
                backend = rpc_python(
                    _binding_ir(rpc_ir(source.document)),
                    PythonClientOptions(
                        package=target.package + "." + prefix.replace("/", "."),
                        client_name="BindingClient",
                        with_transport="websocket",
                    ),
                )
            else:
                backend = rpc_typescript(
                    _binding_ir(rpc_ir(source.document)),
                    TypeScriptClientOptions(
                        client_name="BindingClient", with_transport="websocket"
                    ),
                )
        else:
            from fastapi_canon.integrations.webrtc.codegen.typescript import (
                generate_typescript as generate,
            )

            prefix = f"internal/{source.name}_webrtc"
            backend = generate(source.document, client_name="BindingClient")
        for path, content in backend.items():
            if python:
                content = _python_backend(
                    content, path, prefix, target.package, canonical
                )
            else:
                content = _typescript_backend(content, path, prefix, canonical)
            if path in {
                "problem.py",
                "internal/problem.py",
                "problem.ts",
                "transport/problem.ts",
            }:
                continue
            output[prefix + "/" + path] = content
    output.update(
        _python_client(plan, target) if python else _typescript_client(plan, target)
    )
    if python:
        output["internal/__init__.py"] = ""
        output["py.typed"] = ""
    runtime = (
        files("fastapi_canon.codegen.templates")
        .joinpath("client.py" if python else "client.ts")
        .read_text(encoding="utf-8")
    )
    if python:
        runtime = runtime.replace(
            "import httpx\n", f"import {target.http_client} as httpx\n"
        )
    if python:
        runtime = runtime.replace("from .problem import", "from ..errors import")
    output["internal/runtime.py" if python else "internal/runtime.ts"] = runtime
    return output


def _errors(plan: Plan, target: TargetConfig) -> str:
    runtime = problem_runtime(target.language)
    if target.language != "python":
        return runtime
    runtime = runtime.replace("def parse_problem(", "def _parse_problem(")
    bindings = ",\n".join(
        f"    {model.problem_type!r}: TypeAdapter(Models.{model.name})"
        for model in plan.models
        if model.problem_type
    )
    return (
        runtime
        + f"""
from pydantic import TypeAdapter, ValidationError
from . import models as Models

_PROBLEM_MODELS: dict[str, TypeAdapter[Any]] = {{
{bindings}
}}

def parse_problem(value: Any, *, embedded: bool = False) -> Any:
    parsed = _parse_problem(value, embedded=embedded)
    adapter = _PROBLEM_MODELS.get(parsed.type)
    if adapter is None:
        return parsed
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise ProtocolError("Invalid declared problem payload") from exc
"""
    )


def _alias(operation: Operation, name: str) -> str:
    return "".join(pascal(part) for part in operation.name) + pascal(name)


def _python_backend(
    content: str, path: str, prefix: str, package: str, canonical: set[str]
) -> str:
    tree = ast.parse(content)
    lines = content.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    base = package + "." + prefix.replace("/", ".")
    module = base + ("." + path[:-3].replace("/", ".") if path != "__init__.py" else "")
    parent = (
        module.removesuffix(".__init__")
        if path.endswith("__init__.py")
        else module.rsplit(".", 1)[0]
    )
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            resolved = node.module or ""
            if node.level:
                parts = parent.split(".")
                resolved = ".".join(
                    [*parts[: len(parts) - node.level + 1], *resolved.split(".")]
                ).rstrip(".")
            if resolved in {base + ".problem", base + ".internal.problem"}:
                names = ", ".join(
                    alias.name + (f" as {alias.asname}" if alias.asname else "")
                    for alias in node.names
                )
                edits.append(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        f"from {package}.errors import {names}\n",
                    )
                )
        if path == "models.py":
            name = (
                node.name.id
                if isinstance(node, ast.TypeAlias)
                else node.target.id
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                else node.name
                if isinstance(node, ast.ClassDef)
                else node.targets[0].id
                if isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                else None
            )
            if name in canonical:
                edits.append(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        f"from {package}.models import {name} as {name}\n",
                    )
                )
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start - 1 : end] = [replacement]
    return "".join(lines)


def _typescript_backend(
    content: str, path: str, prefix: str, canonical: set[str]
) -> str:
    if prefix.endswith("_webrtc") and path == "runtime.ts":
        content = (
            'import { TransportError } from "../../errors.js";\n'
            + content.replace(
                "class RtcError extends Error", "class RtcError extends TransportError"
            )
        )

    def module(match: re.Match[str]) -> str:
        value = match.group(2)
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(path), value)
        ).removesuffix(".js")
        if resolved not in {"problem", "transport/problem"}:
            return match.group(0)
        relative = posixpath.relpath(
            "errors.js", posixpath.dirname(prefix + "/" + path)
        )
        return match.group(1) + json.dumps(relative) + match.group(3)

    content = re.sub(r'(\bfrom\s+)["\']([^"\']+)["\'](;?)', module, content)
    if path in {"models.ts", "types.ts"}:
        # Canonical declarations are imported once; binding-specific event/error aliases stay private.
        for name in sorted(canonical):
            pattern = rf"export (?:interface|type|const) {re.escape(name)}(?:\s|=|\{{)"
            found = False
            while True:
                match = re.search(pattern, content)
                if match is None:
                    break
                found = True
                start = match.start()
                depth = 0
                quote = ""
                escape = False
                end = match.end() - 1
                for end in range(start, len(content)):
                    char = content[end]
                    if quote:
                        if escape:
                            escape = False
                        elif char == "\\":
                            escape = True
                        elif char == quote:
                            quote = ""
                    elif char in "\"'`":
                        quote = char
                    elif char in "{[(":
                        depth += 1
                    elif char in "}])":
                        depth -= 1
                    elif char == ";" and depth == 0:
                        break
                    if (
                        char == "}"
                        and depth == 0
                        and content[start : start + 16] == "export interface"
                    ):
                        break
                content = content[:start] + content[end + 1 :]
            relative = posixpath.relpath(
                "models.js", posixpath.dirname(prefix + "/" + path)
            )
            if found:
                content = (
                    f'import {{ {name} }} from "{relative}";\nexport {{ {name} }};\n'
                    + content
                )
    return content


def _typescript_client(plan: Plan, target: TargetConfig) -> dict[str, str]:
    imports = [
        'import type * as Models from "./models.js";',
        'import { Runtime, type ClientOptions, type RequestOptions } from "./internal/runtime.js";',
    ]
    factories = []
    options = []
    for source in plan.sources:
        if source.kind == "openapi":
            if not any(
                operation.binding.protocol == "sse" for operation in source.operations
            ):
                continue
            imports.append(
                f'import {{ createClient as {source.name}Factory }} from "./internal/{source.name}_sse/client.js";'
            )
            factories.append(
                f"{json.dumps(source.name + ':sse')}: {{ create: () => {source.name}Factory(this.runtime.httpOptions()), owned: true }},"
            )
        elif source.kind == "openrpc":
            imports.append(
                f'import {{ BindingClient as {source.name}Client }} from "./internal/{source.name}_jsonrpc/client.js";'
            )
            options.append(
                f"{source.name}?: {{ client?: {source.name}Client; owned?: boolean; connect?: Parameters<typeof {source.name}Client.connect>[0] }};"
            )
            servers = source.document.get("servers", [])
            defaults = (
                f"url: this.runtime.websocketUrl({json.dumps(urlsplit(servers[0]['url']).path)}), "
                if len(servers) == 1 and "{" not in servers[0]["url"]
                else ""
            )
            factories.append(
                f"{json.dumps(source.name)}: {{ create: () => {source.name}Client.connect({{lazy: true, {defaults}...options.bindings?.{source.name}?.connect}}), client: options.bindings?.{source.name}?.client, owned: options.bindings?.{source.name}?.owned ?? !options.bindings?.{source.name}?.client }},"
            )
        else:
            imports.append(
                f'import {{ BindingClient as {source.name}Client }} from "./internal/{source.name}_webrtc/client.js";'
            )
            options.append(
                f"{source.name}?: {{ config?: ConstructorParameters<typeof {source.name}Client>[0] }};"
            )
            factories.append(
                f"{json.dumps(source.name)}: {{ create: () => new {source.name}Client({{baseUrl: options.baseUrl, ...options.bindings?.{source.name}?.config}}), owned: false }},"
            )
    methods: dict[tuple[str, ...], str] = {}
    for operation in plan.operations:
        name = tuple(camel(part) for part in operation.name)
        fields = "; ".join(
            f"{camel(param.name)}{'' if param.required else '?'}: Models.{_alias(operation, param.name)}"
            for param in operation.parameters
        )
        params = f"params: {{ {fields} }}" + (
            " = {}" if not any(param.required for param in operation.parameters) else ""
        )
        result = (
            "void"
            if operation.result == {"type": "null"}
            else "Models." + _alias(operation, "Result")
        )
        binding = operation.binding
        if binding.protocol == "http":
            spec = {
                "path": binding.path,
                "method": binding.method,
                "statuses": list(binding.metadata["responses"]),
                "parameters": [
                    {
                        "name": camel(param.name),
                        "wire": param.wire_name,
                        "location": param.location,
                    }
                    for param in operation.parameters
                ],
            }
            body = (
                f"this.runtime.request<{result}>({json.dumps(spec)}, params, options)"
            )
            method = (
                f"({params}, options?: RequestOptions): Promise<{result}> => {body}"
            )
        else:
            binding_source = binding.source + (
                ":sse" if binding.protocol == "sse" else ""
            )
            if binding.protocol == "sse":
                path = list(binding.metadata["backend_typescript"])
                args = [
                    "{ "
                    + ", ".join(
                        f"{json.dumps(binding.metadata['typescript_arguments'][param.name])}: params.{camel(param.name)}"
                        for param in operation.parameters
                    )
                    + " }",
                    "options",
                ]
                if not operation.parameters:
                    args = ["options"]
            elif binding.protocol == "webrtc":
                path = list(binding.backend_path)
                backend = (
                    binding_source
                    + "Client"
                    + "".join(f"[{json.dumps(part)}]" for part in path)
                )
                methods[name] = (
                    f"(options: Parameters<{backend}>[0]): ReturnType<{backend}> => this.runtime.session(async () => (await this.runtime.binding<{binding_source}Client>({json.dumps(binding_source)})){_access(path)}(options))"
                )
                continue
            else:
                path = list(binding.metadata["backend_typescript"])
                backend = (
                    binding_source
                    + "Client"
                    + "".join(f"[{json.dumps(part)}]" for part in path)
                )
                args = [
                    "{ "
                    + ", ".join(
                        f"{json.dumps(param.wire_name)}: params.{camel(param.name)}"
                        for param in operation.parameters
                    )
                    + " }"
                ]
                if not operation.parameters:
                    args = []
                if operation.interaction == "call":
                    args.append("options")
            binding_type = (
                f"ReturnType<typeof {binding.source}Factory>"
                if binding.protocol == "sse"
                else binding_source + "Client"
            )
            invoke = f"(await this.runtime.binding<{binding_type}>({json.dumps(binding_source)})){_access(path)}({', '.join(args)})"
            if operation.interaction == "stream":
                method = f"({params}, options?: RequestOptions): AsyncIterable<{result}> => this.runtime.stream(async () => {invoke}, options?.signal)"
            elif operation.interaction == "session":
                method = f"(...args: Parameters<{backend}>): ReturnType<{backend}> => this.runtime.session(async () => (await this.runtime.binding<{binding_source}Client>({json.dumps(binding_source)})){_access(path)}(...args))"
            else:
                call = invoke.replace(", options)", ", requestOptions)").replace(
                    "(options)", "(requestOptions)"
                )
                method = f"({params}, options?: RequestOptions): Promise<{result}> => this.runtime.call(async requestOptions => {call}, options)"
        methods[name] = method
    body = _tree_ts(methods)
    client = (
        "\n".join(imports)
        + f"\n\nexport interface Options extends ClientOptions {{ bindings?: {{ {' '.join(options)} }} }}\nexport class {target.client_name} {{\n  private readonly runtime: Runtime;\n  constructor(options: Options) {{ this.runtime = new Runtime(options); this.runtime.register({{{''.join(factories)}}}); }}\n  close(): Promise<void> {{ return this.runtime.close(); }}\n{body}\n}}\nexport function createClient(options: Options): {target.client_name} {{ return new {target.client_name}(options); }}\n"
    )
    return {
        "client.ts": client,
        "index.ts": 'export * from "./client.js";\nexport * from "./errors.js";\nexport * from "./models.js";\nexport type { RequestOptions } from "./internal/runtime.js";\n',
    }


def _access(path: list[str]) -> str:
    return "".join(f"[{json.dumps(part)}]" for part in path)


def _tree_ts(methods: dict[tuple[str, ...], str]) -> str:
    tree: dict[str, Any] = {}
    for path, method in methods.items():
        node = tree
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = method

    def render_node(node: dict[str, Any], root: bool = False) -> str:
        return "\n".join(
            f"{'  readonly ' if root else ''}{key} {'=' if root else ':'} "
            + ("{\n" + render_node(value) + "\n}" if isinstance(value, dict) else value)
            + (";" if root else ",")
            for key, value in node.items()
        )

    return render_node(tree, True)


def _python_client(plan: Plan, target: TargetConfig) -> dict[str, str]:
    imports = [
        "from __future__ import annotations",
        "from collections.abc import AsyncIterator",
        "from contextlib import AbstractAsyncContextManager",
        "from typing import Any",
        "from . import models as Models",
        "from .internal.runtime import Runtime, RequestOptions, UNSET, UnsetType",
    ]
    registrations = []
    for source in plan.sources:
        if source.kind == "openapi":
            if any(
                operation.binding.protocol == "sse" for operation in source.operations
            ):
                imports.append(
                    f"from .internal.{source.name}_sse import runtime as {source.name}Sse"
                )
                registrations.append(
                    f"self._runtime.register('{source.name}:sse', lambda: {source.name}Sse.Transport(base_url, http_client=self._runtime.http), owned=True)"
                )
        else:
            imports.append(
                f"from .internal.{source.name}_jsonrpc.client import BindingClient as {source.name}Client"
            )
            if any(
                operation.binding.metadata.get("backend_type") == "socket"
                for operation in source.operations
            ):
                imports.append(
                    f"from .internal.{source.name}_jsonrpc import sockets as {source.name}Sockets"
                )
            if any(
                operation.binding.metadata.get("backend_type") == "binary"
                for operation in source.operations
            ):
                imports.append(
                    f"from .internal.{source.name}_jsonrpc import streams as {source.name}Streams"
                )

            servers = source.document.get("servers", [])
            defaults = (
                {"url": urlsplit(servers[0]["url"]).path}
                if len(servers) == 1 and "{" not in servers[0]["url"]
                else {}
            )
            default_options = (
                "{'url': self._runtime.websocket_url(" + repr(defaults["url"]) + ")}"
                if defaults
                else "{}"
            )
            registrations.append(
                f"self._runtime.register('{source.name}', lambda: {source.name}Client.connect(**{{'lazy': True, **{default_options}, **(bindings or {{}}).get('{source.name}', {{}})}}), client=(clients or {{}}).get('{source.name}'), owned='{source.name}' not in (clients or {{}}))"
            )
    groups = {
        operation.name[:index]
        for operation in plan.operations
        for index in range(1, len(operation.name))
    }
    classes = []
    for group in sorted(groups, key=lambda path: (-len(path), path)):
        lines = [
            f"class {''.join(pascal(part) for part in group)}Api:",
            "    def __init__(self, runtime: Runtime) -> None:",
            "        self._runtime = runtime",
        ]
        for child in sorted(groups):
            if len(child) == len(group) + 1 and child[:-1] == group:
                lines.append(
                    f"        self.{snake(child[-1])} = {''.join(pascal(part) for part in child)}Api(runtime)"
                )
        for operation in plan.operations:
            if operation.name[:-1] == group:
                lines.extend(_python_method(operation))
        classes.append("\n".join(lines))
    lines = [
        f"class {target.client_name}:",
        "    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None, http_client: Any = None, bindings: dict[str, dict[str, Any]] | None = None, clients: dict[str, Any] | None = None) -> None:",
        "        self._runtime = Runtime(base_url, headers=headers, http_client=http_client)",
    ]
    lines.extend("        " + registration for registration in registrations)
    for group in sorted(groups):
        if len(group) == 1:
            lines.append(
                f"        self.{snake(group[0])} = {pascal(group[0])}Api(self._runtime)"
            )
    for operation in plan.operations:
        if len(operation.name) == 1:
            lines.extend(_python_method(operation))
    lines.extend(
        [
            "    async def aclose(self) -> None:",
            "        await self._runtime.aclose()",
            f"    async def __aenter__(self) -> {target.client_name}:",
            "        return self",
            "    async def __aexit__(self, *args: Any) -> None:",
            "        await self.aclose()",
        ]
    )
    classes.append("\n".join(lines))
    return {
        "client.py": "\n".join(imports) + "\n\n" + "\n\n".join(classes) + "\n",
        "__init__.py": f"from .client import {target.client_name} as {target.client_name}\nfrom .errors import Problem as Problem, ProblemError as ProblemError, TransportError as TransportError, ProtocolError as ProtocolError\nfrom .internal.runtime import RequestOptions as RequestOptions\n",
    }


def _python_method(operation: Operation) -> list[str]:
    binding = operation.binding
    args = ", ".join(
        f"{snake(param.name)}: Models.{_alias(operation, param.name)}"
        + (" | UnsetType = UNSET" if not param.required else "")
        for param in operation.parameters
    )
    args = (
        ", *, "
        + (args + ", " if args else "")
        + "options: RequestOptions | None = None"
    )
    result = "Models." + _alias(operation, "Result")
    params = (
        "{"
        + ", ".join(
            f"{param.wire_name!r}: {snake(param.name)}"
            for param in operation.parameters
        )
        + "}"
    )
    if binding.protocol == "http":
        spec = {
            "path": binding.path,
            "method": binding.method,
            "statuses": list(binding.metadata["responses"]),
            "parameters": [
                {"wire": param.wire_name, "location": param.location}
                for param in operation.parameters
            ],
        }
        return [
            f"    async def {snake(operation.name[-1])}(self{args}) -> {result}:",
            f"        return await self._runtime.request({spec!r}, {params}, {result}, options, {{"
            + ", ".join(
                f"{param.wire_name!r}: Models.{_alias(operation, param.name)}"
                for param in operation.parameters
            )
            + "})",
        ]
    source = binding.source + (":sse" if binding.protocol == "sse" else "")
    path = binding.metadata["backend_python"]
    call = "client." + ".".join(path)
    kwargs = (
        "**{name: value for name, value in {"
        + ", ".join(
            f"{binding.metadata['python_arguments'][param.name]!r}: {snake(param.name)}"
            for param in operation.parameters
        )
        + "}.items() if value is not UNSET}"
    )
    if binding.protocol == "sse":
        kwargs += (", " if kwargs else "") + "options=options"
    if operation.interaction == "call":
        return [
            f"    async def {snake(operation.name[-1])}(self{args}) -> {result}:",
            f"        return await self._runtime.call({source!r}, {tuple(path)!r}, {{"
            + ", ".join(
                f"{binding.metadata['python_arguments'][param.name]!r}: {snake(param.name)}"
                for param in operation.parameters
            )
            + "}, options)",
        ]
    if operation.interaction == "stream":
        if binding.protocol == "sse":
            spec = {
                "path": binding.path,
                "method": binding.method,
                "canon_stream": binding.metadata["profile"],
                "parameters": [
                    {"wire": param.wire_name, "location": param.location}
                    for param in operation.parameters
                ],
            }
            parameter_types = (
                "{"
                + ", ".join(
                    f"{param.wire_name!r}: Models.{_alias(operation, param.name)}"
                    for param in operation.parameters
                )
                + "}"
            )
            setup = f"        stream = await self._runtime.open_stream({source!r}, {spec!r}, {params}, {result}, {parameter_types}, options, {binding.source}Sse.RequestOptions)"
        else:
            setup = f"        client = await self._runtime.binding({source!r})\n        stream = {call}({kwargs})"
        return [
            f"    async def {snake(operation.name[-1])}(self{args}) -> AsyncIterator[{result}]:",
            setup,
            "        async with self._runtime.iterating(stream) as stream:",
            "            async for item in stream:",
            f"                yield self._runtime.result(item, {result})",
        ]
    if binding.metadata["backend_type"] == "binary":
        session_type = binding.source + "Streams." + binding.metadata["python_session"]
    elif binding.metadata.get("messages"):
        session_type = (
            binding.source
            + "Sockets.TypedSocket[Models."
            + _alias(operation, "client")
            + ", Models."
            + _alias(operation, "server")
            + "]"
        )
    else:
        session_type = binding.source + "Sockets.Socket"
    args = (
        ", *, "
        + ", ".join(
            f"{snake(param.name)}: Models.{_alias(operation, param.name)} | UnsetType = UNSET"
            for param in operation.parameters
        )
        if operation.parameters
        else ""
    )
    return [
        f"    def {snake(operation.name[-1])}(self{args}) -> AbstractAsyncContextManager[{session_type}]:",
        f"        return self._runtime.session({source!r}, {tuple(path)!r}, {{"
        + ", ".join(
            f"{binding.metadata['python_arguments'][param.name]!r}: {snake(param.name)}"
            for param in operation.parameters
        )
        + "})",
    ]


def _binding_ir(ir: Any) -> Any:
    from fastapi_canon.integrations.websockets.codegen.ir import (
        AliasDecl,
        EnumDecl,
        EnumLiteralType,
        LiteralType,
        Primitive,
        PrimitiveType,
        UnionType,
    )

    values = {
        (enum.name, member.name): member.value
        for enum in ir.declarations
        if isinstance(enum, EnumDecl)
        for member in enum.members
    }

    def project(value: Any) -> Any:
        if isinstance(value, EnumLiteralType):
            return LiteralType(values[(value.enum, value.member)])
        if isinstance(value, PrimitiveType) and value.primitive in {
            Primitive.UUID,
            Primitive.DATETIME,
        }:
            return PrimitiveType(Primitive.STRING)
        if isinstance(value, EnumDecl) and any(
            not isinstance(member.value, str) for member in value.members
        ):
            return AliasDecl(
                value.name,
                UnionType(tuple(LiteralType(member.value) for member in value.members)),
            )
        if isinstance(value, tuple):
            return tuple(project(item) for item in value)
        if isinstance(value, dict):
            return {key: project(item) for key, item in value.items()}
        if is_dataclass(value) and not isinstance(value, type):
            return replace(
                value,
                **{
                    field.name: project(getattr(value, field.name))
                    for field in fields(value)
                    if field.init
                },
            )
        return value

    return project(ir)
