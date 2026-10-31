import json
from typing import Any

from pydantic import BaseModel

from fastapi_canon.integrations.websockets.errors import RpcParseError


class RpcCodec:
    """Encode and decode JSON-RPC messages independently of a transport."""

    def decode(self, message: str | bytes | bytearray) -> object:
        try:
            return json.loads(message)
        except (ValueError, RecursionError) as error:
            raise RpcParseError() from error

    def encode(self, message: BaseModel | list[BaseModel]) -> str:
        if isinstance(message, list):
            value: Any = [
                item.model_dump(mode="json", by_alias=True) for item in message
            ]
        else:
            value = message.model_dump(mode="json", by_alias=True)
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
