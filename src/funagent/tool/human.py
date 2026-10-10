"""Static human clarification tool."""
from ..contracts import ControlError, DomainError, ToolResult
from ..interaction import InteractionMgr


class AskUserHandler:
    @staticmethod
    def Execute(args, context):
        if context.purpose != "main" or not context.task_id or not context.run_id:
            return ToolResult("", "denied", "This run cannot request human input")
        try:
            answer = InteractionMgr.GetInstance().Wait("ask_user", args["prompt"], context)
            return ToolResult("", "ok", answer)
        except ControlError:
            return ToolResult("", "cancelled", "Human wait interrupted")
        except DomainError as error:
            return ToolResult("", "denied" if error.code == "INTERACTION_DENIED" else "error", str(error))
