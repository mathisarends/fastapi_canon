from fastapi import APIRouter

router = APIRouter(tags=["status"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
