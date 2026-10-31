from __future__ import annotations

from typing import Any

from fastapi_canon.codegen.config import SourceConfig
from fastapi_canon.codegen.ir import GenerationError, Plan
from fastapi_canon.codegen.names import camel, snake
from fastapi_canon.codegen.readers import read_source
from fastapi_canon.codegen.schema import SchemaRegistry


def build_plan(inputs: tuple[tuple[SourceConfig, dict[str, Any]], ...]) -> Plan:
    registry = SchemaRegistry()
    registry.add_sources(inputs)
    sources = tuple(
        read_source(config, registry.document(config.name)) for config, _ in inputs
    )
    operations = tuple(
        sorted(
            (operation for source in sources for operation in source.operations),
            key=lambda operation: operation.name,
        )
    )
    for convert in (snake, camel):
        names: dict[tuple[str, ...], str] = {}
        for operation in operations:
            name = tuple(convert(part) for part in operation.name)
            owner = f"{operation.binding.source}:{operation.binding.operation}"
            if any(
                name == other
                or name[: len(other)] == other
                or other[: len(name)] == name
                for other in names
            ):
                raise GenerationError(
                    f"Public operation conflict at {'.'.join(name)} ({owner}); set an explicit operations mapping"
                )
            names[name] = owner
    return Plan(sources, operations, registry.models)
