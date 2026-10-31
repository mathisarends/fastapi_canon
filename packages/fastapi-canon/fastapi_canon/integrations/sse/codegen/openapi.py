import copy
import json
from importlib.resources import files
from typing import Any, cast
from urllib.parse import unquote

from fastapi import FastAPI

type Document = dict[str, Any]


class ContractError(ValueError):
    """An invalid or unsupported OpenAPI contract."""


def resolve_pointer(document: Document, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ContractError(f"Only local OpenAPI references are supported: {ref!r}")
    target: Any = document
    try:
        for part in unquote(ref[2:]).split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            target = (
                cast(list[Any], target)[int(key)]
                if isinstance(target, list)
                else target[key]
            )
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ContractError(f"Unresolved reference: {ref}") from exc
    return target


def resolve_ref(document: Document, value: Document) -> Document:
    """Resolve local JSON Pointer references, detecting reference-only cycles."""
    seen: set[str] = set()
    while "$ref" in value:
        ref: str = value["$ref"]
        if ref in seen:
            raise ContractError(f"Cyclic reference: {ref}")
        seen.add(ref)
        target = resolve_pointer(document, ref)
        if not isinstance(target, dict):
            raise ContractError(f"Reference does not point to an object: {ref}")
        value = {**target, **{k: v for k, v in value.items() if k != "$ref"}}
    return value


def validate_contract(document: Document) -> None:
    """Validate structure, JSON Schemas, and local reference closure offline.

    The official OpenAPI meta-schema deliberately omits Schema Object validation;
    those objects are checked separately with JSON Schema Draft 2020-12.
    """
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError as exc:
        raise ContractError(
            "Install fastapi-canon[codegen] to validate contracts"
        ) from exc

    meta = json.loads(
        files(__package__).joinpath("schemas/openapi-3.2.json").read_text()
    )
    errors = sorted(
        Draft202012Validator(meta).iter_errors(document),  # pyright: ignore[reportUnknownMemberType]
        key=lambda e: str(e.json_path),
    )
    if errors:
        error = errors[0]
        raise ContractError(
            f"Invalid OpenAPI 3.2 at {error.json_path}: {error.message}"
        )
    dialect = document.get("jsonSchemaDialect", "")
    if dialect and dialect not in {
        "https://spec.openapis.org/oas/3.2/dialect/2026-02-26",
        "https://json-schema.org/draft/2020-12/schema",
    }:
        raise ContractError(f"Unsupported JSON Schema dialect: {dialect}")

    def walk_schema(node: Any, path: str) -> None:
        try:
            Draft202012Validator.check_schema(node)
        except SchemaError as exc:
            raise ContractError(
                f"Invalid JSON Schema at {path}: {exc.message}"
            ) from exc
        if not isinstance(node, dict):
            return
        schema = cast(Document, node)
        if "$ref" in schema:
            resolve_pointer(document, schema["$ref"])
        for keyword in ("$defs", "properties", "patternProperties", "dependentSchemas"):
            for name, child in schema.get(keyword, {}).items():
                walk_schema(child, f"{path}/{keyword}/{name}")
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for index, child in enumerate(schema.get(keyword, [])):
                walk_schema(child, f"{path}/{keyword}/{index}")
        for keyword in (
            "items",
            "additionalProperties",
            "unevaluatedProperties",
            "unevaluatedItems",
            "contains",
            "not",
            "if",
            "then",
            "else",
            "propertyNames",
            "contentSchema",
        ):
            if keyword in schema:
                walk_schema(schema[keyword], f"{path}/{keyword}")

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                resolve_ref(document, cast(Document, node))
            for key, value in cast(Document, node).items():
                if key in {"example", "examples", "default", "enum", "const"}:
                    continue
                if path == "$/components/schemas" or key in {"schema", "itemSchema"}:
                    walk_schema(value, f"{path}/{key}")
                else:
                    walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(cast(list[Any], node)):
                walk(value, f"{path}/{index}")

    walk(document, "$")


def export_schema(app: FastAPI) -> Document:
    """Copy native OpenAPI output, promote compatible 3.1 output, then validate.

    FastAPI emits the 3.2 itemSchema/contentSchema structure under a 3.1 version.
    Its JSON Schema dialect is already Draft 2020-12. No stream schemas are
    synthesized and the application's cached document is never modified.
    Incompatible custom OpenAPI extensions fail validation instead of being lost.
    """
    document = copy.deepcopy(app.openapi())
    version = document.get("openapi", "")
    if not version.startswith(("3.1.", "3.2.")):
        raise ContractError(f"Expected OpenAPI 3.1 or 3.2, got {version!r}")
    if (
        document.get("jsonSchemaDialect")
        == "https://spec.openapis.org/oas/3.1/dialect/base"
    ):
        document["jsonSchemaDialect"] = (
            "https://spec.openapis.org/oas/3.2/dialect/2026-02-26"
        )
    document["openapi"] = "3.2.0"
    validate_contract(document)
    return document
