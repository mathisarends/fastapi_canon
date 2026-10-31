from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from fastapi_canon.error import (
    Error,
    ErrorConfigurationError,
    ErrorRegistry,
)


class DomainError(Exception):
    pass


class SessionNotFound(DomainError):
    pass


class ExpiredSession(SessionNotFound):
    pass


class AccountNotFound(DomainError):
    pass


class UnknownError(Exception):
    pass


def make_error(
    exception: type[Exception],
    code: str,
    *,
    type_uri: str | None = None,
    schema_name: str | None = None,
) -> Error[Exception]:
    factory = cast("Callable[..., Error[Exception]]", Error)
    return factory(
        exception,
        status=404,
        code=code,
        title=code.replace("_", " ").title(),
        type=type_uri,
        schema_name=schema_name,
    )


def test_registry_preserves_order_and_resolves_type_base() -> None:
    session = make_error(SessionNotFound, "session_not_found")
    account = make_error(AccountNotFound, "account_not_found")

    registry = ErrorRegistry(
        name="api",
        errors=[session, account],
        type_base="https://api.example.com/problems/",
    )

    assert registry.name == "api"
    assert registry.type_base == "https://api.example.com/problems"
    assert registry.errors == (session, account)
    assert tuple(registry) == (session, account)
    assert len(registry) == 2
    assert (
        registry.type_uri_for(session)
        == "https://api.example.com/problems/session_not_found"
    )


def test_registry_deduplicates_same_error_identity() -> None:
    error = make_error(SessionNotFound, "session_not_found")
    registry = ErrorRegistry(errors=[error, error])

    assert registry.errors == (error,)


@pytest.mark.parametrize("name", ["", "two\nlines", 42])
def test_registry_rejects_invalid_name(name: object) -> None:
    with pytest.raises(ErrorConfigurationError):
        ErrorRegistry(errors=[], name=name)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "type_base",
    ["/relative", "https://api.example.com/problems?version=1", 42],
)
def test_registry_rejects_invalid_type_base(type_base: object) -> None:
    with pytest.raises(ErrorConfigurationError):
        ErrorRegistry(errors=[], type_base=type_base)  # type: ignore[arg-type]


def test_registry_rejects_non_error_members() -> None:
    with pytest.raises(ErrorConfigurationError):
        ErrorRegistry(errors=[object()])  # type: ignore[list-item]


def test_registry_retains_explicit_type() -> None:
    error = make_error(
        SessionNotFound,
        "session_not_found",
        type_uri="urn:example:session-not-found",
    )
    registry = ErrorRegistry(
        errors=[error], type_base="https://api.example.com/problems"
    )

    assert registry.type_uri_for(error) == "urn:example:session-not-found"


def test_registry_requires_all_types_before_installation() -> None:
    error = make_error(SessionNotFound, "session_not_found")
    registry = ErrorRegistry(errors=[error])

    with pytest.raises(ErrorConfigurationError, match="session_not_found"):
        registry.require_resolved()


def test_registry_accepts_empty_resolved_collection() -> None:
    registry = ErrorRegistry(errors=[])

    registry.require_resolved()


def test_merge_retains_feature_type_base_and_fills_unresolved_errors() -> None:
    session = make_error(SessionNotFound, "session_not_found")
    account = make_error(AccountNotFound, "account_not_found")
    sessions = ErrorRegistry(
        name="sessions",
        errors=[session],
        type_base="https://sessions.example.com/problems",
    )
    accounts = ErrorRegistry(name="accounts", errors=[account])

    merged = ErrorRegistry.merge(
        sessions,
        accounts,
        type_base="https://api.example.com/problems",
    )

    assert (
        merged.type_uri_for(session)
        == "https://sessions.example.com/problems/session_not_found"
    )
    assert (
        merged.type_uri_for(account)
        == "https://api.example.com/problems/account_not_found"
    )


def test_merge_prefers_existing_resolution_for_shared_error() -> None:
    shared = make_error(SessionNotFound, "session_not_found")
    unresolved = ErrorRegistry(name="unresolved", errors=[shared])
    resolved = ErrorRegistry(
        name="resolved",
        errors=[shared],
        type_base="https://feature.example.com/problems",
    )

    merged = ErrorRegistry.merge(
        unresolved,
        resolved,
        type_base="https://api.example.com/problems",
    )

    assert (
        merged.type_uri_for(shared)
        == "https://feature.example.com/problems/session_not_found"
    )


def test_merge_rejects_shared_error_with_incompatible_feature_bases() -> None:
    shared = make_error(SessionNotFound, "session_not_found")
    first = ErrorRegistry(
        name="first", errors=[shared], type_base="https://one.example/problems"
    )
    second = ErrorRegistry(
        name="second", errors=[shared], type_base="https://two.example/problems"
    )

    with pytest.raises(ErrorConfigurationError, match="incompatible type URIs"):
        ErrorRegistry.merge(first, second)


def test_merge_rejects_non_registry_argument() -> None:
    with pytest.raises(ErrorConfigurationError):
        ErrorRegistry.merge(object())  # type: ignore[arg-type]


def test_merge_reports_original_feature_owners_on_collision() -> None:
    sessions = ErrorRegistry(
        name="sessions",
        errors=[make_error(SessionNotFound, "resource_not_found")],
    )
    accounts = ErrorRegistry(
        name="accounts",
        errors=[make_error(AccountNotFound, "resource_not_found")],
    )

    with pytest.raises(ErrorConfigurationError) as error:
        ErrorRegistry.merge(sessions, accounts, name="api")

    assert "sessions" in str(error.value)
    assert "accounts" in str(error.value)


def test_resolution_uses_most_specific_registered_mro_class() -> None:
    domain = make_error(DomainError, "domain_error")
    missing = make_error(SessionNotFound, "session_not_found")
    registry = ErrorRegistry(
        errors=[domain, missing], type_base="https://api.example.com/problems"
    )

    assert registry.resolve(ExpiredSession()) is missing
    assert registry.resolve(AccountNotFound()) is domain
    assert registry.resolve(UnknownError()) is None


def test_registry_rejects_lookup_for_foreign_error() -> None:
    registry = ErrorRegistry(errors=[])
    foreign = make_error(SessionNotFound, "session_not_found")

    with pytest.raises(ErrorConfigurationError):
        registry.type_uri_for(foreign)


def test_registry_is_frozen() -> None:
    registry = ErrorRegistry(errors=[])

    with pytest.raises(FrozenInstanceError):
        registry.name = "changed"  # type: ignore[misc]
