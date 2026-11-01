"""Select explicit operation IDs before planning models or invoking emitters."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from fastapi_canon.codegen.config import SourceConfig
from fastapi_canon.codegen.ir import GenerationError

_HTTP_METHODS = {"get", "put", "post", "patch", "delete", "head", "options", "trace"}
_RPC_GROUPS = (
    "methods",
    "x-rpc-notifications",
    "x-rpc-subscriptions",
    "x-rpckit-binary-streams",
    "x-rpckit-sockets",
)


def select_operations(
    config: SourceConfig, document: dict[str, Any]
) -> tuple[SourceConfig, dict[str, Any]]:
    """Keep the original input intact, including its provenance hash.

    Selection uses wire IDs before public naming. Unknown, duplicate and empty
    selections fail explicitly. Only selected operations reach binding emitters.
    """
    if config.include_operations is None and not config.exclude_operations:
        return config, document
    selected = copy.deepcopy(document)
    operations: list[tuple[str, Any, Any]] = []
    try:
        if config.kind == "openapi":
            from fastapi_canon.integrations.sse.codegen.openapi import resolve_ref

            for path, item in selected.get("paths", {}).items():
                item = copy.deepcopy(resolve_ref(document, item))
                selected["paths"][path] = item
                for method in _HTTP_METHODS & item.keys():
                    operations.append((item[method]["operationId"], item, method))
        else:
            for key in _RPC_GROUPS if config.kind == "openrpc" else ("sessions",):
                for operation in selected.get(key, []):
                    operations.append((operation["name"], selected[key], operation))
    except (KeyError, TypeError, AttributeError) as exc:
        raise GenerationError(
            f"Source {config.name}: invalid operation catalog"
        ) from exc
    names = [name for name, _, _ in operations]
    if any(not isinstance(name, str) or not name for name in names) or len(
        set(names)
    ) != len(names):
        raise GenerationError(f"Source {config.name}: operations require unique IDs")
    known = set(names)
    include = (
        set(config.include_operations)
        if config.include_operations is not None
        else known
    )
    exclude = set(config.exclude_operations)
    unknown = (include | exclude | config.operations.keys()) - known
    if unknown:
        raise GenerationError(
            f"Source {config.name}: unknown operation IDs: {sorted(unknown)}"
        )
    if config.include_operations is not None and include & exclude:
        raise GenerationError(
            f"Source {config.name}: cannot include and exclude the same operation"
        )
    keep = include - exclude
    if not keep:
        raise GenerationError(f"Source {config.name}: selection contains no operations")
    for name, owner, key in operations:
        if name not in keep:
            if isinstance(owner, dict):
                del owner[key]
            else:
                owner.remove(key)
    if config.kind == "openapi":
        selected["paths"] = {
            path: item
            for path, item in selected.get("paths", {}).items()
            if _HTTP_METHODS & item.keys()
        }
    _prune_models(selected, config.kind == "webrtc")
    return replace(
        config,
        operations={
            key: value for key, value in config.operations.items() if key in keep
        },
    ), selected


def _prune_models(document: dict[str, Any], webrtc: bool) -> None:
    owner = document if webrtc else document.get("components", {})
    key = "definitions" if webrtc else "schemas"
    models = owner.pop(key, {})
    prefix = "#/definitions/" if webrtc else "#/components/schemas/"
    pending = list(_references(document, prefix))
    keep = set()
    while pending:
        name = pending.pop()
        if name in keep or name not in models:
            continue
        keep.add(name)
        pending.extend(_references(models[name], prefix))
    owner[key] = {name: schema for name, schema in models.items() if name in keep}


def _references(value: Any, prefix: str, *, container: bool = False) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_references(item, prefix) for item in value))
    if not isinstance(value, dict):
        return set()
    found = set()
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith(prefix):
        found.add(
            reference.removeprefix(prefix)
            .split("/", 1)[0]
            .replace("~1", "/")
            .replace("~0", "~")
        )
    for key, item in value.items():
        if container or key not in {"default", "example", "examples", "const", "enum"}:
            found.update(
                _references(
                    item,
                    prefix,
                    container=not container
                    and key
                    in {
                        "components",
                        "schemas",
                        "definitions",
                        "$defs",
                        "properties",
                        "patternProperties",
                        "dependentSchemas",
                        "paths",
                        "responses",
                        "parameters",
                        "headers",
                        "requestBodies",
                        "callbacks",
                        "pathItems",
                        "links",
                        "content",
                        "encoding",
                        "mapping",
                    },
                )
            )
    return found
