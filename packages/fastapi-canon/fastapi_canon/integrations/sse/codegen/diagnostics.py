from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast
from urllib.parse import unquote

from fastapi_canon.integrations.sse.codegen.ir import Contract, Operation
from fastapi_canon.integrations.sse.codegen.openapi import (
    ContractError,
    Document,
    resolve_pointer,
)

type GenerationTarget = Literal["python", "typescript"]

_UNSUPPORTED = {
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "contains",
    "minContains",
    "maxContains",
    "minProperties",
    "maxProperties",
    "patternProperties",
    "propertyNames",
    "dependentRequired",
    "dependentSchemas",
    "unevaluatedProperties",
    "unevaluatedItems",
    "not",
    "if",
    "then",
    "else",
}
_SCALAR = {
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
}


class OptionalDiscriminatorWarning(UserWarning):
    """An event union may be missing the field used to select its variant."""


class UnsupportedConstraintWarning(UserWarning):
    """A source validation keyword cannot be enforced by the selected target."""


@dataclass(frozen=True)
class GenerationDiagnostic:
    """A source-located limitation with an identity independent of message wording."""

    code: str
    pointer: str
    keyword: str
    value: Any
    target: GenerationTarget
    operation_id: str
    method: str
    path: str
    message: str

    @property
    def id(self) -> str:
        identity = asdict(self)
        identity.pop("message")
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()
        return self.code + ":" + hashlib.sha256(encoded).hexdigest()[:20]

    def to_dict(self, *, accepted: bool = False) -> Document:
        return {"id": self.id, **asdict(self), "accepted": accepted}

    def __str__(self) -> str:
        return (
            f"{self.code} [{self.id}] {self.operation_id} "
            f"({self.method} {self.path}) at {self.pointer}: "
            f"{self.keyword}: {json.dumps(self.value, sort_keys=True)}; "
            f"target={self.target}. {self.message}"
        )


def collect_diagnostics(
    document: Document, contract: Contract, target: GenerationTarget
) -> list[GenerationDiagnostic]:
    """Inspect schemas reachable from generated operations, following local refs."""
    result: dict[str, GenerationDiagnostic] = {}
    locations: dict[int, str] = {}
    _index_locations(document, "", locations)
    for operation in contract.operations:
        visitor = _SourceDiagnostics(document, target, operation, result, locations)
        base = "/paths/" + _escape(operation.path)
        path_item, base = visitor.resolve(document["paths"][operation.path], base)
        method = operation.method.lower()
        raw = path_item[method]
        operation_pointer = locations.get(id(raw), base + "/" + method)
        merged: dict[tuple[str, str], tuple[Document, str]] = {}
        for container, pointer in ((path_item, base), (raw, operation_pointer)):
            for index, value in enumerate(container.get("parameters", [])):
                param, location = visitor.resolve(
                    value, f"{pointer}/parameters/{index}"
                )
                merged[(param["in"], param["name"])] = (param, location)
        for param, pointer in merged.values():
            visitor.schema(param.get("schema", {}), pointer + "/schema")
        if "requestBody" in raw:
            visitor.openapi(raw["requestBody"], operation_pointer + "/requestBody")
        for status, response in raw.get("responses", {}).items():
            pointer = operation_pointer + "/responses/" + _escape(status)
            resolved, pointer = visitor.resolve(response, pointer)
            for media, value in resolved.get("content", {}).items():
                if status.startswith("2") and media != "text/event-stream":
                    continue
                visitor.openapi(
                    value,
                    pointer + "/content/" + _escape(media),
                    event=media == "text/event-stream" and status.startswith("2"),
                )
    return sorted(result.values(), key=lambda item: (item.pointer, item.id))


def emit_diagnostics(diagnostics: list[GenerationDiagnostic]) -> None:
    for diagnostic in diagnostics:
        category = (
            OptionalDiscriminatorWarning
            if diagnostic.code == "optional-discriminator"
            else UnsupportedConstraintWarning
        )
        warnings.warn(str(diagnostic), category, stacklevel=2)


def accepted_diagnostics(
    diagnostics: list[GenerationDiagnostic], allowances: Any
) -> set[str]:
    """Accept exact reviewed IDs; reject malformed, duplicate and stale allowances."""
    if not isinstance(allowances, dict) or set(cast(Document, allowances)) != {"allow"}:
        raise ContractError('Allowances must be an object containing only "allow"')
    entries = cast(Document, allowances)["allow"]
    if not isinstance(entries, list):
        raise ContractError('Allowances "allow" must be a list')
    accepted: set[str] = set()
    current = {item.id for item in diagnostics}
    for raw_entry in cast(list[Any], entries):
        if not isinstance(raw_entry, dict) or set(cast(Document, raw_entry)) != {
            "id",
            "reason",
        }:
            raise ContractError("Each allowance needs exactly id and reason")
        entry = cast(Document, raw_entry)
        identity, reason = entry["id"], entry["reason"]
        if (
            not isinstance(identity, str)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ContractError("Allowance id and non-empty review reason are required")
        if identity in accepted:
            raise ContractError(f"Duplicate diagnostic allowance: {identity}")
        if identity not in current:
            raise ContractError(
                f"Stale diagnostic allowance: {identity}; review the report"
            )
        accepted.add(identity)
    return accepted


class _SourceDiagnostics:
    def __init__(
        self,
        document: Document,
        target: GenerationTarget,
        operation: Operation,
        result: dict[str, GenerationDiagnostic],
        locations: dict[int, str],
    ) -> None:
        self.document = document
        self.target: GenerationTarget = target
        self.operation = operation
        self.result = result
        self.locations = locations
        self.seen: set[tuple[str, bool]] = set()

    def resolve(self, node: Document, pointer: str) -> tuple[Document, str]:
        pointer = self.locations.get(id(node), pointer)
        seen: set[str] = set()
        while "$ref" in node:
            ref = cast(str, node["$ref"])
            if ref in seen:
                raise ContractError(f"Cyclic reference: {ref}")
            seen.add(ref)
            target = resolve_pointer(self.document, ref)
            # Boolean Schema references have no diagnosable keywords. OpenAPI
            # reference siblings retain their original child source locations.
            if not isinstance(target, dict):
                return {}, unquote(ref[1:])
            node = {
                **cast(Document, target),
                **{key: value for key, value in node.items() if key != "$ref"},
            }
            pointer = unquote(ref[1:])
        return node, pointer

    def add(
        self, code: str, pointer: str, keyword: str, value: Any, message: str
    ) -> None:
        op = self.operation
        diagnostic = GenerationDiagnostic(
            code,
            pointer,
            keyword,
            value,
            self.target,
            op.operation_id,
            op.method,
            op.path,
            message,
        )
        self.result[diagnostic.id] = diagnostic

    def openapi(self, node: Any, pointer: str, *, event: bool = False) -> None:
        if not isinstance(node, dict):
            return
        node, pointer = self.resolve(cast(Document, node), pointer)
        for key, value in node.items():
            location = pointer + "/" + _escape(key)
            if key == "itemSchema":
                envelope, envelope_pointer = self.resolve(value, location)
                data, data_pointer = self.resolve(
                    envelope.get("properties", {}).get("data", {}),
                    envelope_pointer + "/properties/data",
                )
                self.schema(
                    data.get("contentSchema", {}),
                    data_pointer + "/contentSchema",
                    event=event,
                )
            elif key == "schema":
                self.schema(value, location, event=event)
            elif key not in {"example", "examples", "default"}:
                self.openapi(value, location, event=event)

    def schema(self, node: Any, pointer: str, *, event: bool = False) -> None:
        if not isinstance(node, dict):
            return
        node, pointer = self.resolve(cast(Document, node), pointer)
        if (pointer, event) in self.seen:
            return
        self.seen.add((pointer, event))
        unsupported: set[str] = _UNSUPPORTED | (
            _SCALAR if self.target == "typescript" else set()
        )
        for keyword in sorted(unsupported & node.keys()):
            self.add(
                "unsupported-constraint",
                pointer + "/" + keyword,
                keyword,
                node[keyword],
                "This constraint is not enforced at runtime by the generated "
                + self.target
                + " SDK.",
            )
        if event and self.target == "python":
            self.optional_discriminator(node, pointer)
        for keyword in ("properties", "patternProperties", "dependentSchemas"):
            for name, child in node.get(keyword, {}).items():
                self.schema(
                    child, pointer + "/" + keyword + "/" + _escape(name), event=event
                )
        for keyword in ("oneOf", "anyOf", "allOf", "prefixItems"):
            for index, child in enumerate(node.get(keyword, [])):
                self.schema(child, f"{pointer}/{keyword}/{index}", event=event)
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
            if keyword in node:
                self.schema(node[keyword], pointer + "/" + keyword, event=event)

    def optional_discriminator(self, node: Document, pointer: str) -> None:
        field = node.get("discriminator", {}).get("propertyName")
        if not isinstance(field, str):
            return
        for keyword in ("oneOf", "anyOf"):
            for index, member in enumerate(node.get(keyword, [])):
                if not isinstance(member, dict):
                    continue
                model, location = self.resolve(
                    cast(Document, member), f"{pointer}/{keyword}/{index}"
                )
                if field in model.get("properties", {}) and field not in model.get(
                    "required", []
                ):
                    name = model.get("title", location.rsplit("/", 1)[-1])
                    self.add(
                        "optional-discriminator",
                        location + "/properties/" + _escape(field),
                        "required",
                        field,
                        f"Event schema {name!r} has optional discriminator {field!r}. "
                        "Missing tags can prevent variant selection. The generated "
                        "field remains optional; mark it required in the output schema "
                        "if the serializer always emits it. "
                        "See docs/codegen.md#event-discriminators.",
                    )


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _index_locations(node: Any, pointer: str, locations: dict[int, str]) -> None:
    if isinstance(node, dict):
        locations[id(cast(Document, node))] = pointer
        for key, value in sorted(cast(Document, node).items()):
            _index_locations(value, pointer + "/" + _escape(key), locations)
    elif isinstance(node, list):
        for index, value in enumerate(cast(list[Any], node)):
            _index_locations(value, f"{pointer}/{index}", locations)
