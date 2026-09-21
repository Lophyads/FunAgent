"""Minimal custom tool example.

Register tools before constructing AgentCore; the registry freezes at construction.
"""

from pydantic import BaseModel, Field

from agentcore import ToolContext, ToolRegistry, ToolSpec


class GreetArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)


async def greet(args: GreetArgs, context: ToolContext) -> dict[str, str]:
    return {"message": f"Hello, {args.name}!", "session_id": context.session_id}


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="greet",
            description="Greet a person by name.",
            args_model=GreetArgs,
            handler=greet,
        )
    )
    return registry
