"""Commands use application APIs; execution stays on the main worker."""
from threading import Event
import sys

from ..conf import ConfMgr
from ..contracts import DomainError, SessionCreate, TurnRequest, PageQuery, HistoryQuery
from ..session import SessionMgr
from ..task import TaskMgr
from ..interaction import InteractionMgr


class CLIController:
    @staticmethod
    def _show(task, output):
        print(f"{task.task_id} {task.status}", file=output)
        if task.result is not None:
            if task.result.final is not None:
                print(task.result.final, file=output)
            elif task.result.reason:
                print(task.result.reason, file=output)

    @staticmethod
    def _show_interaction(request, output):
        print(f"pending {request.interaction_id} {request.kind}: {request.prompt}", file=output, flush=True)

    @staticmethod
    def _resolve(request, text):
        answer = text
        if request.kind == "approval":
            value = text.strip().lower()
            if value not in {"yes", "y", "no", "n"}:
                raise DomainError("INVALID_REQUEST", "Approval answer must be yes or no")
            answer = value in {"yes", "y"}
        InteractionMgr.GetInstance().Resolve(request.interaction_id, answer,
                                            task_id=request.task_id, run_id=request.run_id)

    @staticmethod
    def Execute(line, output=None, read_answer=None):
        output = output or sys.stdout
        read_answer = read_answer or input
        answer_parts = line.strip().split(maxsplit=3)
        if answer_parts and answer_parts[0] == "answer":
            if len(answer_parts) != 4:
                raise DomainError("INVALID_REQUEST", "Use answer <task_id> <interaction_id> <text>")
            _, task_id, identifier, text = answer_parts
            TaskMgr.GetInstance().Get(task_id)
            request = next((r for r in InteractionMgr.GetInstance().Pending(task_id) if r.interaction_id == identifier), None)
            if request is None:
                raise DomainError("INTERACTION_STALE", "Human request is no longer pending for this task")
            CLIController._resolve(request, text)
            print(f"answered {identifier}", file=output)
            return
        parts = line.strip().split(maxsplit=2)
        if not parts:
            return
        command = parts[0]
        if command == "help" and len(parts) == 1:
            print("new [title] | sessions | submit <session_id> <text> | get <task_id> | wait <task_id> | cancel <task_id> | history <session_id> | pending <task_id> | answer <task_id> <interaction_id> <text> | quit", file=output)
        elif command == "new":
            title = line.strip().partition(" ")[2].strip() or "New session"
            workspace = ConfMgr.GetInstance().Serv().Options()["workspace"]
            print(SessionMgr.Create(SessionCreate(title, workspace)).session_id, file=output)
        elif command == "sessions" and len(parts) == 1:
            cursor = None
            while True:
                page = SessionMgr.List(PageQuery(cursor=cursor))
                for session in page.items:
                    print(f"{session.session_id} {session.title}", file=output)
                cursor = page.next_cursor
                if cursor is None:
                    break
        elif command == "submit" and len(parts) == 3:
            print(TaskMgr.GetInstance().Submit(TurnRequest(parts[1], parts[2])), file=output)
        elif command in {"get", "wait", "cancel", "history", "pending"} and len(parts) == 2:
            identifier = parts[1]
            if command == "pending":
                TaskMgr.GetInstance().Get(identifier)
                for request in InteractionMgr.GetInstance().Pending(identifier):
                    CLIController._show_interaction(request, output)
            elif command == "history":
                cursor = None
                while True:
                    page = SessionMgr.ReadHistory(identifier, HistoryQuery(cursor=cursor))
                    for message in page.items:
                        print(f"{message.role}: {message.content}", file=output)
                        for call in message.calls:
                            print(f"  call {call.call_id} {call.name}", file=output)
                        if message.call_id:
                            print(f"  result {message.call_id} {message.status} effect={message.effect}", file=output)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
            elif command == "cancel":
                result = TaskMgr.GetInstance().Cancel(identifier)
                print(f"{result.task_id} {result.status} requested={result.requested}", file=output)
            else:
                task = TaskMgr.GetInstance().Get(identifier)
                while command == "wait" and task.status in {"queued", "running"}:
                    for request in InteractionMgr.GetInstance().Pending(identifier):
                        CLIController._show_interaction(request, output)
                        try:
                            answer = read_answer("answer (yes/no for approval; /cancel to cancel)> ")
                            if answer.strip() == "/cancel":
                                TaskMgr.GetInstance().Cancel(identifier)
                            else:
                                CLIController._resolve(request, answer)
                        except EOFError:
                            TaskMgr.GetInstance().Cancel(identifier)
                        except DomainError as error:
                            print(f"{error.code}: {error}", file=output, flush=True)
                    Event().wait(0.05)
                    task = TaskMgr.GetInstance().Get(identifier)
                CLIController._show(task, output)
        else:
            raise DomainError("INVALID_REQUEST", "Invalid CLI command; type help")

    @staticmethod
    def Run(read_command=None, output=None):
        read_command = read_command or input
        output = output or sys.stdout
        print("FunAgent CLI. Type help for commands.", file=output)
        while True:
            try:
                line = read_command("funagent> ")
            except EOFError:
                return 0
            if line.strip() in {"quit", "exit"}:
                return 0
            try:
                CLIController.Execute(line, output, read_answer=read_command)
            except DomainError as error:
                print(f"{error.code}: {error}", file=output)
