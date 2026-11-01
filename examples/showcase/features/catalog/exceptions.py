class ProductNotFound(Exception):
    """Raised when a product identifier has no catalog entry."""

    def __init__(self, product_id: str) -> None:
        self.product_id = product_id


class ProductUnavailable(Exception):
    """Raised when a reservation would exceed available stock."""

    def __init__(self, product_id: str, available: int) -> None:
        self.product_id = product_id
        self.available = available
