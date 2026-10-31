from fastapi_canon import Error, ErrorRegistry

from app.authentication.application import AuthenticationFailed

authentication_required = Error(
    AuthenticationFailed,
    status=401,
    code="authentication_required",
    title="Authentication required",
    detail="Authentication required",
    headers={"WWW-Authenticate": "Bearer"},
    openapi_headers={
        "WWW-Authenticate": {
            "description": "Authentication scheme required by this API.",
            "schema": {"type": "string"},
        }
    },
)
errors = ErrorRegistry(name="authentication", errors=[authentication_required])
