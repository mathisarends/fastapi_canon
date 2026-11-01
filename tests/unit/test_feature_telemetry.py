import pytest
from fastapi import FastAPI

from fastapi_canon import Composition, FeatureConfigurationError
from fastapi_canon.integrations.opentelemetry import Telemetry


def test_telemetry_requires_native_fastapi_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    monkeypatch.delattr(app, "_telemetry")
    with pytest.raises(FeatureConfigurationError, match=r"FastAPI >=0\.143"):
        Composition(telemetry=Telemetry()).apply(app)
