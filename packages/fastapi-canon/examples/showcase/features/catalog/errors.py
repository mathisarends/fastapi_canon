from pydantic import BaseModel

from fastapi_canon import Error, ErrorRegistry

from .exceptions import ProductNotFound, ProductUnavailable


class UnavailableExtensions(BaseModel):
    product_id: str
    available: int


product_not_found = Error(
    ProductNotFound,
    status=404,
    code="product_not_found",
    title="Product not found",
    detail=lambda error: f"Product '{error.product_id}' does not exist.",
    description="The requested product identifier is unknown.",
)
product_unavailable = Error(
    ProductUnavailable,
    status=409,
    code="product_unavailable",
    title="Product unavailable",
    detail="The requested quantity is not currently available.",
    extensions_model=UnavailableExtensions,
    extensions=lambda error: {
        "product_id": error.product_id,
        "available": error.available,
    },
    description="The product exists, but not enough units are in stock.",
)

registry = ErrorRegistry(
    name="catalog",
    errors=[product_not_found, product_unavailable],
)
