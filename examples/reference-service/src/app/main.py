from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi_canon import CanonRouter, Composition, ErrorOptions, Feature

from app.authentication.feature import feature as authentication_feature
from app.features.tasks.feature import feature as tasks_feature
from app.infrastructure.database.provider import DatabaseProvider
from app.presentation.health import router as health_router

if TYPE_CHECKING:
    from fastapi_canon.integrations.opentelemetry import Telemetry

FEATURES: tuple[Feature, ...] = (
    Feature(name="database", providers=[DatabaseProvider]),
    authentication_feature,
    tasks_feature,
)


def create_app(*, telemetry: Telemetry | None = None) -> FastAPI:
    app = FastAPI(title="FastAPI Canon reference service")
    app.include_router(health_router)
    composition = Composition(
        *FEATURES,
        router_factory=lambda: CanonRouter(prefix="/api/v1"),
        errors=ErrorOptions(type_base="https://api.example.test/problems"),
        telemetry=telemetry,
    )
    composition.apply(app)
    composition.validate(app)
    return app


app = create_app()
