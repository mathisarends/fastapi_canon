from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import SchemaValidator


@dataclass(frozen=True, slots=True)
class SocketMessages:
    client: TypeAdapter[Any]
    server: TypeAdapter[Any]
    output_validator: SchemaValidator

    @classmethod
    def create(cls, incoming: Any, outgoing: Any) -> "SocketMessages":
        client, server = TypeAdapter(incoming), TypeAdapter(outgoing)
        schema = deepcopy(server.core_schema)
        _revalidate_models(schema)
        return cls(client, server, SchemaValidator(schema))

    def receive(self, message: str | bytes) -> Any:
        return self.client.validate_json(message, strict=True)

    def send(self, message: Any) -> str:
        validated = self.output_validator.validate_python(message, strict=True)
        return self.server.dump_json(
            validated, by_alias=True, warnings="error"
        ).decode()


def _revalidate_models(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "model":
            value["config"] = {
                **value.get("config", {}),
                "revalidate_instances": "always",
            }
        elif value.get("type") == "dataclass":
            value["revalidate_instances"] = "always"
        for child in value.values():
            _revalidate_models(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _revalidate_models(child)
