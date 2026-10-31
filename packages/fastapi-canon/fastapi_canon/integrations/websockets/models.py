from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class RpcModel(BaseModel):
    """Pythonic RPC payload model with canonical camelCase wire names."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )
