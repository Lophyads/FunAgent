from ...frame.contracts import ToolDefinition
from ..tool_registrar import ToolEntry
from ..backends.handler import BackendHandler


def register_tools(registrar):
    specs = {
        "web_search": ({"query": {"type": "string", "minLength": 1, "maxLength": 500},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"], "web.search"),
        "web_extract": ({"urls": {"type": "array", "minItems": 1, "maxItems": 5,
                                  "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
                         "char_limit": {"type": "integer", "minimum": 500, "maximum": 100000}}, ["urls"], "web.extract")}
    for name, (properties, required, capability) in specs.items():
        description = "Bounded " + name + " with source artifacts"
        if name == "web_extract":
            description += (". Returns successful pages and per-URL errors; partial=true means some URLs failed."
                            " Failed pages are not verified sources.")
        registrar.Add(ToolEntry(name, ToolDefinition(name, description, {
            "type": "object", "required": required, "properties": properties, "additionalProperties": False}),
            lambda backend, operation=name: BackendHandler(backend, operation, read_only=True), toolsets=("web",),
            purposes=("main",), access={"network.read"}, capabilities={capability}))
