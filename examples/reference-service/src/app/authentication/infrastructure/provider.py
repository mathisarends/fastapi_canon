from dishka import Provider, Scope, provide

from app.authentication.application import TokenVerifier
from app.authentication.infrastructure.settings import AuthenticationSettings
from app.authentication.infrastructure.verifier import LocalTokenVerifier


class AuthenticationProvider(Provider):
    @provide(scope=Scope.APP)
    def settings(self) -> AuthenticationSettings:
        return AuthenticationSettings()

    @provide(scope=Scope.APP)
    def token_verifier(self, settings: AuthenticationSettings) -> TokenVerifier:
        return LocalTokenVerifier(settings)
