import keyword
import re

from fastapi_canon.codegen.ir import GenerationError

_RESERVED = {
    "constructor",
    "prototype",
    "__proto__",
    "then",
    "close",
    "aclose",
    "closed",
    "runtime",
}


def words(value: str) -> list[str]:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.findall(r"[A-Za-z0-9]+", value)


def camel(value: str) -> str:
    parts = words(value)
    if not parts:
        raise GenerationError(f"Invalid identifier {value!r}")
    return parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])


def pascal(value: str) -> str:
    name = camel(value)
    return name[:1].upper() + name[1:]


def snake(value: str) -> str:
    result = "_".join(p.lower() for p in words(value))
    if result and result[0].isdigit():
        result = "value_" + result
    if keyword.iskeyword(result):
        result += "_"
    return result


def public_name(value: str) -> tuple[str, ...]:
    parts = value.split(".")
    if any(
        not part or not part.isidentifier() or part.startswith("_") for part in parts
    ):
        raise GenerationError(
            f"Operation name must be a dotted public identifier: {value!r}"
        )
    for part in parts:
        if camel(part) in _RESERVED or snake(part) in _RESERVED:
            raise GenerationError(f"Reserved operation member {part!r} in {value!r}")
    return tuple(parts)


def model_field(value: str) -> str:
    from pydantic import BaseModel

    name = snake(value)
    return name + "_" if hasattr(BaseModel, name) else name
