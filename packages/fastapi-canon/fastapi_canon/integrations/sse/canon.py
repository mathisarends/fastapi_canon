import inspect
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing
from functools import wraps
from typing import Any, cast, get_args, get_type_hints

from fastapi import Request
from pydantic import TypeAdapter
from starlette.concurrency import iterate_in_threadpool

from fastapi_canon.error import ErrorConfigurationError, ErrorRegistry
from fastapi_canon.error.catalog import app_registry, internal_problem, problem_for
from fastapi_canon.error.registry import AnyError
from fastapi_canon.integrations.sse.router import (
    ServerSentEvent,
    StreamRouter,
    validate_endpoint,
)
from fastapi_canon.policy import normalize_raises
from fastapi_canon.router import CanonRouter

__all__ = ["CanonStreamRouter"]

logger = logging.getLogger(__name__)


class CanonStreamRouter(CanonRouter, StreamRouter):
    """Typed SSE with Canon HTTP admission and terminal problem contracts."""

    def __init__(
        self, *, stream_raises: Sequence[AnyError] = (), **options: Any
    ) -> None:
        super().__init__(**options)
        self.stream_raises = normalize_raises(
            stream_raises, registry=self.error_registry, parameter="stream_raises"
        )

    def stream[Endpoint: Callable[..., Any]](
        self,
        path: str,
        *,
        methods: list[str] | None = None,
        stream_raises: Sequence[AnyError] = (),
        **options: Any,
    ) -> Callable[[Endpoint], Endpoint]:
        """Declare HTTP failures with raises and processing failures with stream_raises.

        Normal exhaustion sends canon.complete. Processing failures send a terminal
        canon.problem. Disconnects and cancellation propagate through cleanup.
        """
        registry = self.error_registry
        local = normalize_raises(
            stream_raises, registry=registry, parameter="stream_raises"
        )
        errors = tuple(dict.fromkeys((*self.stream_raises, *local)))
        extra = dict(options.pop("openapi_extra", None) or {})
        if "x-canon-stream" in extra:
            raise ErrorConfigurationError(
                "x-canon-stream is owned by CanonStreamRouter"
            )
        extra["x-canon-stream"] = {
            "version": 1,
            "errorIds": [str(id(error)) for error in errors],
        }
        options["openapi_extra"] = extra

        register = super().stream(path, methods=methods, **options)

        def decorate(endpoint: Endpoint) -> Endpoint:
            validate_endpoint(endpoint)
            wrapped = _terminal_stream(endpoint, registry, errors)
            register(wrapped)
            return endpoint

        return decorate


def _terminal_stream(
    endpoint: Callable[..., Any],
    registry: ErrorRegistry | None,
    errors: tuple[AnyError, ...],
) -> Callable[..., Any]:
    hints = get_type_hints(endpoint, include_extras=True)
    item_type = get_args(hints["return"])[0]
    adapter = TypeAdapter(item_type)
    signature = inspect.signature(endpoint)
    existing_request = next(
        (
            p.name
            for p in signature.parameters.values()
            if hints.get(p.name, p.annotation) is Request
        ),
        None,
    )
    request_name = existing_request or "_canon_request"
    if existing_request is None:
        while request_name in signature.parameters:
            request_name += "_"

    @wraps(endpoint)
    async def wrapped(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        request: Request = kwargs[request_name]
        if existing_request is None:
            kwargs.pop(request_name)
        catalog = app_registry(
            request.app, registry if registry is not None else ErrorRegistry(errors=[])
        )
        source = endpoint(*args, **kwargs)
        iterator = (
            source
            if inspect.isasyncgen(source)
            else cast(AsyncGenerator[Any, None], iterate_in_threadpool(source))
        )
        try:
            async with aclosing(iterator):
                async for item in iterator:
                    if isinstance(item, ServerSentEvent):
                        if item.event and item.event.startswith("canon."):
                            raise ValueError("canon.* SSE event names are reserved")
                        if (
                            item.raw_data is not None
                            or item.comment is not None
                            and item.data is None
                        ):
                            raise ValueError(
                                "Canon streams require typed JSON business events"
                            )
                        value = adapter.validate_python(item.data)
                        yield item.model_copy(
                            update={
                                "data": None,
                                "raw_data": adapter.dump_json(
                                    value, by_alias=True
                                ).decode("utf-8"),
                            }
                        )
                    else:
                        value = adapter.validate_python(item)
                        yield ServerSentEvent(
                            raw_data=adapter.dump_json(value, by_alias=True).decode(
                                "utf-8"
                            )
                        )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # This boundary handles arbitrary application generator exceptions.
            declared = catalog.resolve(exc)
            try:
                problem = (
                    problem_for(catalog, exc)
                    if declared is not None and any(declared is e for e in errors)
                    else None
                )
            except Exception:  # pylint: disable=broad-exception-caught
                # Public renderers are application callbacks and may fail arbitrarily.
                logger.exception("SSE problem renderer failed")
                problem = None
            if problem is None:
                logger.error("SSE operation %s failed", endpoint.__name__, exc_info=exc)
                problem = internal_problem(catalog)
            yield ServerSentEvent(event="canon.problem", data=problem.as_dict())
            return
        finally:
            if inspect.isgenerator(source):
                source.close()
        yield ServerSentEvent(event="canon.complete", data={})

    parameters = [
        p.replace(annotation=hints.get(p.name, p.annotation))
        for p in signature.parameters.values()
    ]
    position = next(
        (
            i
            for i, p in enumerate(parameters)
            if p.kind == inspect.Parameter.VAR_KEYWORD
        ),
        len(parameters),
    )
    if existing_request is None:
        parameters.insert(
            position,
            inspect.Parameter(
                request_name, inspect.Parameter.KEYWORD_ONLY, annotation=Request
            ),
        )
    wrapped.__annotations__ = {
        **{
            p.name: p.annotation
            for p in parameters
            if p.annotation is not inspect.Parameter.empty
        },
        "return": AsyncIterator[item_type],
    }
    cast(Any, wrapped).__signature__ = signature.replace(
        parameters=parameters, return_annotation=AsyncIterator[item_type]
    )
    return wrapped
