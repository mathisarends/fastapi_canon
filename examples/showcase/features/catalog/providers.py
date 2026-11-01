from dishka import Provider, Scope, provide

from .service import CatalogService


class CatalogProvider(Provider):
    @provide(scope=Scope.APP)
    def catalog_service(self) -> CatalogService:
        """Create the feature's application-scoped catalog use case."""
        return CatalogService()
