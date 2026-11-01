"""Explicit, isolated trace capture; requires the application test SDK dependency."""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

type SpanTree = str | tuple[str, Sequence[SpanTree]]


class SpanCapture:
    """An in-memory provider that never changes OpenTelemetry's global provider.

    Pass ``capture.provider`` to ``Telemetry(tracer_provider=...)``. Use as a
    context manager to shut down the provider after the application exits.
    """

    def __init__(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def __enter__(self) -> SpanCapture:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.provider.shutdown()

    @property
    def spans(self) -> tuple[ReadableSpan, ...]:
        return self.exporter.get_finished_spans()

    def clear(self) -> None:
        self.exporter.clear()

    def one(self, name: str) -> ReadableSpan:
        matches = [span for span in self.spans if span.name == name]
        assert len(matches) == 1, f"expected one {name!r}, found {len(matches)}"
        return matches[0]

    def assert_tree(self, root: str, children: Sequence[SpanTree]) -> None:
        """Match direct parentage and sibling order, ignoring unlisted children."""
        self._children(self.one(root), children)

    def _children(self, parent: ReadableSpan, expected: Sequence[SpanTree]) -> None:
        assert parent.context is not None
        children = sorted(
            (
                span
                for span in self.spans
                if span.parent is not None
                and span.parent.span_id == parent.context.span_id
                and span.parent.trace_id == parent.context.trace_id
            ),
            key=lambda span: span.start_time or 0,
        )
        position = 0
        for item in expected:
            name, descendants = (item, ()) if isinstance(item, str) else item
            while position < len(children) and children[position].name != name:
                position += 1
            assert position < len(children), (
                f"missing child {name!r} below {parent.name!r}; found {[span.name for span in children]}"
            )
            child = children[position]
            position += 1
            self._children(child, descendants)
