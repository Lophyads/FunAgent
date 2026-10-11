"""Local JSON Schema validation, shared by config and dispatch."""
from collections.abc import Mapping
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from .contracts import DomainError, thaw


def ToolValidator(schema):
    schema = thaw(schema)
    def local(value):
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise DomainError("CONFIG_INVALID", "Tool schema references must be local")
                local(item)
        elif isinstance(value, list):
            for item in value:
                local(item)
    try:
        local(schema)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise DomainError("CONFIG_INVALID", "Tool parameters must be an object schema")
        return Draft202012Validator(schema)
    except SchemaError:
        raise DomainError("CONFIG_INVALID", "Invalid tool parameter schema") from None
