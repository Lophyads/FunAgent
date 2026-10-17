"""Fixed JSON query endpoint for a read-only external semantic source."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ..frame.contracts import ControlError, DomainError
from .contracts import MemoryQueryBatch
from .failure import ReadIssues


class HTTPMemoryQuery:
    def __init__(self, settings, *, transport=None):
        self._endpoint = settings["endpoint"]
        parsed = urllib.parse.urlsplit(self._endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise DomainError("CONFIG_INVALID", "Invalid memory query endpoint")
        self._timeout = settings.get("timeout", 2)
        self._transport = transport or urllib.request.urlopen
        self._token = None
        if "credential_env" in settings:
            self._token = os.environ.get(settings["credential_env"])
            if not self._token:
                raise DomainError("CONFIG_CREDENTIAL", "Memory query credential is missing")

    def __call__(self, query, context):
        context.CheckCancelled()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(self._endpoint, data=json.dumps({
            "text": query.text, "types": query.types, "limit": query.limit,
        }, ensure_ascii=False).encode("utf8"), headers=headers, method="POST")
        try:
            with self._transport(request, timeout=min(self._timeout, context.Remaining())) as response:
                body = response.read(1048577)
            context.CheckCancelled()
            if len(body) > 1048576:
                raise DomainError("MEMORY_PROTOCOL", "Memory response is too large")
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise DomainError("MEMORY_PROTOCOL", "Invalid memory query response")
            return MemoryQueryBatch(rows=payload["records"], status=payload.get("status", "ok"),
                                    issues=ReadIssues(payload.get("issues", [])))
        except (ControlError, DomainError):
            raise
        except (UnicodeError, ValueError):
            raise DomainError("MEMORY_PROTOCOL", "Invalid memory query response") from None
        except Exception:
            context.CheckCancelled()
            raise DomainError("MEMORY_SOURCE", "External memory query failed") from None
