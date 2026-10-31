from dishka import FromDishka
from dishka.integrations.fastapi import inject

from fastapi_canon import CanonRouter

from .errors import product_not_found, product_unavailable, registry
from .models import Product, Reservation
from .service import CatalogService

router = CanonRouter(
    prefix="/products",
    tags=["catalog"],
    error_registry=registry,
)


@router.get("/{product_id}", raises=[product_not_found])
@inject
async def get_product(
    product_id: str,
    catalog: FromDishka[CatalogService],
) -> Product:
    return catalog.get_product(product_id)


@router.post(
    "/{product_id}/reservations",
    raises=[product_not_found, product_unavailable],
)
@inject
async def reserve_product(
    product_id: str,
    reservation: Reservation,
    catalog: FromDishka[CatalogService],
) -> Product:
    return catalog.reserve_product(product_id, reservation.quantity)
