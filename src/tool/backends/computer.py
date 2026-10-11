from .managed import ManagedBackend
from ..contracts import BackendResult, Availability, ComputerReadOnly
from .network import Fetch, JSONPost
from ...frame.contracts import ControlError, DomainError, RunContext, thaw
from dataclasses import replace
import json
import os
import time


class ComputerBackend(ManagedBackend):
    def __init__(self, settings):
        super().__init__(settings)
        self._endpoint = settings["endpoint"].rstrip("/")
        self._timeout = settings.get("timeout", 10)
        self._private = settings.get("allow_private", False)
        self._headers = {}
        if settings.get("credential_env"):
            token = os.environ.get(settings["credential_env"])
            if not token:
                raise DomainError("CONFIG_CREDENTIAL", "Computer backend credential is missing")
            self._headers["Authorization"] = "Bearer " + token
        context = RunContext(deadline=time.monotonic() + self._timeout)
        _, mime, body = Fetch(self._endpoint + "/capabilities", context, timeout=self._timeout,
                              allow_private=self._private, max_bytes=16000,
                              headers=self._headers, follow_redirects=False)
        try:
            data = json.loads(body)
            if (mime.split(";")[0] != "application/json" or type(data.get("available")) is not bool
                    or not isinstance(data.get("capabilities"), list)
                    or any(not isinstance(c, str) or not c for c in data["capabilities"])):
                raise ValueError
            self._availability = Availability(data["available"], data["capabilities"])
            self._actions = frozenset(data.get("actions", ()))
        except (ValueError, AttributeError, TypeError):
            raise DomainError("BACKEND_PROTOCOL", "Invalid computer backend capabilities") from None
    def Probe(self):
        return Availability(self._availability.available and not self._closing, self._availability.capabilities)
    def Execute(self, operation, args, context):
        context.CheckCancelled()
        if operation != "computer_use":
            return BackendResult("invalid", '{"code":"invalid_operation"}')
        action = args["action"]
        readonly = ComputerReadOnly(args)
        effect = "none" if readonly else "unknown"
        if not self.Probe().available or "computer.ax" not in self.Probe().capabilities or self._actions and action not in self._actions:
            return BackendResult("denied", '{"code":"COMPUTER_UNAVAILABLE"}')
        if args.get("bring_to_front", False) and args.get("delivery_mode", "background") != "foreground":
            return BackendResult("invalid", '{"code":"invalid_delivery_mode"}')
        data = {"mode": "ax", "delivery_mode": "background", **thaw(args)}
        child = replace(context, deadline=min(context.deadline or float("inf"), time.monotonic() + self._timeout))
        try:
            with self.Borrow(context):
                value = JSONPost(self._endpoint + "/execute", {"session_id": context.session_id,
                    "task_id": context.task_id, "run_id": context.run_id, "arguments": data}, child,
                    timeout=self._timeout, headers=self._headers, allow_private=self._private)
                if not isinstance(value, dict) or value.get("isError") or value.get("error"):
                    raise DomainError("BACKEND_PROTOCOL", "Computer backend operation failed")
                if value.get("verdict") not in {"completed", "selected", "verify_with_capture", "use_element_indices_or_re_capture"}:
                    raise DomainError("BACKEND_PROTOCOL", "Computer backend did not confirm the operation")
                return BackendResult("ok", json.dumps(value, ensure_ascii=False), effect)
        except ControlError:
            if effect == "none":
                context.CheckCancelled()
                return BackendResult("error", '{"code":"COMPUTER_TIMEOUT"}')
            return BackendResult("unknown", '{"code":"COMPUTER_INTERRUPTED"}', effect)
        except Exception:
            return BackendResult("error" if effect == "none" else "unknown", '{"code":"COMPUTER_FAILED"}', effect)
