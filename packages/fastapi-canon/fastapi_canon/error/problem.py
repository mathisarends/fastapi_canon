import math
from collections.abc import Mapping
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fastapi_canon.error.types import (
    CODE_PATTERN,
    JsonValue,
    is_absolute_uri,
    is_uri_reference,
)


class Problem(BaseModel):
    """The stable Problem Details profile emitted by fastapi-canon."""

    model_config = ConfigDict(extra="allow", frozen=True)

    __pydantic_extra__: dict[str, Any] = Field(init=False)

    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if not is_absolute_uri(value):
            msg = "type must be an absolute URI without a fragment"
            raise ValueError(msg)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value.strip() or "\r" in value or "\n" in value:
            msg = "title must be a non-empty, single-line string"
            raise ValueError(msg)
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: int) -> int:
        if isinstance(value, bool) or not 100 <= value <= 599:
            msg = "status must be an HTTP status code between 100 and 599"
            raise ValueError(msg)
        return value

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        if CODE_PATTERN.fullmatch(value) is None:
            msg = "code must match ^[a-z][a-z0-9_]{2,}$"
            raise ValueError(msg)
        return value

    @field_validator("instance")
    @classmethod
    def _validate_instance(cls, value: str | None) -> str | None:
        if value is not None and not is_uri_reference(value):
            msg = "instance must be a non-empty URI reference"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _validate_extensions(self) -> Self:
        for name, value in (self.__pydantic_extra__ or {}).items():
            if not _is_json_value(value):
                msg = f"extension member {name!r} must be JSON-serializable"
                raise ValueError(msg)
        return self

    def as_dict(self) -> dict[str, JsonValue]:
        """Serialize to a JSON-native object, omitting absent optional members."""
        return cast(
            dict[str, JsonValue],
            self.model_dump(mode="json", exclude_none=True),
        )


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list | tuple):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_json_value(item) for key, item in value.items()
        )
    return False
