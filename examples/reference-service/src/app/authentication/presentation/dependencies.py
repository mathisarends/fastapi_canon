from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.authentication.application import AuthenticationFailed, TokenVerifier

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Opaque bearer token for the local reference service.",
)


@inject
async def authenticated_user_id(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer),
    ],
    verifier: FromDishka[TokenVerifier],
) -> UUID:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationFailed
    principal = verifier.verify(token=credentials.credentials)
    return principal.user_id


AuthenticatedUserId = Annotated[UUID, Depends(authenticated_user_id)]
