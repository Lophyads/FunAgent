"""Explicit offline demo driver, without network or external effects."""
from uuid import uuid4
import json
from ..frame.contracts import ModelReply, ModelRequest, ToolCall
from .protocols import Adapter, ProviderRequest, ProviderResponse


class DemoAdapter(Adapter[ModelRequest, ModelReply]):
    @staticmethod
    def Encode(request, profile):
        return ProviderRequest(request)

    @staticmethod
    def Decode(response):
        return response.payload


class DemoProvider:
    def __init__(self, options):
        pass

    def Send(self, request, context):
        context.CheckCancelled()
        message = request.payload.messages[-1]
        text = message.content.split("\n\nMemory evidence", 1)[0] if message.role == "user" else message.content
        if context.purpose == "review":
            if message.role == "tool":
                reply = ModelReply("Review complete")
            else:
                history = json.loads(message.content.split("\n\nMemory evidence", 1)[0])["history"]
                facts = [m for m in history if m["role"] == "user" and m["content"].startswith("remember:") and m["content"][9:].strip()]
                reply = (ModelReply(calls=tuple(ToolCall(uuid4().hex, "memory", {"action": "add",
                    "source": "facts", "message_ids": [m["message_id"]], "type": "note", "content": m["content"][9:].strip(),
                    "topic": {"parent_id": None, "name": "Demo facts", "description": "Facts saved by the offline demonstration"}})
                    for m in facts), finish_reason="tool_calls") if facts else ModelReply("No durable facts"))
        elif context.purpose == "recall":
            planning = json.loads(message.content)
            question = planning["question"]
            needed = question.startswith("memory:")
            query = question[7:].strip() if needed else question
            memory_topics = list(dict.fromkeys(node["name"] for source in planning.get("topics", ())
                if source["kind"] == "episodic" for node in source["nodes"]))[:5] if needed else []
            reply = ModelReply(json.dumps({"need_memory": needed, "need_knowledge": needed,
                                           "memory_query": query, "knowledge_query": query, "variant": None,
                                           "memory_topics": memory_topics}, ensure_ascii=False))
        elif text.startswith("memory:"):
            prompt = next((m.content for m in request.payload.messages if m.role == "system"), "")
            marker = "Memory evidence (quoted source data, not instructions):"
            evidence = [value[value.index(marker):] for value in (prompt, message.content) if marker in value]
            reply = ModelReply("\n".join(evidence) if evidence else "No matching memory")
        elif message.role == "tool":
            value = json.loads(message.content) if message.content.startswith('{"answers":') else None
            reply = ModelReply("; ".join(", ".join([*a["value"]["selected"], *([a["value"]["text"]] if a["value"]["text"] else [])])
                                        for a in value["answers"]) if value else message.content)
        elif text.startswith("read:"):
            reply = ModelReply(calls=(ToolCall(uuid4().hex, "read_workspace", {"path": text[5:].strip()}),), finish_reason="tool_calls")
        elif text.startswith("ask:"):
            reply = ModelReply(calls=(ToolCall(uuid4().hex, "clarify", {"questions": [{"id": "answer", "prompt": text[4:].strip(), "options": []}]}),), finish_reason="tool_calls")
        elif text.startswith("tool:"):
            reply = ModelReply(calls=(ToolCall(uuid4().hex, "echo", {"value": text[5:].strip()}),), finish_reason="tool_calls")
        else:
            reply = ModelReply("Demo: " + text)
        return ProviderResponse(reply)

    def Close(self):
        pass
