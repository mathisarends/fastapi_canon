from fastapi_canon import Feature

from .providers import CatalogProvider
from .router import router

feature = Feature(
    name="catalog",
    routers=[router],
    providers=[CatalogProvider()],
)

__all__ = ["feature"]
