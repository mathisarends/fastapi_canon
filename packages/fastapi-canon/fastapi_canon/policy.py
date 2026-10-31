from collections.abc import Sequence
from dataclasses import dataclass

from fastapi.params import Depends

from fastapi_canon.error.error import Error
from fastapi_canon.error.registry import AnyError, ErrorRegistry
from fastapi_canon.error.types import ErrorConfigurationError
from fastapi_canon.error.validation import unique_instances


@dataclass(frozen=True, slots=True, init=False)
class RouterPolicy:
    """Reusable HTTP prerequisites and their declared error contracts.

    Dependencies execute through FastAPI; ``raises`` documents possible errors.
    Route classes remain a declaration-time choice on each router.
    """

    error_registry: ErrorRegistry | None
    dependencies: tuple[Depends, ...]
    raises: tuple[AnyError, ...]

    def __init__(
        self,
        *,
        error_registry: ErrorRegistry | None = None,
        dependencies: Sequence[Depends] = (),
        raises: Sequence[AnyError] = (),
    ) -> None:
        registry = validate_registry(error_registry)
        raw_dependencies: object = dependencies
        if isinstance(raw_dependencies, str) or not isinstance(
            raw_dependencies, Sequence
        ):
            msg = "dependencies must be a sequence of Depends instances"
            raise ErrorConfigurationError(msg)
        normalized_dependencies = tuple(
            unique_instances(dependencies, Depends, parameter="dependencies")
        )
        normalized_raises = normalize_raises(
            raises, registry=registry, parameter="raises"
        )
        object.__setattr__(self, "error_registry", registry)
        object.__setattr__(self, "dependencies", normalized_dependencies)
        object.__setattr__(self, "raises", normalized_raises)


def validate_registry(value: ErrorRegistry | None) -> ErrorRegistry | None:
    raw: object = value
    if raw is not None and not isinstance(raw, ErrorRegistry):
        msg = "error_registry must be an ErrorRegistry instance or None"
        raise ErrorConfigurationError(msg)
    return value


def validate_policy(value: RouterPolicy | None) -> None:
    raw: object = value
    if raw is not None and not isinstance(raw, RouterPolicy):
        msg = "policy must be a RouterPolicy instance or None"
        raise ErrorConfigurationError(msg)


def merge_policy_registry(
    registry: ErrorRegistry | None, policy: RouterPolicy | None
) -> ErrorRegistry | None:
    shared = policy.error_registry if policy is not None else None
    if shared is None or shared is registry:
        return registry
    if registry is None:
        return shared
    bases = {value.type_base for value in (shared, registry) if value.type_base}
    return ErrorRegistry.merge(
        shared, registry, type_base=next(iter(bases)) if len(bases) == 1 else None
    )


def normalize_raises(
    values: Sequence[AnyError],
    *,
    registry: ErrorRegistry | None,
    parameter: str,
) -> tuple[AnyError, ...]:
    raw_values: object = values
    if isinstance(raw_values, str) or not isinstance(raw_values, Sequence):
        msg = f"{parameter} must be a sequence of Error instances"
        raise ErrorConfigurationError(msg)
    normalized = tuple(unique_instances(values, Error, parameter=parameter))
    if normalized and registry is None:
        msg = f"{parameter} requires an error registry"
        raise ErrorConfigurationError(msg)
    if registry is not None:
        for error in normalized:
            if not registry.contains(error):
                msg = f"error {error.code!r} does not belong to this router's registry"
                raise ErrorConfigurationError(msg)
    return normalized
