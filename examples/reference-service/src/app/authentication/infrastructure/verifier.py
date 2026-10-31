from hmac import compare_digest

from app.authentication.application.errors import AuthenticationFailed
from app.authentication.application.principal import AuthenticatedPrincipal
from app.authentication.application.token_verifier import TokenVerifier
from app.authentication.infrastructure.settings import AuthenticationSettings


class LocalTokenVerifier(TokenVerifier):
    """Verify the single opaque token used by this local reference service."""

    def __init__(self, settings: AuthenticationSettings) -> None:
        self._settings = settings

    def verify(self, *, token: str) -> AuthenticatedPrincipal:
        expected = self._settings.local_bearer_token.get_secret_value()
        if not compare_digest(token, expected):
            raise AuthenticationFailed
        return AuthenticatedPrincipal(user_id=self._settings.local_user_id)
