"""One Responses attempt, accepted only after a consistent completed event."""
import json
from .protocols import ModelError, ProviderResponse, RemoteError
from .openai_responses import MAX_BYTES, NormalizeItems


class ResponsesAccumulator:
    def __init__(self, adapter):
        self.adapter = adapter
        self.items, self.added, self.fragments = {}, {}, {}
        self.fragment_ids = {}
        self.completed = None
        self.size = 0

    def Add(self, response):
        try:
            event = response.payload
            if not isinstance(event, dict) or self.completed is not None:
                raise ValueError
            self.size += len(json.dumps(event, ensure_ascii=False).encode("utf8"))
            if self.size > MAX_BYTES * 4:
                raise ValueError
            kind = event["type"]
            if kind in {"response.failed", "response.incomplete", "error"}:
                detail = event.get("response") or event
                error = detail.get("error") or detail
                if kind == "response.incomplete":
                    raise ModelError("TRUNCATED")
                raise RemoteError(remote_code=error.get("code"), siwc=True)
            if kind == "response.completed":
                result = event["response"]
                if result.get("status") != "completed" or result.get("error") or not isinstance(result.get("output"), list):
                    raise ValueError
                if self.items and sorted(self.items) != list(range(len(self.items))):
                    raise ValueError
                output = NormalizeItems(result["output"])
                done = NormalizeItems([self.items[i] for i in sorted(self.items)])
                if output:
                    for index, item in self.items.items():
                        if index >= len(output) or NormalizeItems([item])[0] != output[index]:
                            raise ValueError
                else:
                    output = done
                if any(index >= len(output) for index in self.added) or any(index >= len(output) for index, _ in self.fragments):
                    raise ValueError
                for index, item in self.added.items():
                    for key in ('type', 'id', 'call_id', 'name', 'namespace'):
                        if item.get(key) not in (None, '') and item[key] != output[index].get(key):
                            raise ValueError
                for index, identity in self.fragment_ids.items():
                    if index >= len(output) or output[index].get('id') != identity:
                        raise ValueError
                for (index, field), fragments in self.fragments.items():
                    item = output[index]
                    expected = item.get("arguments") if field == "arguments" else "".join(
                        p.get("text", "") for p in item.get("content", ()) if p.get("type") == "output_text")
                    if "".join(fragments) != expected:
                        raise ValueError
                self.completed = dict(result, output=output)
                return ""
            if kind in {"response.output_item.added", "response.output_item.done",
                        "response.output_text.delta", "response.function_call_arguments.delta"}:
                index = event["output_index"]
                if type(index) is not int or not 0 <= index < 128:
                    raise ValueError
                if kind == "response.output_item.added":
                    if index in self.added or index in self.items or not isinstance(event.get("item"), dict):
                        raise ValueError
                    self.added[index] = event["item"]
                elif kind == "response.output_item.done":
                    if index in self.items:
                        raise ValueError
                    NormalizeItems([event["item"]])
                    self.items[index] = event["item"]
                else:
                    if index in self.items or not isinstance(event.get("delta"), str):
                        raise ValueError
                    field = "arguments" if "function_call" in kind else "text"
                    identity = self.added.get(index, {}).get("id")
                    if identity and event.get("item_id") != identity:
                        raise ValueError
                    if event.get('item_id') is not None:
                        item_id = event['item_id']
                        if not isinstance(item_id, str) or not item_id or self.fragment_ids.get(index, item_id) != item_id:
                            raise ValueError
                        self.fragment_ids[index] = item_id
                    self.fragments.setdefault((index, field), []).append(event["delta"])
                    return event["delta"] if field == "text" else ""
            return ""
        except (KeyError, ValueError, TypeError, AttributeError, RecursionError):
            raise ModelError("PROTOCOL") from None

    def Finish(self):
        if self.completed is None:
            raise ModelError("TRUNCATED")
        return self.adapter.Decode(ProviderResponse(self.completed))

    def Snapshot(self):
        # Opaque items and arguments stay out of partial diagnostic snapshots.
        return {"content": "".join("".join(parts) for (_, field), parts in self.fragments.items() if field == "text"),
                "partial": self.completed is None}
