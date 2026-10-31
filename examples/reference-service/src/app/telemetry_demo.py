"""Opt-in, application-owned OTLP tracing for the local two-service demo."""

import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import FastAPI
from fastapi_canon import CanonRouter, Composition, ErrorOptions, Feature
from fastapi_canon.integrations.opentelemetry import Telemetry, observe
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import Lifespan

from app.infrastructure.database.provider import DatabaseProvider
from app.main import FEATURES
from app.presentation.health import router as health_router
from app.settings import DatabaseSettings


def _telemetry(name: str) -> tuple[Telemetry, Lifespan[FastAPI]]:
    provider = TracerProvider(
        resource=Resource.create({"service.name": name}),
        sampler=ParentBased(TraceIdRatioBased(1.0)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=os.getenv(
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                    "http://localhost:4318/v1/traces",
                ),
            )
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            provider.shutdown()

    return (
        Telemetry(
            tracer_provider=provider,
            metrics=False,
            logs=False,
            exclude=lambda scope: scope["path"] in {"/health", "/ready"},
        ),
        lifespan,
    )


class NotificationGateway(ABC):
    @abstractmethod
    async def send(self) -> None: ...


class HttpNotificationGateway(NotificationGateway):
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self.client = client

    async def send(self) -> None:
        response = await self.client.post("/notifications")
        response.raise_for_status()


def _notifier_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=os.getenv("NOTIFIER_URL", "http://localhost:8001")
    )


def _notification_feature(options: Telemetry) -> Feature:
    class NotifierProvider(Provider):
        @provide(scope=Scope.APP)
        async def client(self) -> AsyncIterator[httpx2.AsyncClient]:
            async with _notifier_client() as client:
                # Upstream HTTPX2 helpers still expose legacy HTTPX annotations.
                HTTPX2ClientInstrumentor.instrument_client(
                    client,  # type: ignore[arg-type]
                    tracer_provider=options.tracer_provider,
                )
                try:
                    yield client
                finally:
                    HTTPX2ClientInstrumentor.uninstrument_client(
                        client,  # type: ignore[arg-type]
                    )

        gateway = provide(
            HttpNotificationGateway, provides=NotificationGateway, scope=Scope.REQUEST
        )
        gateway_telemetry = observe(NotificationGateway)

    router = CanonRouter()

    @router.post("/demo/notify")
    @inject
    async def notify(gateway: FromDishka[NotificationGateway]) -> dict[str, str]:
        await gateway.send()
        return {"status": "sent"}

    return Feature(
        name="notifications", routers=(router,), providers=(NotifierProvider,)
    )


def create_tasks_app() -> FastAPI:
    options, lifecycle = _telemetry("tasks")

    class TracedDatabaseProvider(DatabaseProvider):
        @provide(scope=Scope.APP)
        async def engine(
            self, settings: DatabaseSettings
        ) -> AsyncIterator[AsyncEngine]:
            engine = create_async_engine(settings.url, pool_pre_ping=True)
            instrumentation = SQLAlchemyInstrumentor()
            instrumentation.instrument(
                engine=engine.sync_engine, tracer_provider=options.tracer_provider
            )
            try:
                yield engine
            finally:
                instrumentation.uninstrument(engine=engine.sync_engine)
                await engine.dispose()

    app = FastAPI(title="Canon telemetry: tasks", lifespan=lifecycle)
    app.include_router(health_router)
    Composition(
        Feature(name="database", providers=(TracedDatabaseProvider,)),
        *FEATURES[1:],
        _notification_feature(options),
        telemetry=options,
        errors=ErrorOptions(type_base="https://api.example.test/problems"),
        router_factory=lambda: CanonRouter(prefix="/api/v1"),
    ).apply(app)
    return app


def create_notifier_app() -> FastAPI:
    options, lifecycle = _telemetry("notifier")
    router = CanonRouter()

    @router.post("/notifications")
    async def notify() -> dict[str, str]:
        return {"status": "received"}

    return Composition(
        Feature(name="notifications", routers=(router,)),
        telemetry=options,
    ).apply(FastAPI(title="Canon telemetry: notifier", lifespan=lifecycle))
