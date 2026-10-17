"""One model-facing memory tool."""
import hashlib
import json
import unicodedata
from dataclasses import replace

from ...frame.contracts import ControlError, DomainError, ToolDefinition, ToolResult
from ...memory.memory_mgr import MemoryMgr
from ...memory.contracts import MemoryDraft, MemoryListQuery, MemoryPatch, MemoryQuery, MemoryRef, TopicSelection, UNSET
from ...memory.topic_projection import TopicData
from ...memory.policy import Outcome
from ...memory.prompt import RecordData
from ...memory.contracts import MemoryFailure
from ...memory.failure import CODES, ErrorIssue, FailureData, IssueData
from ...memory.protocols import MemoryReader, MemoryWriter
from ..tool_registrar import ToolEntry
from ..contracts import ToolDecision


def definition(actions, *, review=False):
    common = {"source": {"type": "string", "minLength": 1},
              "action": {"enum": list(actions)}}
    evidence = {"message_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                                "items": {"type": "integer", "minimum": 1}}}
    content = {"content": {"type": "string", "minLength": 1, "maxLength": 65536},
               "type": {"type": "string", "minLength": 1}}
    ref = {"record_id": {"type": "string", "minLength": 1},
           "expected_version": {"type": "string", "minLength": 1}}
    metadata = {field: {"type": ["string", "null"], "minLength": 1, "maxLength": 512}
                for field in ("subject", "event_at", "origin")}
    filters = {field: {"type": "string", "minLength": 1, "maxLength": 512}
               for field in ("subject", "event_from", "event_to", "origin")}
    variants = []
    for action in actions:
        properties, required = {}, []
        if action == "list":
            properties = {**filters, "type": {"type": "string", "minLength": 1},
                          "cursor": {"type": "string"},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
        elif action == "search":
            properties = {**filters, "text": {"type": "string", "maxLength": 8000},
                          "types": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
            required = ["text"]
        else:
            properties = {**evidence, **({**content, **metadata} if action != "remove" else {}),
                          **(ref if action != "add" else {})}
            required = (["content"] if action != "remove" else []) + (["record_id", "expected_version"] if action != "add" else [])
            if review:
                required.append("message_ids")
            if action == "add":
                properties["topic"] = {"oneOf": [
                    {"type": "object", "additionalProperties": False, "required": ["topic_id"],
                     "properties": {"topic_id": {"type": "string", "minLength": 1, "maxLength": 128}}},
                    {"type": "object", "additionalProperties": False, "required": ["parent_id", "name", "description"],
                     "properties": {"parent_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
                                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                                    "description": {"type": "string", "minLength": 1, "maxLength": 240}}}]}
                required.append("topic")
        variants.append({"type": "object", "additionalProperties": False,
                         "required": ["action", "source", *required],
                         "properties": {**common, **properties, "action": {"const": action}}})
    description = ("List, search or add episodic facts with committed review evidence; replacing and removing are forbidden"
                   if review else "List, search or add memory; replacing and removing require one approval")
    return ToolDefinition("memory", description, {
        "type": "object", "properties": common, "oneOf": variants})


MEMORY_DEFINITION = definition(("list", "search", "add", "replace", "remove"))
REVIEW_DEFINITION = definition(("list", "search", "add"), review=True)


def _evidence(args, context):
    trusted = context.review_evidence if context.purpose == "review" else context.memory_evidence
    if context.purpose not in {"main", "review"} or not trusted or not context.session_id:
        raise DomainError("DENIED", "Memory writes require trusted conversation evidence")
    ids = args.get("message_ids")
    if ids is None:
        if context.purpose == "review":
            raise DomainError("DENIED", "Review writes require selected history evidence")
        return (trusted[-1],)
    selected = tuple(f"session:{context.session_id}/message:{key}" for key in sorted(ids))
    if not selected or not set(selected) <= set(trusted):
        raise DomainError("DENIED", "Evidence is outside the authorized history")
    return selected


class MemoryHandler:
    @staticmethod
    def Execute(args, context):
        try:
            if Outcome(args.get("action"), context.purpose) == "deny":
                raise DomainError("DENIED", "Memory action is forbidden for this purpose")
            reader: MemoryReader = MemoryMgr.GetInstance()
            action, source = args["action"], args["source"]
            filters = {field: args.get(field) for field in ("subject", "event_from", "event_to", "origin")}
            if action == "list":
                page = reader.List(MemoryListQuery(source, args.get("type"), args.get("limit", 50), args.get("cursor"), **filters), context)
                value = {"records": [RecordData(r) for r in page.items], "next_cursor": page.next_cursor}
            elif action == "search":
                result = reader.Search(MemoryQuery(args["text"], (source,), types=args.get("types", ()), limit=args.get("limit", 10), include_deleted=context.purpose == "review", **filters), context)
                if context.purpose == "review" and (result.warnings or result.issues or result.source_status.get(source) != "ok"):
                    raise MemoryFailure(result.issues[0] if result.issues else
                                        ErrorIssue(DomainError('MEMORY_SOURCE', ''), source_id=source, stage='search'))
                value = {"records": [RecordData(r) for r in result.records],
                         "source_status": dict(result.source_status), "warnings": result.warnings,
                         "issues": [IssueData(i) for i in result.issues]}
            elif action == "add":
                selection = TopicSelection.FromData(args.get("topic"))
                evidence = _evidence(args, context)
                content = " ".join(unicodedata.normalize("NFKC", args["content"]).split())
                category = args.get("type", "note")
                metadata = {field: args.get(field) for field in ("subject", "event_at", "origin")}
                key = hashlib.sha256(json.dumps([source, evidence, category, content, metadata], ensure_ascii=False).encode()).hexdigest()
                writer: MemoryWriter = MemoryMgr.GetInstance()
                receipt = writer.Write(MemoryDraft(source, content, "memory:" + key, evidence, category, **metadata), context, topic=selection)
                value = RecordData(receipt.record)
            else:
                evidence = _evidence(args, context)
                writer: MemoryWriter = MemoryMgr.GetInstance()
                ref = MemoryRef(source, args["record_id"])
                if action == "replace":
                    metadata = {field: args.get(field, UNSET) for field in ("subject", "event_at", "origin")}
                    value = RecordData(writer.Update(ref, MemoryPatch(args["content"], evidence, args.get("type"), **metadata),
                                                     args["expected_version"], context))
                elif action == "remove":
                    writer.Delete(ref, args["expected_version"], replace(context, memory_evidence=evidence))
                    value = {"deleted": True}
                else:
                    raise DomainError("DENIED", "Unknown memory action")
            if action in {"list", "search"}:
                snapshot, = reader.ReadTopics((source,), context)
                value["topics"] = [TopicData(topic) for topic in snapshot.topics.values()]
            return ToolResult("", "ok", json.dumps(value, ensure_ascii=False),
                              "known" if action in {"add", "replace", "remove"} else "none")
        except ControlError:
            raise
        except MemoryFailure as error:
            effect = error.issue.effect
            status = 'unknown' if effect == 'unknown' else 'denied' if error.code == 'DENIED' and effect == 'none' else 'error'
            return ToolResult('', status, json.dumps(FailureData(error), ensure_ascii=False), effect)
        except DomainError as error:
            code = error.code if error.code in CODES else "MEMORY_STORAGE"
            mutation = args.get('action') in {'add', 'replace', 'remove'}
            effect = 'known' if code == 'TOPIC_SYNC_FAILED' else 'unknown' if mutation and code in {
                'MEMORY_TIMEOUT', 'MEMORY_PROTOCOL', 'MEMORY_SOURCE', 'MEMORY_STORAGE'} else 'none'
            failure = MemoryFailure(ErrorIssue(DomainError(code, ''), effect=effect))
            status = 'unknown' if effect == 'unknown' else 'denied' if code == 'DENIED' else 'error'
            return ToolResult('', status, json.dumps(FailureData(failure), ensure_ascii=False), effect)


class MemoryPolicy:
    @staticmethod
    def Decide(call, context):
        return ToolDecision(Outcome(call.args.get("action"), context.purpose))


def register_tools(registrar):
    registrar.Add(ToolEntry("memory", MEMORY_DEFINITION, lambda backend: MemoryHandler,
                            toolsets=("memory",), purposes=("main", "review"), policy=MemoryPolicy,
                            purpose_definitions={"review": REVIEW_DEFINITION}))
