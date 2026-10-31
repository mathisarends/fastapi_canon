from abc import ABC, abstractmethod

from app.authentication.application.principal import AuthenticatedPrincipal


class TokenVerifier(ABC):
    @abstractmethod
    def verify(self, *, token: str) -> AuthenticatedPrincipal: ...
