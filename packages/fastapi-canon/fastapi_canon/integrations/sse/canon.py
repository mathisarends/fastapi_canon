"""Canon error contracts for typed SSE endpoints."""

from fastapi_canon.integrations.sse.router import StreamRouter
from fastapi_canon.router import CanonRouter

__all__ = ["CanonStreamRouter"]


class CanonStreamRouter(CanonRouter, StreamRouter):
    """Combine canon contracts with native typed GET/POST SSE generators.

    StreamRouter.stream() registers through CanonRouter.api_route(), preserving
    canon's registry validation, shared/local raises, and contract compilation.
    Feature discovers the registry through the CanonRouter base class.

    Run HTTP preconditions in dependencies. Even code before a generator's first
    yield runs after the response starts. ``raises=`` documents HTTP failures;
    errors during streaming need an application-specific event protocol.
    """
