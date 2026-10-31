import importlib
from dataclasses import replace

from fastapi_canon.integrations.websockets.channel import RpcChannel
from fastapi_canon.integrations.websockets.contract import RpcContract
from fastapi_canon.integrations.websockets.protocol import RpcProtocol
from fastapi_canon.integrations.websockets.schema.openrpc import Server
from fastapi_canon.integrations.websockets.service import RpcService


class ProtocolReferenceError(Exception):
    """Raised when a ``module:attribute`` reference names no RPC source."""


type ContractSource = RpcContract


def load_contract_source(reference: str) -> ContractSource:
    """Import a contract named by ``module:attribute``."""
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ProtocolReferenceError(
            f"Expected a module:attribute reference, got {reference!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ProtocolReferenceError(f"Cannot import {module_name}: {error}") from error
    try:
        source = getattr(module, attribute_name)
    except AttributeError as error:
        raise ProtocolReferenceError(
            f"{module_name} has no attribute {attribute_name}"
        ) from error
    if isinstance(source, RpcService):
        raise ProtocolReferenceError(
            f"{reference} is an RpcService; export "
            "rpc.contract(title=..., base_url=...) instead"
        )
    if isinstance(source, RpcChannel):
        raise ProtocolReferenceError(
            f"{reference} is an RpcChannel; add it to an RpcService and export "
            "rpc.contract(title=..., base_url=...) instead"
        )
    if not isinstance(source, RpcContract):
        raise ProtocolReferenceError(
            f"{reference} is a {type(source).__name__}, not an RpcContract"
        )
    return source


def load_protocol(reference: str) -> RpcProtocol:
    """Import a channel or contract and return its internal protocol."""
    source = load_contract_source(reference)
    return source.protocol


def render_contract(
    source: ContractSource,
    *,
    title: str | None = None,
    description: str | None = None,
    servers: tuple[Server, ...] | None = None,
) -> str:
    """Render a contract as the JSON text committed to the repository."""
    resolved_title = source.title if title is None else title
    resolved_description = source.description if description is None else description
    resolved_servers = tuple(source.servers) if servers is None else servers
    return replace(
        source,
        title=resolved_title,
        description=resolved_description,
        servers=resolved_servers,
    ).to_json()
