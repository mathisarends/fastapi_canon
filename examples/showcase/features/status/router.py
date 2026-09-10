from fastapi_canon import CanonRouter

router = CanonRouter(tags=["status"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
