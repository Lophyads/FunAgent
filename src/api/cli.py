"""Commands use application APIs; execution stays on the main worker."""
from threading import Event
import json
import sys
import shlex
from . import DiagnosticsMgr

from ..frame.conf import ConfMgr
from ..frame.contracts import DomainError, SessionCreate, TurnRequest, PageQuery, HistoryQuery
from ..frame.session import SessionMgr
from ..task import TaskMgr
from ..frame.review import ReviewScheduler
from ..frame.interaction import InteractionMgr
from ..frame.clarification import QuestionData
from ..memory import MemoryOperationsMgr, MemoryQuery, MemoryListQuery, MemoryDraft, MemoryPatch, MemoryRef
from . import diagnostics
from .export import Export


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
        for question in request.questions:
            print(json.dumps(QuestionData(question), ensure_ascii=False), file=output, flush=True)

    @staticmethod
    def _resolve(request, text):
        answer = text
        if request.kind == "approval":
            value = text.strip().lower()
            if value not in {"yes", "y", "no", "n"}:
                raise DomainError("INVALID_REQUEST", "Approval answer must be yes or no")
            answer = value in {"yes", "y"}
        elif request.kind == "clarify":
            try:
                answer = json.loads(text)
            except (TypeError, ValueError):
                raise DomainError("INVALID_REQUEST", "Clarification answer must be structured JSON") from None
        InteractionMgr.GetInstance().Resolve(request.interaction_id, answer,
                                            task_id=request.task_id, run_id=request.run_id)

    @staticmethod
    def _read_clarification(request, read_answer):
        answers = []
        for question in request.questions:
            text = read_answer(f"{question.question_id} ({question.kind})> ")
            if text.strip() == "/cancel":
                return text
            if question.kind == "multiple":
                try:
                    value = json.loads(text)
                except ValueError:
                    raise DomainError("INVALID_REQUEST", "Multiple choice answer must be a JSON array") from None
            else:
                value = text
            answers.append({"question_id": question.question_id, "value": value})
        return json.dumps({"answers": answers}, ensure_ascii=False)

    @staticmethod
    def _memory(line, output):
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            raise DomainError("INVALID_REQUEST", "Use memory search/list/write/update/delete/confirm/export")
        _, operation, rest = parts
        manager = MemoryOperationsMgr.GetInstance()
        if operation == "export":
            try:
                fields = [value.strip('"') for value in shlex.split(rest, posix=False)]
            except ValueError:
                raise DomainError("INVALID_REQUEST", "Invalid export command") from None
            if len(fields) != 2 or not all(fields):
                raise DomainError("INVALID_REQUEST", "Use memory export <source> <new-file.jsonl>")
            value = Export(fields[0], fields[1])
        elif operation == "search":
            fields = rest.split(maxsplit=1)
            if len(fields) != 2:
                raise DomainError("INVALID_REQUEST", "Use memory search <source|*> <text>")
            source, query = fields
            value = manager.Search(MemoryQuery(query, () if source == "*" else (source,)))
        elif operation == "list":
            fields = rest.split()
            if len(fields) not in {1, 2}:
                raise DomainError("INVALID_REQUEST", "Use memory list <source> [type]")
            # Stream all pages through the same stable Store cursor.
            records, cursor = [], None
            while True:
                page = manager.List(MemoryListQuery(fields[0], fields[1] if len(fields) == 2 else None, cursor=cursor))
                records.extend(page["records"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            value = {"records": records}
        elif operation == "write":
            fields = rest.split(maxsplit=2)
            if len(fields) != 3:
                raise DomainError("INVALID_REQUEST", "Use memory write <source> <type> <content>")
            value = manager.Write(MemoryDraft(fields[0], fields[2], "", (), fields[1]))
        elif operation == "update":
            fields = rest.split(maxsplit=3)
            if len(fields) != 4:
                raise DomainError("INVALID_REQUEST", "Use memory update <source> <record_id> <version> <content>")
            value = manager.Request("replace", MemoryRef(fields[0], fields[1]), fields[2], MemoryPatch(fields[3], ()))
        elif operation == "delete":
            fields = rest.split()
            if len(fields) != 3:
                raise DomainError("INVALID_REQUEST", "Use memory delete <source> <record_id> <version>")
            value = manager.Request("remove", MemoryRef(fields[0], fields[1]), fields[2])
        elif operation == "confirm":
            fields = rest.split()
            if len(fields) != 3 or fields[2].lower() not in {"yes", "no"}:
                raise DomainError("INVALID_REQUEST", "Use memory confirm <challenge_id> <digest> <yes|no>")
            value = manager.Confirm(fields[0], fields[1], fields[2].lower() == "yes")
        else:
            raise DomainError("INVALID_REQUEST", "Unknown memory command")
        print(json.dumps(value, ensure_ascii=False), file=output)

    @staticmethod
    def _logs(line, output):
        try:
            parts = shlex.split(line)
        except ValueError:
            raise DomainError("INVALID_REQUEST", "Invalid log command") from None
        if len(parts) < 2 or parts[1] not in {"tail", "follow"}:
            raise DomainError("INVALID_REQUEST", "Use logs tail/follow [--lines N] [--filter text] [--source normal|error] [--seconds N]")
        values = {}
        rest = parts[2:]
        if len(rest) % 2:
            raise DomainError("INVALID_REQUEST", "Log options require values")
        for key, value in zip(rest[::2], rest[1::2]):
            if key not in {"--lines", "--filter", "--source", "--seconds"} or key in values:
                raise DomainError("INVALID_REQUEST", "Invalid log option")
            values[key] = value
        try:
            limit = int(values.get("--lines", "50"))
            seconds = float(values.get("--seconds", "10" if parts[1] == "follow" else "0"))
        except ValueError:
            raise DomainError("INVALID_REQUEST", "Invalid log bounds") from None
        if parts[1] == "tail" and seconds != 0:
            raise DomainError("INVALID_REQUEST", "Tail does not follow")
        for line in DiagnosticsMgr.GetInstance().Logs(source=values.get("--source", "normal"), limit=limit,
                 contains=values.get("--filter", ""), follow_seconds=seconds):
            print(line, file=output, flush=True)

    @staticmethod
    def Execute(line, output=None, read_answer=None):
        output = output or sys.stdout
        read_answer = read_answer or input
        if line.strip().split(maxsplit=1)[:1] == ["memory"]:
            return CLIController._memory(line, output)
        if line.strip().split(maxsplit=1)[:1] == ["logs"]:
            return CLIController._logs(line, output)
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
        if command == "tools" and parts[1:] == ["doctor"]:
            tools = diagnostics.Read("tools")["tools"]
            connections = diagnostics.Read("connections")["connections"]
            healthy = all(item["available"] for item in connections if item["selected"])
            healthy = healthy and all(item["selected"] for item in tools if item["requested"])
            print(json.dumps({"healthy": healthy, "tools": tools, "connections": connections}, ensure_ascii=False), file=output)
        elif command in diagnostics.KINDS and len(parts) == 1:
            print(json.dumps(diagnostics.Read(command), ensure_ascii=False), file=output)
        elif command == "help" and len(parts) == 1:
            print("new [title] | sessions | submit <session_id> <text> | get <task_id> | wait <task_id> | cancel <task_id> | history <session_id> | pending <task_id> | answer <task_id> <interaction_id> <text> | review <session_id> | memory search/list/write/update/delete/confirm/export | tools doctor | connections/models/config/status | logs tail/follow | quit", file=output)
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
            text, key = parts[2], None
            if text.startswith("--request-key "):
                fields = text.split(maxsplit=2)
                if len(fields) != 3:
                    raise DomainError("INVALID_REQUEST", "Use submit <session_id> --request-key <key> <text>")
                _, key, text = fields
            print(TaskMgr.GetInstance().Submit(TurnRequest(parts[1], text, key)), file=output)
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
                            answer = (CLIController._read_clarification(request, read_answer) if request.kind == "clarify"
                                      else read_answer("answer (yes/no for approval; /cancel to cancel)> "))
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
