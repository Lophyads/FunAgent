"""Minimal custom tool example.

Register tools before constructing AgentCore; the catalog freezes at construction.
"""

from pydantic import BaseModel, Field

from agentcore import ToolCatalog, ToolContext, ToolSpec


class GreetArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def greet(args: GreetArgs, context: ToolContext) -> dict[str, str]:
    return {"message": f"Hello, {args.name}!", "session_id": context.session_id}


def build_catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    catalog.register(
        ToolSpec(
            tool_id="example.greet",
            implementation_version="1",
            name="greet",
            description="Greet a person by name.",
            args_model=GreetArgs,
            handler=greet,
            side_effects="none",
        )
    )
    return catalog
