import pytest
from fastapi import APIRouter, FastAPI

from fastapi_canon import (
    Composition,
    Error,
    ErrorConfigurationError,
    ErrorOptions,
    ErrorRegistry,
    Feature,
    FeatureConfigurationError,
    validate_openapi_contracts,
)


class DeclaredError(Exception):
    pass


class InstalledError(Exception):
    pass


def test_validate_openapi_contracts_runs_installed_contract_checks() -> None:
    declared = Error(
        DeclaredError,
        status=404,
        code="declared_error",
        title="Declared error",
    )
    installed = Error(
        InstalledError,
        status=409,
        code="installed_error",
        title="Installed error",
    )
    declared_registry = ErrorRegistry(errors=[declared])
    router = APIRouter()
    router.get("/resource", responses=declared_registry.responses(declared))(
        lambda: None
    )
    app = FastAPI()
    app.include_router(router)
    installed_registry = ErrorRegistry(
        errors=[installed],
        type_base="https://example.test/problems",
    )
    installed_registry.install(app)

    with pytest.raises(
        ErrorConfigurationError,
        match="exact definition is missing",
    ):
        validate_openapi_contracts(app)


def test_composition_validate_requires_and_validates_its_application() -> None:
    composition = Composition(
        Feature(name="health"),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    )
    app = FastAPI()

    with pytest.raises(FeatureConfigurationError, match="must be applied"):
        composition.validate(app)

    composition.apply(app)
    composition.validate(app)
    assert app.openapi_schema is not None
