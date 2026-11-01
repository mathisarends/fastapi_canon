from collections.abc import Iterator, Sequence

from fastapi_canon.error.types import ErrorConfigurationError


def unique_instances[ItemT](
    values: Sequence[object], expected_type: type[ItemT], *, parameter: str
) -> Iterator[ItemT]:
    """Validate every item and yield distinct instances in declaration order."""
    seen: set[int] = set()
    for index, value in enumerate(values):
        if not isinstance(value, expected_type):
            msg = f"{parameter}[{index}] must be a {expected_type.__name__} instance"
            raise ErrorConfigurationError(msg)
        identity = id(value)
        if identity not in seen:
            seen.add(identity)
            yield value
