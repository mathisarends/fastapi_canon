from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from fastapi_canon.integrations.websockets.codegen.ir import (
    SocketDecl,
    SocketProtocolDecl,
    UnsupportedSchemaError,
)

ENTRY_POINT_GROUP = "fastapi_canon.integrations.websockets.socket_bindings"

type BindingLanguage = Literal["python", "typescript"]

__all__ = [
    "ENTRY_POINT_GROUP",
    "BindingContext",
    "BindingLanguage",
    "ProtocolBinding",
    "SocketBinding",
    "bind_sockets",
    "load_binding",
]


@dataclass(frozen=True, slots=True)
class BindingContext:
    """What a binding learns about the client it extends."""

    language: BindingLanguage
    protocols: tuple[SocketProtocolDecl, ...]
    """The distinct declarations of the binding's protocol in this contract."""
    contract: str | Path | None = None
    """The contract's URL or file path, for resolving relative schemas."""

    def schemas(self) -> tuple[str, ...]:
        """Every declared schema reference, resolved against the contract."""
        resolved = (
            schema
            for protocol in self.protocols
            for schema in protocol.resolve_schemas(self.contract)
        )
        return tuple(dict.fromkeys(resolved))


@dataclass(frozen=True, slots=True)
class SocketBinding:
    """A typed client for one protocol, emitted into one generated client.

    ``source`` becomes the module ``bindings/<module>`` of the generated client.
    It must define ``open`` and ``session``: ``open`` receives the generic socket
    and returns the typed session that socket methods return.

    Python: ``open(socket: AbstractAsyncContextManager[Socket])`` returns an
    ``AbstractAsyncContextManager[session]``; import ``Socket`` from
    ``..sockets``. TypeScript: ``open(socket: Socket)`` returns ``session`` or
    a promise of it; import ``Socket`` from ``"../sockets"``.
    """

    module: str
    source: str
    open: str
    session: str


@runtime_checkable
class ProtocolBinding(Protocol):
    """Generates typed client access for sockets that declare one protocol."""

    @property
    def protocol(self) -> str: ...

    def bind(self, context: BindingContext) -> SocketBinding: ...


def load_binding(reference: str) -> ProtocolBinding:
    """Load a binding registered as an entry point or named as ``module:attr``."""
    if ":" in reference:
        module_name, _, attribute = reference.partition(":")
        try:
            binding = getattr(import_module(module_name), attribute)
        except (ImportError, AttributeError) as error:
            raise ValueError(
                f"Cannot load socket binding {reference!r}: {error}"
            ) from error
    else:
        matches = entry_points(group=ENTRY_POINT_GROUP, name=reference)
        if not matches:
            raise ValueError(
                f"Unknown socket binding {reference!r}; register it in the "
                f"{ENTRY_POINT_GROUP!r} entry point group or pass module:attr"
            )
        binding = next(iter(matches)).load()
    if not isinstance(binding, ProtocolBinding):
        raise ValueError(f"Socket binding {reference!r} is not a ProtocolBinding")
    return binding


def bind_sockets(
    sockets: Iterable[SocketDecl],
    bindings: Sequence[ProtocolBinding],
    language: BindingLanguage,
    contract: str | Path | None = None,
) -> dict[str, SocketBinding]:
    """Bind each configured protocol once; the result is keyed by protocol name."""
    sockets = tuple(sockets)
    declared: dict[str, list[SocketProtocolDecl]] = {}
    for socket in sockets:
        if socket.protocol is not None:
            protocols = declared.setdefault(socket.protocol.name, [])
            if socket.protocol not in protocols:
                protocols.append(socket.protocol)
    bound: dict[str, SocketBinding] = {}
    modules: set[str] = set()
    for binding in bindings:
        name = binding.protocol
        if name in bound:
            raise UnsupportedSchemaError(f"Duplicate socket binding for {name!r}")
        if name not in declared:
            raise UnsupportedSchemaError(
                f"Socket binding {name!r} matches no socket protocol in the contract"
            )
        if any(
            socket.protocol is not None
            and socket.protocol.name == name
            and socket.client_message is not None
            for socket in sockets
        ):
            raise UnsupportedSchemaError(
                f"Socket binding {name!r} cannot wrap typed JSON sockets; "
                "use their generated message API or an untyped external socket"
            )
        result = binding.bind(BindingContext(language, tuple(declared[name]), contract))
        _validate(name, result)
        if result.module in modules:
            raise UnsupportedSchemaError(
                f"Socket bindings share the module name {result.module!r}"
            )
        modules.add(result.module)
        bound[name] = result
    return bound


def _validate(protocol: str, binding: object) -> None:
    if not isinstance(binding, SocketBinding):
        raise UnsupportedSchemaError(
            f"Socket binding {protocol!r} must return a SocketBinding"
        )
    names = (binding.module, binding.open, binding.session)
    if not all(name.isidentifier() for name in names):
        raise UnsupportedSchemaError(
            f"Socket binding {protocol!r} module, open, and session must be identifiers"
        )
    if binding.open == binding.session:
        raise UnsupportedSchemaError(
            f"Socket binding {protocol!r} open and session must differ"
        )
