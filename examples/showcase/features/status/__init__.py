from fastapi_canon import Feature

from .router import router

feature = Feature(name="status", routers=[router])

__all__ = ["feature"]
