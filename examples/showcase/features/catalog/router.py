from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter

from .errors import product_not_found, product_unavailable, registry
from .models import Product, Reservation
from .service import CatalogService

router = APIRouter(prefix="/products", tags=["catalog"])


@router.get("/{product_id}", responses=registry.responses(product_not_found))
@inject
async def get_product(
    product_id: str,
    catalog: FromDishka[CatalogService],
) -> Product:
    return catalog.get_product(product_id)


@router.post(
    "/{product_id}/reservations",
    responses=registry.responses(product_not_found, product_unavailable),
)
@inject
async def reserve_product(
    product_id: str,
    reservation: Reservation,
    catalog: FromDishka[CatalogService],
) -> Product:
    return catalog.reserve_product(product_id, reservation.quantity)
