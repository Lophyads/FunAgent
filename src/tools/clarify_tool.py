"""Model-visible batch clarification using the Agent-owned interaction broker."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from agentcore.interfaces import ToolContext
from agentcore.interaction import Question
from .registry import ToolEntry, ToolRegistry


class ClarifyQuestion(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    question: str = Field(min_length=1, max_length=2000)
    choices: list[str] = Field(default_factory=list, max_length=4)
    multi_select: bool = False

    @model_validator(mode="after")
    def validate_choices(self) -> "ClarifyQuestion":
        if self.multi_select and not self.choices:
            raise ValueError("multi_select requires choices")
        if any(not choice.strip() for choice in self.choices):
            raise ValueError("choices must not be blank")
        return self


class ClarifyArgs(BaseModel):
    questions: list[ClarifyQuestion] = Field(min_length=1, max_length=5)


def _normalize_answer(question: ClarifyQuestion, value: Any) -> Any:
    if question.multi_select:
        if not isinstance(value, (list, tuple)):
            return []
        return [
            str(item).strip().removesuffix(" (Recommended)")[:1000]
            for item in value[:4] if str(item).strip()
        ]
    if value is None:
        return ""
    return str(value).strip().removesuffix(" (Recommended)")[:2000]


def clarify(args: ClarifyArgs, context: ToolContext) -> dict[str, Any]:
    questions = tuple(
        Question(qid=f"q{index}", id=item.id, question=item.question,
                 choices=tuple(item.choices), multi_select=item.multi_select)
        for index, item in enumerate(args.questions)
    )
    response = context.interactions.ask(questions)
    answers = response.answers if response is not None else {}
    result = {"responses": [
        {"id": item.id or f"q{index}", "question": item.question,
         "choices_offered": item.choices or None,
         "user_response": _normalize_answer(item, answers.get(f"q{index}"))}
        for index, item in enumerate(args.questions)
    ]}
    if response is None:
        result["timed_out"] = True
    return result


def register_tools(registry: ToolRegistry) -> None:
    registry.register(ToolEntry(
        tool_id="builtin.clarify", implementation_version="1",
        name="clarify",
        description=(
            "Ask the user 1–5 structured questions. Each question may have up to "
            "four choices, with the first recommended; free-text answers are allowed."
        ),
        args_model=ClarifyArgs, handler=clarify, toolset="default",
        required_capabilities=frozenset({"clarify"}), side_effects="none", interactive_wait=True,
    ))


__all__ = ["ClarifyArgs", "ClarifyQuestion", "register_tools"]
