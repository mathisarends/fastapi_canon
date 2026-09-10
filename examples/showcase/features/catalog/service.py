from .exceptions import ProductNotFound, ProductUnavailable
from .models import Product


class CatalogService:
    """A replaceable in-memory implementation for the showcase."""

    def __init__(self) -> None:
        self._stock = {"coffee": 3, "tea": 0}

    def get_product(self, product_id: str) -> Product:
        try:
            available = self._stock[product_id]
        except KeyError:
            raise ProductNotFound(product_id) from None
        return Product(id=product_id, available=available)

    def reserve_product(self, product_id: str, quantity: int) -> Product:
        product = self.get_product(product_id)
        if quantity > product.available:
            raise ProductUnavailable(product_id, product.available)

        remaining = product.available - quantity
        self._stock[product_id] = remaining
        return Product(id=product_id, available=remaining)
