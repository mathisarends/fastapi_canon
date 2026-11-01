from copy import copy
from functools import reduce
from operator import or_
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from fastapi_canon.integrations.websockets.models import RpcModel

_MODEL_ADAPTERS: dict[type[BaseModel], type[BaseModel]] = {}
_BUILDING: set[type[BaseModel]] = set()


def wire_annotation(annotation: Any) -> Any:
    """Apply the WebSocket field-name convention without changing user models."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _wire_model(annotation)

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return Annotated[wire_annotation(arguments[0]), *arguments[1:]]
    if origin in (Union, UnionType):
        return reduce(or_, (wire_annotation(item) for item in arguments))
    if origin in (list, set, frozenset):
        return origin[wire_annotation(arguments[0])]
    if origin is dict:
        return dict[wire_annotation(arguments[0]), wire_annotation(arguments[1])]
    if origin is tuple:
        return tuple[tuple(wire_annotation(item) for item in arguments)]
    return annotation


def _wire_model(model: type[BaseModel]) -> type[BaseModel]:
    if issubclass(model, RpcModel):
        return model
    existing = _MODEL_ADAPTERS.get(model)
    if existing is not None:
        return existing
    if model in _BUILDING:
        return model

    _BUILDING.add(model)
    try:
        annotations: dict[str, Any] = {}
        namespace: dict[str, Any] = {
            "__annotations__": annotations,
            "__module__": model.__module__,
            "__doc__": model.__doc__,
            "model_config": ConfigDict(
                model.model_config
                | {
                    "alias_generator": to_camel,
                    "validate_by_alias": True,
                    "validate_by_name": True,
                    "serialize_by_alias": True,
                    "extra": "forbid",
                }
            ),
        }
        for name, field in model.model_fields.items():
            annotations[name] = wire_annotation(field.annotation)
            namespace[name] = copy(field)
        adapted = type(model.__name__, (model,), namespace)
        _MODEL_ADAPTERS[model] = adapted
        return adapted
    finally:
        _BUILDING.remove(model)
