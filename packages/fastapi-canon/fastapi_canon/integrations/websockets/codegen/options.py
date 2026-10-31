from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fastapi_canon.integrations.websockets.codegen.bindings import ProtocolBinding


@dataclass(frozen=True, slots=True)
class PythonClientOptions:
    package: str
    client_name: str | None = None
    base_model_name: str = "RpcModel"
    api_root: str | None = None
    api_names: Mapping[str, str] = field(default_factory=dict)
    source: str = "the OpenRPC document"
    with_transport: str | None = None
    extra_files: Mapping[str, str] = field(default_factory=dict)
    extra_exports: Mapping[str, str] = field(default_factory=dict)
    bindings: tuple[ProtocolBinding, ...] = ()
    """Typed clients for sockets that declare the bindings' protocols."""
    contract: str | Path | None = None
    """The contract's URL or path; bindings resolve relative schemas against it."""


@dataclass(frozen=True, slots=True)
class TypeScriptClientOptions:
    client_name: str | None = None
    transport_module: str = "../transport"
    api_root: str | None = None
    api_names: Mapping[str, str] = field(default_factory=dict)
    source: str = "the OpenRPC document"
    with_transport: str | None = None
    bindings: tuple[ProtocolBinding, ...] = ()
    """Typed clients for sockets that declare the bindings' protocols."""
    contract: str | Path | None = None
    """The contract's URL or path; bindings resolve relative schemas against it."""
