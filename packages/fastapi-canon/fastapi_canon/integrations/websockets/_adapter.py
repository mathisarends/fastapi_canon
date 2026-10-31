from functools import cache
from typing import Any

from pydantic import TypeAdapter


@cache
def adapter(annotation: Any) -> TypeAdapter[Any]:
    return TypeAdapter(annotation)
