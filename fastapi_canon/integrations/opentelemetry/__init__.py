"""Optional OpenTelemetry integration declared on Canon features."""

from .declarations import Instrumentation, Telemetry, instrument

__all__ = ["Instrumentation", "Telemetry", "instrument"]
