"""Business access and internal recording ports, with inert standalone defaults."""
from contextlib import nullcontext
from typing import Protocol

from .contracts import ObservationRecord


class ObservationRecorder(Protocol):
    def Prepare(self, fields): ...
    def Publish(self, record: ObservationRecord): ...
    def Diagnose(self, code, task_id=None): ...
    def Close(self): ...


class ExportSink(Protocol):
    """Reserved for exporting saved traces; no SDK or network implementation."""
    def Export(self, trace): ...


class ObservationAccess(Protocol):
    """Scope kind describes physical work (including rerank model attempts).

    Logical model.rank scopes retain the default span kind, so summaries and
    external-call lists count the physical attempts only.
    """
    def PrepareInput(self, projection): ...
    def Bind(self, key): ...
    def Diagnose(self, code, task_id=None): ...
    def Scope(self, name, **fields): ...
    def Enter(self, name, **fields): ...
    def Exit(self, **fields): ...
    def Start(self, name, **fields): ...
    def Update(self, **fields): ...
    def Complete(self, **fields): ...
    def Event(self, name, **fields): ...
    def UseTask(self, task_id): ...
    def UseBinding(self, key): ...
    def Suspend(self): ...
    def StartTask(self, task_id, **fields): ...
    def EndTask(self, **fields): ...


class NullRecorder:
    def Prepare(self, fields):
        return fields

    def Publish(self, record):
        pass

    def Diagnose(self, code, task_id=None):
        pass

    def Close(self):
        pass


class NullObservation:
    """Does not evaluate lazy projections, even when passed to StartTask."""
    def _noop(self, *args, **kwargs):
        return None

    StartTask = Start = Enter = Exit = Update = Complete = EndTask = Event = Bind = _noop
    Diagnose = Close = _noop
    PrepareInput = _noop

    def Scope(self, *args, **kwargs):
        return nullcontext()

    UseTask = UseBinding = Suspend = Scope
