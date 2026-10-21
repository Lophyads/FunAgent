"""Structured clarification through the shared human interaction broker."""
import json
from ...frame.contracts import ClarificationQuestion, ControlError, DomainError, ToolDefinition, ToolResult, thaw
from ...frame.interaction_mgr import InteractionMgr
from ..tool_registrar import ToolEntry


CLARIFY_DESCRIPTION = (
    "Ask the user for missing information, decisions, or feedback that "
    "materially affect the result and cannot be reliably inferred from "
    "the conversation or obtained through available tools. When such "
    "information is needed, call clarify before proceeding with work "
    "that depends on the answer. Use lookup tools for retrievable facts; "
    "use clarify for user preferences, constraints, and choices. "
    "Prefer deciding minor details yourself when a reasonable default exists. "
    "Ask one to five questions per call. Batch independent questions "
    "together; ask dependent questions after their prerequisites are answered. "
    "Give each question a unique, stable id. Put the question text in prompt "
    "and selectable answers only in options, rather than listing them in prompt. "
    "Every question supports multiple selections and free text. "
    "Use options=[] for an open-ended question."
)


CLARIFY_DEFINITION = ToolDefinition("clarify", CLARIFY_DESCRIPTION, {
    "type": "object", "required": ["questions"], "additionalProperties": False,
    "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 5,
        "description": "One to five independent questions in one call; use a one-entry array for a single question.",
        "items": {"type": "object", "additionalProperties": False, "required": ["id", "prompt", "options"],
            "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 128,
                                  "description": "A stable identifier, unique within this call, used to match the answer to this question."},
                           "prompt": {"type": "string", "minLength": 1, "maxLength": 4000,
                                      "description": "A complete, self-contained question. Put predefined answers in options instead of listing them here."},
                           "options": {"type": "array", "maxItems": 20, "uniqueItems": True,
                                       "description": "Predefined answers supporting multiple selections and free text; use [] when there are no predefined answers.",
                                       "items": {"type": "string", "minLength": 1, "maxLength": 4000}}}}}}})


class ClarifyHandler:
    @staticmethod
    def Execute(args, context):
        if context.purpose != "main" or not context.task_id or not context.run_id:
            return ToolResult("", "denied", "This run cannot request human input")
        try:
            questions = tuple(ClarificationQuestion(q["id"], q["prompt"], q["options"])
                              for q in args["questions"])
            answer = InteractionMgr.GetInstance().Wait("clarify", "\n".join(q.prompt for q in questions),
                                                       context, questions=questions)
            return ToolResult("", "ok", json.dumps(thaw(answer), ensure_ascii=False))
        except ControlError as error:
            return ToolResult("", "cancelled", json.dumps({"code": error.code}))
        except DomainError as error:
            status = "denied" if error.code == "INTERACTION_DENIED" else "invalid" if error.code == "INVALID_REQUEST" else "error"
            return ToolResult("", status, json.dumps({"code": error.code}))


def register_tools(registrar):
    registrar.Add(ToolEntry("clarify", CLARIFY_DEFINITION, lambda backend: ClarifyHandler,
                            toolsets=("interaction",), purposes=("main",)))
