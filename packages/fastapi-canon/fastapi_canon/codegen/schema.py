from __future__ import annotations

import copy
import json
from typing import Any, cast

from fastapi_canon.codegen.config import SourceConfig
from fastapi_canon.codegen.ir import GenerationError, Model
from fastapi_canon.codegen.names import pascal

_RESERVED = {
    "Problem",
    "ProblemError",
    "ProtocolError",
    "TransportError",
    "ApiClient",
    "ClientOptions",
    "RequestOptions",
    "JsonValue",
}


class SchemaRegistry:
    """One identity and one definition for each model in an SDK target."""

    def __init__(self) -> None:
        self._models: dict[str, Model] = {}
        self._names: dict[str, str] = {}
        self._refs: dict[tuple[str, str], str] = {}
        self._documents: dict[str, dict[str, Any]] = {}
        self._configs: dict[str, SourceConfig] = {}
        self._compiling: set[str] = set()
        self._compiled: set[tuple[str, str]] = set()

    @property
    def models(self) -> tuple[Model, ...]:
        return tuple(sorted(self._models.values(), key=lambda item: item.name))

    @property
    def schemas(self) -> dict[str, Any]:
        return {model.name: model.schema for model in self.models}

    def add_sources(
        self, sources: tuple[tuple[SourceConfig, dict[str, Any]], ...]
    ) -> None:
        self._documents = {config.name: document for config, document in sources}
        self._configs = {config.name: config for config, _ in sources}
        # Claim every name first so recursive references and source ordering are stable.
        for config, document in sorted(sources, key=lambda item: item[0].name):
            root = (
                "#/definitions/" if config.kind == "webrtc" else "#/components/schemas/"
            )
            schemas = (
                document.get("definitions", {})
                if config.kind == "webrtc"
                else document.get("components", {}).get("schemas", {})
            )
            for name in sorted(schemas):
                self._claim(config.name, root + _escape(name))
            for ref in _inline_problems(document):
                self._claim(config.name, ref)
        for config, _ in sorted(sources, key=lambda item: item[0].name):
            for source, ref in tuple(self._refs):
                if source == config.name:
                    self._compile(source, ref)

    def document(self, source: str) -> dict[str, Any]:
        raw = copy.deepcopy(self._documents[source])
        if self._configs[source].kind == "webrtc":
            raw.pop("definitions", None)
        else:
            raw.get("components", {}).pop("schemas", None)
        document = self._rewrite(source, raw)
        names = {name for (owner, _), name in self._refs.items() if owner == source}
        # Include only this source's definitions, then close over their references.
        queue = list(names)
        while queue:
            name = queue.pop()
            identity = self._names[name]
            schema = self._models[identity].schema
            for reference in _references(schema):
                child = reference.removeprefix("#/components/schemas/")
                if child not in names:
                    names.add(child)
                    queue.append(child)
        schemas = {
            name: copy.deepcopy(self._models[self._names[name]].schema)
            for name in sorted(names)
        }
        if self._configs[source].kind == "webrtc":
            document["definitions"] = _rtc_refs(schemas)
            document = _rtc_refs(document)
        else:
            document.setdefault("components", {})["schemas"] = schemas
        return cast(dict[str, Any], document)

    def _rewrite(
        self,
        source: str,
        value: Any,
        *,
        scope: str | None = None,
        address: str = "#",
        container: bool = False,
        declaration: bool = False,
    ) -> Any:
        if isinstance(value, list):
            return [
                self._rewrite(
                    source, item, scope=scope, address=address + "/" + str(index)
                )
                for index, item in enumerate(value)
            ]
        if not isinstance(value, dict):
            return value
        if not container and "$defs" in value and scope is None:
            scope = address
        if not container and _problem_type(value) is not None and not declaration:
            return {"$ref": "#/components/schemas/" + self._claim(source, address)}
        result = {}
        for key, item in value.items():
            if not container and key == "$ref":
                if not isinstance(item, str) or not item.startswith("#/"):
                    raise GenerationError(
                        f"{source}: external model references require a bundled contract: {item!r}"
                    )
                ref = item
                local = item.startswith("#/$defs/")
                if local:
                    if scope is None:
                        raise GenerationError(
                            f"{source}: unscoped model reference {item}"
                        )
                    ref = scope + "/$defs/" + item.removeprefix("#/$defs/")
                if local or ref.startswith(("#/components/schemas/", "#/definitions/")):
                    name = self._claim(source, ref)
                    self._compile(source, ref)
                    result[key] = f"#/components/schemas/{name}"
                else:
                    result[key] = item
            elif not container and key in {"default", "examples", "example"}:
                result[key] = copy.deepcopy(item)
            elif container or key not in {"$defs", "$id"}:
                result[key] = self._rewrite(
                    source,
                    item,
                    scope=scope,
                    address=address + "/" + _escape(key),
                    container=not container
                    and key in {"properties", "patternProperties"},
                )
        return result

    def _claim(self, source: str, ref: str) -> str:
        key = source, ref
        if key in self._refs:
            return self._refs[key]
        raw = _pointer(self._documents[source], ref)
        if not isinstance(raw, (dict, bool)):
            raise GenerationError(f"{source}{ref} is not a schema")
        config = self._configs[source]
        last = ref.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        problem = _problem_type(raw)
        identity = config.identities.get(ref, config.identities.get(last))
        if identity is None and isinstance(raw, dict):
            identity = raw.get("x-canon-model-id") or raw.get("$id")
        identity = (
            f"problem:{problem}"
            if problem is not None
            else identity or f"{source}{ref}"
        )
        if not isinstance(identity, str):
            raise GenerationError(f"{source}{ref}: model identity must be a string")
        requested = config.names.get(ref, config.names.get(last))
        if requested is None and isinstance(raw, dict):
            requested = raw.get("x-canon-model-name")
        if (
            last == "Problem"
            and isinstance(raw, dict)
            and {"type", "title", "code", "status"} <= raw.get("properties", {}).keys()
            and not problem
        ):
            identity = "canon:generic-problem"
            requested = "ProblemDetails"
        if identity in self._models:
            name = self._models[identity].name
            if requested and pascal(requested) != name:
                raise GenerationError(
                    f"Identity {identity!r} has conflicting public names at {source}{ref}"
                )
        else:
            title = raw.get("title") if isinstance(raw, dict) else None
            if problem and title in (None, "Problem"):
                title = pascal(raw["properties"]["code"]["const"]) + "Problem"
            name = pascal(
                requested
                or (
                    title
                    if identity != f"{source}{ref}" and title not in (None, "Problem")
                    else None
                )
                or last
            )
            if problem and name.endswith("Embedded"):
                name = name.removesuffix("Embedded")
            if problem and name.endswith("Error"):
                name += "Problem"
            if name in _RESERVED:
                raise GenerationError(
                    f"Reserved generated model name {name!r} at {source}{ref}"
                )
            previous = self._names.get(name)
            if previous is not None and previous != identity:
                origin = self._models[previous].origin
                raise GenerationError(
                    f"Model name {name!r} collides between {origin} and {source}{ref}; declare matching identities or an explicit source names mapping"
                )
            self._names[name] = identity
            self._models[identity] = Model(name, identity, {}, source + ref, problem)
        self._refs[key] = name
        if isinstance(raw, dict):
            for child in raw.get("$defs", {}):
                self._claim(source, ref + "/$defs/" + _escape(child))
        return name

    def _compile(self, source: str, ref: str) -> None:
        name = self._claim(source, ref)
        identity = self._names[name]
        marker = source + ref
        if marker in self._compiling or (source, ref) in self._compiled:
            return
        self._compiling.add(marker)
        try:
            raw = copy.deepcopy(_pointer(self._documents[source], ref))
            schema = self._rewrite(
                source,
                raw,
                scope=ref.split("/$defs/", 1)[0],
                address=ref,
                declaration=True,
            )
            model = self._models[identity]
            if model.problem_type:
                schema = _problem_schema(schema)
            if not isinstance(schema, dict):
                schema = {} if schema else {"not": {}}
            schema["title"] = name
            if model.schema and _semantic(model.schema) != _semantic(schema):
                raise GenerationError(
                    f"Incompatible wire schemas for {identity!r}: {model.origin} and {marker}"
                )
            self._models[identity] = Model(
                name, identity, schema, model.origin, model.problem_type
            )
            self._compiled.add((source, ref))
        finally:
            self._compiling.remove(marker)


def _problem_type(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    description = schema.get("description", "")
    if not schema.get("x-canon-problem") and not (
        isinstance(description, str) and description.startswith("RFC 9457")
    ):
        return None
    properties = schema.get("properties", {})
    value = properties.get("type", {}).get("const")
    return (
        value
        if isinstance(value, str)
        and value != "about:blank"
        and "code" in properties
        and "title" in properties
        else None
    )


def _problem_schema(schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(schema)
    schema.setdefault("properties", {})["status"] = {
        "type": "integer",
        "minimum": 100,
        "maximum": 599,
    }
    schema["required"] = [
        name for name in schema.get("required", []) if name != "status"
    ]
    schema.pop("x-canon-problem", None)
    schema.pop("examples", None)
    if "const" in schema.get("properties", {}).get("code", {}):
        schema["properties"]["code"].pop("pattern", None)
    return schema


def _semantic(value: Any) -> str:
    def clean(item: Any, *, container: bool = False) -> Any:
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not container and (
                    key in {"title", "description", "examples", "$comment"}
                    or key.startswith("x-")
                ):
                    continue
                if not container and key in {"default", "const", "enum"}:
                    result[key] = child
                elif not container and key == "required" and isinstance(child, list):
                    result[key] = sorted(child)
                else:
                    result[key] = clean(
                        child,
                        container=not container
                        and key
                        in {
                            "properties",
                            "patternProperties",
                            "dependentSchemas",
                            "mapping",
                        },
                    )
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return json.dumps(clean(value), sort_keys=True, separators=(",", ":"))


def _pointer(document: dict[str, Any], reference: str) -> Any:
    value: Any = document
    try:
        for name in reference[2:].split("/"):
            value = (
                value[int(name)]
                if isinstance(value, list)
                else value[name.replace("~1", "/").replace("~0", "~")]
            )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise GenerationError(f"Unresolved contract reference {reference}") from exc
    return value


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _references(value: Any) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_references(item) for item in value))
    if not isinstance(value, dict):
        return set()
    found = (
        {value["$ref"]}
        if isinstance(value.get("$ref"), str)
        and value["$ref"].startswith("#/components/schemas/")
        else set()
    )
    return found | set().union(*(_references(item) for item in value.values()))


def _rtc_refs(value: Any) -> Any:
    if isinstance(value, list):
        return [_rtc_refs(item) for item in value]
    if isinstance(value, dict):
        return {
            key: item.replace("#/components/schemas/", "#/definitions/")
            if key == "$ref" and isinstance(item, str)
            else _rtc_refs(item)
            for key, item in value.items()
        }
    return value


def _inline_problems(value: Any, address: str = "#") -> list[str]:
    if isinstance(value, list):
        return [
            ref
            for index, item in enumerate(value)
            for ref in _inline_problems(item, address + "/" + str(index))
        ]
    if not isinstance(value, dict):
        return []
    refs = [address] if _problem_type(value) is not None else []
    return refs + [
        ref
        for key, item in value.items()
        if key not in {"default", "examples", "example"}
        for ref in _inline_problems(item, address + "/" + _escape(key))
    ]
