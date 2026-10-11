"""Opt-in process backend."""
from ..contracts import BackendResult, Availability
from .managed import ManagedBackend
from .process import RunProcess
from ..files import Resolve
from ...frame.contracts import DomainError, ControlError
import json


class TerminalBackend(ManagedBackend):
    def __init__(self, settings):
        super().__init__(settings)
        self._timeout = settings.get("timeout", 60)
        self._max_output = settings.get("max_output", 16000)
    def Probe(self):
        return Availability(not self._closing, {"process.execute"})
    def Execute(self, operation, args, context):
        if operation != "terminal":
            return BackendResult("invalid", '{"code":"invalid_operation"}')
        try:
            with self.Borrow(context):
                directory = Resolve(args.get("workdir", "."), context)
                if not directory.is_dir():
                    raise DomainError("INVALID_REQUEST", "Terminal requires an existing directory")
                value = RunProcess(args["command"], directory, context,
                    min(self._timeout, args.get("timeout_seconds", 30)), self._max_output)
                return BackendResult("unknown" if value["interrupted"] else "ok" if value["exit_code"] == 0 else "error",
                                     json.dumps(value, ensure_ascii=False), "unknown")
        except ControlError:
            raise
        except DomainError as error:
            return BackendResult("denied" if error.code == "DENIED" else "error", json.dumps({"code": error.code}))
        except Exception:
            return BackendResult("unknown", '{"code":"PROCESS_FAILED"}', "unknown")
