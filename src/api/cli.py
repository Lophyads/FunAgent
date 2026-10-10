"""Commands use application APIs; execution stays on the main worker."""
from threading import Event
from uuid import uuid4
import json
import sys

from ..conf import ConfMgr
from ..contracts import DomainError, SessionCreate, TurnRequest, PageQuery, HistoryQuery, RunContext
from ..session import SessionMgr
from ..task import TaskMgr
from ..review import ReviewScheduler
from ..interaction import InteractionMgr
from ..memory import MemoryMgr, MemoryQuery, MemoryListQuery, MemoryDraft, MemoryPatch, MemoryRef
from ..memory.retrieval import RecordData


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
    def _memory(line, output):
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            raise DomainError("INVALID_REQUEST", "Use memory search/list/write/update/delete with a source")
        _, operation, rest = parts
        sources = ConfMgr.GetInstance().Memory().Sources()
        # CLI is a local administrative entry; grants come from trusted config.
        access = frozenset(token for name, settings in sources.items() for token in (
            settings.get("read_access", f"memory.{name}.read"), settings.get("write_access", f"memory.{name}.write")))
        context = RunContext(run_id=uuid4().hex, purpose="management", access=access)
        manager = MemoryMgr.GetInstance()
        if operation == "search":
            fields = rest.split(maxsplit=1)
            if len(fields) != 2:
                raise DomainError("INVALID_REQUEST", "Use memory search <source|*> <text>")
            source, query = fields
            result = manager.Search(MemoryQuery(query, () if source == "*" else (source,)), context)
            value = {"records": [RecordData(r) for r in result.records], "source_status": dict(result.source_status), "warnings": result.warnings}
        elif operation == "list":
            fields = rest.split()
            if len(fields) not in {1, 2}:
                raise DomainError("INVALID_REQUEST", "Use memory list <source> [type]")
            # Stream all pages through the same stable Store cursor.
            records, cursor = [], None
            while True:
                page = manager.List(MemoryListQuery(fields[0], fields[1] if len(fields) == 2 else None, cursor=cursor), context)
                records.extend(RecordData(r) for r in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            value = {"records": records}
        elif operation == "write":
            fields = rest.split(maxsplit=2)
            if len(fields) != 3:
                raise DomainError("INVALID_REQUEST", "Use memory write <source> <type> <content>")
            action = "cli:" + context.run_id
            value = RecordData(manager.Write(MemoryDraft(fields[0], fields[2], action, (action,), fields[1]), context))
        elif operation == "update":
            fields = rest.split(maxsplit=3)
            if len(fields) != 4:
                raise DomainError("INVALID_REQUEST", "Use memory update <source> <record_id> <version> <content>")
            value = RecordData(manager.Update(MemoryRef(fields[0], fields[1]), MemoryPatch(fields[3], ("cli:" + context.run_id,)), fields[2], context))
        elif operation == "delete":
            fields = rest.split()
            if len(fields) != 3:
                raise DomainError("INVALID_REQUEST", "Use memory delete <source> <record_id> <version>")
            manager.Delete(MemoryRef(fields[0], fields[1]), fields[2], context)
            value = {"deleted": True}
        else:
            raise DomainError("INVALID_REQUEST", "Unknown memory command")
        print(json.dumps(value, ensure_ascii=False), file=output)

    @staticmethod
    def Execute(line, output=None, read_answer=None):
        output = output or sys.stdout
        read_answer = read_answer or input
        if line.strip().split(maxsplit=1)[:1] == ["memory"]:
            return CLIController._memory(line, output)
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
            print("new [title] | sessions | submit <session_id> <text> | get <task_id> | wait <task_id> | cancel <task_id> | history <session_id> | pending <task_id> | answer <task_id> <interaction_id> <text> | review <session_id> | memory search/list/write/update/delete | quit", file=output)
        elif command == "new":
            title = line.strip().partition(" ")[2].strip() or "New session"
            workspace = ConfMgr.GetInstance().Serv().Options()["workspace"]
            print(SessionMgr.Create(SessionCreate(title, workspace)).session_id, file=output)
        elif command == "review" and len(parts) == 2:
            SessionMgr.Load(parts[1])
            print(json.dumps(dict(ReviewScheduler.GetInstance().GetStatus(parts[1])), ensure_ascii=False), file=output)
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
