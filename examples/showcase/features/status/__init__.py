from fastapi_canon import Feature

from .router import router

feature = Feature(routers=[router])

__all__ = ["feature"]
