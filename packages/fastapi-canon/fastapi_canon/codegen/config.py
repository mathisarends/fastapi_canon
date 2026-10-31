from __future__ import annotations

import keyword
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from fastapi_canon.codegen.ir import ContractKind, GenerationError
from fastapi_canon.codegen.names import public_name


@dataclass(frozen=True)
class SourceConfig:
    name: str
    kind: ContractKind
    input: Path | None = None
    source: str | None = None
    output: Path | None = None
    factory: bool = False
    operations: dict[str, str] = field(default_factory=dict)
    identities: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetConfig:
    name: str
    language: Literal["python", "typescript"]
    sources: tuple[str, ...]
    output: Path
    package: str = "api_client"
    client_name: str = "ApiClient"
    http_client: Literal["httpx", "httpx2"] = "httpx"


@dataclass(frozen=True)
class Configuration:
    path: Path
    sources: tuple[SourceConfig, ...]
    targets: tuple[TargetConfig, ...]


def load_config(path: Path) -> Configuration:
    path = path.resolve()
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GenerationError(f"Cannot read {path}: {exc}") from exc
    raw = (
        parsed.get("tool", {}).get("canon", {})
        if path.name == "pyproject.toml"
        else parsed
    )
    _keys(raw, {"version", "sources", "targets"}, str(path))
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise GenerationError("Canon generation configuration requires version = 1")
    root = path.parent
    sources: list[SourceConfig] = []
    for name, values in _table(raw, "sources").items():
        if not name.isidentifier() or name.startswith("_"):
            raise GenerationError(f"Invalid source name {name!r}")
        _keys(
            values,
            {
                "kind",
                "input",
                "source",
                "output",
                "factory",
                "operations",
                "identities",
                "names",
            },
            f"source {name}",
        )
        kind = values.get("kind")
        if not isinstance(kind, str) or kind not in {"openapi", "openrpc", "webrtc"}:
            raise GenerationError(
                f"Source {name} has unsupported contract kind {kind!r}"
            )
        if ("input" in values) == ("source" in values):
            raise GenerationError(f"Source {name} needs exactly one of input or source")
        if "source" in values and (
            not isinstance(values["source"], str) or not values["source"]
        ):
            raise GenerationError(
                f"Source {name} requires a module:attribute reference"
            )
        if "source" in values and "output" not in values:
            raise GenerationError(f"Live source {name} needs a contract output path")
        if "input" in values and ("output" in values or "factory" in values):
            raise GenerationError(
                f"Saved source {name} cannot declare output or factory"
            )
        if type(values.get("factory", False)) is not bool:
            raise GenerationError(f"Source {name} factory must be a boolean")
        mappings = {
            key: _strings(values.get(key, {}), f"{name}.{key}")
            for key in ("operations", "identities", "names")
        }
        for value in mappings["operations"].values():
            public_name(value)
        sources.append(
            SourceConfig(
                name=name,
                kind=cast(ContractKind, kind),
                input=_path(root, values.get("input")),
                source=values.get("source"),
                output=_path(root, values.get("output")),
                factory=values.get("factory", False),
                **mappings,
            )
        )
    targets: list[TargetConfig] = []
    source_names = {source.name for source in sources}
    for name, values in _table(raw, "targets").items():
        _keys(
            values,
            {"language", "sources", "output", "package", "client-name", "http-client"},
            f"target {name}",
        )
        language = values.get("language")
        if not isinstance(language, str) or language not in {"python", "typescript"}:
            raise GenerationError(
                f"Target {name} has unsupported language {language!r}"
            )
        selected = values.get("sources")
        if (
            not isinstance(selected, list)
            or not selected
            or any(
                not isinstance(value, str) or value not in source_names
                for value in selected
            )
            or len(set(selected)) != len(selected)
        ):
            raise GenerationError(
                f"Target {name} needs a non-empty unique list of known sources"
            )
        output = _path(root, values.get("output"))
        if output is None or output == root:
            raise GenerationError(
                f"Target {name} needs a dedicated SDK output directory"
            )
        package = values.get("package", "api_client")
        client = values.get("client-name", "ApiClient")
        if (
            not isinstance(package, str)
            or any(not part.isidentifier() for part in package.split("."))
            or not isinstance(client, str)
            or not client.isidentifier()
            or keyword.iskeyword(client)
            or any(keyword.iskeyword(part) for part in package.split("."))
        ):
            raise GenerationError(f"Target {name} has invalid package or client-name")
        backend = values.get("http-client", "httpx")
        if (
            not isinstance(backend, str)
            or backend not in {"httpx", "httpx2"}
            or (language != "python" and "http-client" in values)
        ):
            raise GenerationError(f"Target {name} has unsupported http-client")
        targets.append(
            TargetConfig(
                name,
                cast(Literal["python", "typescript"], language),
                tuple(selected),
                output,
                package,
                client,
                cast(Literal["httpx", "httpx2"], backend),
            )
        )
    if not sources or not targets:
        raise GenerationError("Canon configuration needs sources and targets")
    outputs = [target.output for target in targets]
    contract_paths = [source.output or source.input for source in sources]
    for index, output in enumerate(outputs):
        if any(
            output == other
            or output.is_relative_to(other)
            or other.is_relative_to(output)
            for other in outputs[index + 1 :]
        ):
            raise GenerationError("SDK output directories must not overlap")
        if any(
            contract is not None
            and (contract == output or contract.is_relative_to(output))
            for contract in contract_paths
        ):
            raise GenerationError(
                "Saved contracts must stay outside SDK output directories"
            )
    if len({source.output for source in sources if source.output}) != sum(
        source.output is not None for source in sources
    ):
        raise GenerationError("Live contract output paths must be distinct")
    return Configuration(path, tuple(sources), tuple(targets))


def _keys(value: Any, allowed: set[str], owner: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed:
        raise GenerationError(
            f"Unknown or invalid settings in {owner}: {set(value) - allowed if isinstance(value, dict) else value}"
        )


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict) or any(
        not isinstance(item, dict) for item in value.values()
    ):
        raise GenerationError(f"{name} must be a table of named tables")
    return value


def _strings(raw: Any, owner: str) -> dict[str, str]:
    if not isinstance(raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in raw.items()
    ):
        raise GenerationError(f"{owner} must map strings to non-empty strings")
    return raw


def _path(root: Path, value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GenerationError("Contract and SDK paths must be non-empty strings")
    import os

    return Path(os.path.abspath(root / value))
