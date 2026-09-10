# Streaming SSE and PDF responses

Streaming endpoints declare their successful media type with ordinary FastAPI
response metadata. Canon error contracts describe failures that occur before
streaming begins.

The following is a complete example with an SSE endpoint, a streamed PDF, a
domain 404, and normalized authentication errors:

```python
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from fastapi_canon import Composition, Error, ErrorRegistry, Feature


class DocumentMissing(Exception):
    pass


document_missing = Error(
    DocumentMissing,
    status=404,
    code="document_missing",
    title="Document not found",
    detail="The requested document does not exist.",
)
api_errors = ErrorRegistry(
    errors=[document_missing],
    type_base="https://api.example.com/problems",
)


async def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if authorization != "Bearer demo":
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter()


async def event_chunks() -> AsyncIterator[str]:
    yield "event: ready\ndata: {}\n\n"
    yield 'event: message\ndata: {"id": 1}\n\n'


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-sent event stream",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
            },
        },
        **api_errors.responses(http_statuses=[401, 403]),
    },
)
async def events(
    _user: None = Depends(require_user),
) -> StreamingResponse:
    return StreamingResponse(
        event_chunks(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def pdf_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            yield chunk


@router.get(
    "/documents/{document_id}",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "PDF document",
            "headers": {
                "Content-Disposition": {
                    "schema": {"type": "string"},
                }
            },
            "content": {
                "application/pdf": {
                    "schema": {"type": "string", "format": "binary"},
                }
            },
        },
        **api_errors.responses(document_missing, http_statuses=[401, 403]),
    },
)
async def document(
    document_id: UUID,
    _user: None = Depends(require_user),
) -> StreamingResponse:
    path = Path("documents") / f"{document_id}.pdf"
    if not path.is_file():
        raise DocumentMissing
    return StreamingResponse(
        pdf_chunks(path),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{document_id}.pdf"',
        },
    )


streaming_feature = Feature(
    name="streaming",
    routers=[router],
    errors=api_errors,
)
app = Composition(
    streaming_feature,
    router_factory=lambda: APIRouter(prefix="/api/v1"),
).apply(FastAPI())
```

The generated contract has `text/event-stream` and `application/pdf` success
responses. The error responses use `application/problem+json`; 401 and 403
reference `Http401Problem` and `Http403Problem`, while the PDF route's 404
references `DocumentMissingProblem`. Internal `x-fastapi-canon-*` metadata is
removed from the final OpenAPI document.

## Streaming boundary

Run authentication, authorization, validation, resource lookup, and other
expected failure checks before returning `StreamingResponse`. Once response
headers have been sent, an exception inside the iterator cannot be converted
into a new HTTP Problem Details response. For SSE, represent post-start failures
as an explicitly designed SSE event or terminate the stream and let the client
reconnect. For PDF streams, an iterator failure produces an incomplete download.

Do not put exception strings or internal provider messages into SSE events,
filenames, headers, or Problem Details. Keep `code` stable, use reviewed public
detail text, and log internal context on the server.
