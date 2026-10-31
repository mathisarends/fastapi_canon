from collections.abc import Callable
from typing import Any, cast

from jinja2 import Environment, PackageLoader, StrictUndefined


def environment() -> Environment:
    return Environment(
        loader=PackageLoader("fastapi_canon.integrations.sse.codegen", "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def add_filters(env: Environment, **filters: Callable[..., Any]) -> None:
    cast(dict[str, Callable[..., Any]], env.filters).update(filters)
