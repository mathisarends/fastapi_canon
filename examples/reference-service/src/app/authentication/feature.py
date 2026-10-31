from fastapi_canon import Feature

from app.authentication.infrastructure.provider import AuthenticationProvider
from app.authentication.presentation.errors import errors

feature = Feature(
    name="authentication",
    providers=(AuthenticationProvider,),
    errors=errors,
)
