"""Evaluation HTTP validation and presentation; the worker owns all IO."""
import re

from ..eval.eval_mgr import EvalMgr
from ..frame.contracts import DomainError
from .request_validation import Fields, Text


def Handle(method, route, query, body):
    if query:
        raise DomainError("INVALID_REQUEST", "Unexpected query")
    service = EvalMgr.GetInstance()
    if route == "/eval/options" and method == "GET":
        return 200, service.Options()
    if route == "/eval/runs":
        if method == "GET":
            return 200, service.List()
        if method == "POST":
            Fields(body, {"question", "source_ids", "top_k", "request_key"}, {"question", "source_ids"})
            key = body.get("request_key")
            if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key)):
                raise DomainError("INVALID_REQUEST", "Invalid evaluation request key")
            run_id = service.Submit(Text(body["question"], 8000), body["source_ids"], body.get("top_k", 5), key)
            return 202, {"run_id": run_id}
    match = re.fullmatch(r"/eval/runs/([a-f0-9]{32})", route)
    if match and method == "GET":
        return 200, service.Get(match[1])
    if route in {"/eval/options", "/eval/runs"} or match:
        raise DomainError("METHOD_NOT_ALLOWED", "Invalid evaluation method")
    raise DomainError("NOT_FOUND", "Evaluation route not found")
