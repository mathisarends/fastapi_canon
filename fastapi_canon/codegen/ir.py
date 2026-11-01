from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type Interaction = Literal["call", "stream", "session"]
type Protocol = Literal["http", "sse", "jsonrpc", "websocket", "webrtc"]
type ContractKind = Literal["openapi", "openrpc", "webrtc"]


class GenerationError(ValueError):
    """A contract or SDK configuration cannot be represented unambiguously."""


@dataclass(frozen=True)
class Parameter:
    name: str
    wire_name: str
    schema: dict[str, Any]
    required: bool = True
    location: str = "argument"


@dataclass(frozen=True)
class Binding:
    source: str
    protocol: Protocol
    transport: Literal["http", "websocket", "webrtc"]
    operation: str
    backend_path: tuple[str, ...] = ()
    path: str = ""
    method: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Operation:
    name: tuple[str, ...]
    interaction: Interaction
    binding: Binding
    parameters: tuple[Parameter, ...] = ()
    result: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True)
class Source:
    name: str
    kind: ContractKind
    document: dict[str, Any]
    operations: tuple[Operation, ...]


@dataclass(frozen=True)
class Model:
    name: str
    identity: str
    schema: dict[str, Any]
    origin: str
    problem_type: str | None = None


@dataclass(frozen=True)
class Plan:
    sources: tuple[Source, ...]
    operations: tuple[Operation, ...]
    models: tuple[Model, ...]
