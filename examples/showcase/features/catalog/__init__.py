from fastapi_canon import Feature

from .errors import registry
from .providers import CatalogProvider
from .router import router

feature = Feature(
    routers=[router],
    providers=[CatalogProvider()],
    errors=registry,
)

__all__ = ["feature"]
