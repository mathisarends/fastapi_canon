import inspect
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
)
from typing import Any, TypeAliasType, get_args, get_origin, get_type_hints

from fastapi import APIRouter
from pydantic import TypeAdapter

try:
    from fastapi.sse import EventSourceResponse, ServerSentEvent
except ModuleNotFoundError as error:
    if error.name != "fastapi.sse":
        raise
    raise ModuleNotFoundError(
        "Typed SSE requires newer FastAPI dependencies; install fastapi-canon[sse]",
        name=error.name,
    ) from error

_ASYNC = (AsyncIterator, AsyncIterable, AsyncGenerator)
_SYNC = (Iterator, Iterable, Generator)


class StreamRouter(APIRouter):
    """An APIRouter with typed SSE routes; normal FastAPI routes work unchanged."""

    def stream[Endpoint: Callable[..., Any]](
        self,
        path: str,
        *,
        methods: list[str] | None = None,
        **options: Any,
    ) -> Callable[[Endpoint], Endpoint]:
        """Register a GET (default) or POST JSON-data SSE generator.

        Other options are forwarded to ``APIRouter.api_route``, including tags,
        dependencies, responses and operation_id. Serialization must preserve
        the exported event schema: include must be None, exclude must be empty,
        by_alias must be True, and exclude_none/unset/defaults must be False.
        Put HTTP precondition checks in dependencies: a generator's body executes
        after the response has started, following native FastAPI behavior.
        """
        selected = [
            method.upper() for method in (methods if methods is not None else ["GET"])
        ]
        if not selected or not set(selected) <= {"GET", "POST"}:
            raise ValueError("Streams support GET and POST only")
        if "response_class" in options or "response_model" in options:
            raise TypeError(
                "Streams infer their response model and use EventSourceResponse"
            )
        for option, changes_contract in (
            (
                "response_model_include",
                options.get("response_model_include") is not None,
            ),
            ("response_model_exclude", bool(options.get("response_model_exclude"))),
            (
                "response_model_by_alias",
                options.get("response_model_by_alias", True) is not True,
            ),
            (
                "response_model_exclude_none",
                bool(options.get("response_model_exclude_none")),
            ),
            (
                "response_model_exclude_unset",
                bool(options.get("response_model_exclude_unset")),
            ),
            (
                "response_model_exclude_defaults",
                bool(options.get("response_model_exclude_defaults")),
            ),
        ):
            if changes_contract:
                raise TypeError(
                    f"{option} would change the stream's exported event contract"
                )
        status = options.get("status_code", 200)
        if status is not None and (not 200 <= status < 300 or status in (204, 205)):
            raise ValueError("A stream must use a successful status code with a body")
        options.setdefault("response_description", "Stream of JSON events")

        def decorator(endpoint: Endpoint) -> Endpoint:
            _validate_endpoint(endpoint)
            self.api_route(
                path,
                methods=selected,
                response_class=EventSourceResponse,
                **options,
            )(endpoint)
            return endpoint

        return decorator


def _has_any(annotation: Any, seen: frozenset[int] = frozenset()) -> bool:
    if annotation is Any or any(
        annotation is item for item in (object, dict, list, tuple, set)
    ):
        return True
    if id(annotation) in seen:
        return False
    seen = seen | {id(annotation)}
    if isinstance(annotation, TypeAliasType):
        return _has_any(annotation.__value__, seen)
    return any(_has_any(arg, seen) for arg in get_args(annotation))


def _validate_endpoint(endpoint: Callable[..., Any]) -> None:
    try:
        annotation = get_type_hints(endpoint, include_extras=True).get("return")
    except (NameError, TypeError) as exc:
        raise TypeError(
            f"Cannot resolve stream annotation for {endpoint.__name__}"
        ) from exc
    if inspect.isasyncgenfunction(endpoint):
        origins = _ASYNC
    elif inspect.isgeneratorfunction(endpoint):
        origins = _SYNC
    else:
        raise TypeError("A stream endpoint must be a generator function using yield")
    args = get_args(annotation)
    if get_origin(annotation) not in origins or not args:
        raise TypeError(
            "A stream must declare AsyncIterator[Event] (or Iterable[Event] for def)"
        )
    item = args[0]
    if _has_any(item) or item is ServerSentEvent:
        raise TypeError(
            "Declare a concrete JSON event type, not Any or ServerSentEvent"
        )
    try:
        TypeAdapter(item).json_schema(mode="serialization")
    except Exception as exc:
        raise TypeError(f"Unsupported stream item type: {item!r}") from exc
