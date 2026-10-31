from functools import lru_cache
from typing import Any

try:
    from jinja2 import Environment, PackageLoader, StrictUndefined
except ModuleNotFoundError as error:
    raise ModuleNotFoundError(
        "Client generation requires the optional codegen dependencies; "
        "install fastapi-canon[codegen]",
        name="jinja2",
    ) from error


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader(
            "fastapi_canon.integrations.websockets.codegen", "templates"
        ),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_template(
    name: str,
    *,
    filters: dict[str, Any] | None = None,
    **context: Any,
) -> str:
    environment = _environment()
    if filters:
        environment = environment.overlay()
        environment.filters.update(filters)
    return environment.get_template(name).render(**context)
