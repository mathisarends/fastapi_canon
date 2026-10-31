from dataclasses import replace

from fastapi_canon.integrations.sse.codegen.ir import (
    Contract,
    Declaration,
    EnumAlias,
    EnumMember,
    EnumValue,
    Field,
    LiteralType,
    Object,
    Operation,
    Reference,
    StringEnum,
    Type,
    Union,
)
from fastapi_canon.integrations.sse.codegen.names import identifier, python_identifier
from fastapi_canon.integrations.sse.codegen.openapi import ContractError


def with_enums(contract: Contract) -> Contract:
    declarations = {
        declaration.name: declaration.type for declaration in contract.declarations
    }
    operations: list[Operation] = []
    for operation in contract.operations:
        if isinstance(operation.event, Union) and operation.event.discriminator:
            name = identifier(operation.operation_id, upper=True) + "Event"
            if name in declarations:
                raise ContractError(f"Event alias name collision: {name}")
            declarations[name] = operation.event
            operation = replace(operation, event=Reference(name))
        operations.append(operation)
    candidates: list[tuple[str, set[tuple[str, str]], set[str]]] = []
    for name, type_ in sorted(declarations.items()):
        if not isinstance(type_, Union):
            continue
        if all(
            isinstance(member, LiteralType) and isinstance(member.value, str)
            for member in type_.members
        ):
            values = {
                member.value
                for member in type_.members
                if isinstance(member, LiteralType) and isinstance(member.value, str)
            }
            declarations[name] = StringEnum(_members(values))
        elif type_.discriminator:
            fields: set[tuple[str, str]] = set()
            values: set[str] = set()
            for member in type_.members:
                if not isinstance(member, Reference):
                    break
                model = declarations.get(member.name)
                if not isinstance(model, Object):
                    break
                field = next(
                    (
                        field
                        for field in model.fields
                        if field.name == type_.discriminator
                    ),
                    None,
                )
                literals = (
                    field.type.members
                    if field and isinstance(field.type, Union)
                    else (field.type,)
                    if field
                    else ()
                )
                if not literals or not all(
                    isinstance(part, LiteralType) and isinstance(part.value, str)
                    for part in literals
                ):
                    break
                assert field is not None
                fields.add((member.name, field.name))
                values.update(
                    part.value
                    for part in literals
                    if isinstance(part, LiteralType) and isinstance(part.value, str)
                )
            else:
                candidates.append(
                    (name + identifier(type_.discriminator, upper=True), fields, values)
                )
    # Overlapping unions share one enum class, keeping Python narrowing consistent.
    while candidates:
        name, fields, values = candidates.pop(0)
        aliases = {name}
        changed = True
        while changed:
            changed = False
            for candidate in candidates[:]:
                other_name, other_fields, other_values = candidate
                if fields & other_fields:
                    candidates.remove(candidate)
                    fields |= other_fields
                    values |= other_values
                    aliases.add(other_name)
                    changed = True
        canonical = min(aliases)
        enum = StringEnum(_members(values))
        for alias in sorted(aliases):
            if alias in declarations and declarations[alias] != enum:
                raise ContractError(f"Enum type name collision: {alias}")
            declarations[alias] = enum if alias == canonical else EnumAlias(canonical)
        by_value = {member.value: member.name for member in enum.members}
        for model_name, field_name in fields:
            model = declarations[model_name]
            assert isinstance(model, Object)
            declarations[model_name] = replace(
                model,
                fields=tuple(
                    replace(field, type=_enum_literals(field.type, canonical, by_value))
                    if field.name == field_name
                    else field
                    for field in model.fields
                ),
            )
    for name, type_ in list(declarations.items()):
        if not isinstance(type_, Object):
            continue
        model_fields: list[Field] = []
        for field in type_.fields:
            literals = (
                field.type.members if isinstance(field.type, Union) else (field.type,)
            )
            values = {
                part.value
                for part in literals
                if isinstance(part, LiteralType) and isinstance(part.value, str)
            }
            # A lone constant field is clearer as a literal. Discriminators have
            # already been assigned their shared enum above; named enums remain.
            if len(values) > 1 and len(values) == len(literals):
                enum_name = name + identifier(field.name, upper=True)
                enum = StringEnum(_members(values))
                if enum_name in declarations and declarations[enum_name] != enum:
                    raise ContractError(f"Enum type name collision: {enum_name}")
                declarations[enum_name] = enum
                field = replace(
                    field,
                    type=Reference(enum_name),
                )
            model_fields.append(field)
        declarations[name] = replace(type_, fields=tuple(model_fields))
    return replace(
        contract,
        operations=tuple(operations),
        declarations=tuple(
            Declaration(name, type_) for name, type_ in sorted(declarations.items())
        ),
    )


def _members(values: set[str]) -> tuple[EnumMember, ...]:
    result = tuple(
        EnumMember(python_identifier(value).upper(), value) for value in sorted(values)
    )
    if len({member.name for member in result}) != len(result):
        raise ContractError(f"Enum member names collide: {sorted(values)}")
    return result


def _enum_literals(type_: Type, enum: str, members: dict[str, str]) -> Type:
    if isinstance(type_, LiteralType) and isinstance(type_.value, str):
        return EnumValue(enum, members[type_.value])
    assert isinstance(type_, Union)
    return Union(tuple(_enum_literals(part, enum, members) for part in type_.members))
