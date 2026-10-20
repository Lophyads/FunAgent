"""Structured clarification through the shared human interaction broker."""
import json
from ...frame.contracts import ClarificationQuestion, ControlError, DomainError, ToolDefinition, ToolResult, thaw
from ...frame.interaction_mgr import InteractionMgr
from ..tool_registrar import ToolEntry


CLARIFY_DEFINITION = ToolDefinition("clarify", "Ask one to five questions with stable IDs. Each question allows multiple selections and free text; options may be empty.", {
    "type": "object", "required": ["questions"], "additionalProperties": False,
    "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 5,
        "items": {"type": "object", "additionalProperties": False, "required": ["id", "prompt", "options"],
            "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 128},
                           "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
                           "options": {"type": "array", "maxItems": 20, "uniqueItems": True,
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
