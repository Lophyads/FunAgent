"""Apply development memory settings using the existing application factories."""
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from ..frame.contracts import DomainError
from ..frame.lifecycle import Lifecycle


logger = logging.getLogger("funagent.memory_settings")


@dataclass
class MemoryServices:
    resources: Lifecycle
    models: object
    memory: object
    operations: object
    judge: object

    def Close(self):
        self.resources.Stop()


class MemorySettings:
    def __init__(self, conf_dir, conf, services, *, build, bind, busy):
        self._path = Path(conf_dir) / "memory.conf"
        self._conf, self._services = conf, services
        self._build, self._bind, self._busy = build, bind, busy
        self._lock, self._stopping = RLock(), False

    @contextmanager
    def Scope(self):
        """Serialize synchronous memory operations and background job admission."""
        with self._lock:
            if self._stopping:
                raise DomainError("STOPPING", "Application is stopping")
            yield

    def Read(self):
        with self.Scope():
            try:
                return {"content": self._path.read_text(encoding="utf8")}
            except (OSError, UnicodeError):
                raise DomainError("STORAGE", "Could not read memory.conf") from None

    def _Save(self, content):
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf8", newline="\n",
                                    dir=self._path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            temporary.replace(self._path)
        except (OSError, UnicodeError):
            raise DomainError("STORAGE", "Could not save memory.conf") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _Discard(self, services):
        try:
            services.Close()
        except Exception:
            # Keep failed closers owned by the existing lifecycle until shutdown.
            self._services.resources.Own(services, services.Close)
            logger.warning("Memory settings resource cleanup failed")

    def Apply(self, content):
        with self.Scope():
            if self._busy():
                raise DomainError("CONFLICT", "请等待当前任务、复盘、导入或评测完成后再应用记忆配置。")
            candidate = self._conf.WithMemory(content)
            services = self._build(candidate)
            try:
                self._Save(content)
            except BaseException:
                self._Discard(services)
                raise
            # All validation and construction precedes these reference updates.
            self._bind(candidate, services)
            previous, self._services = self._services, services
            self._Discard(previous)
            logger.info("Memory configuration applied")
            return {"applied": True}

    def RequestStop(self):
        with self._lock:
            self._stopping = True

    def Close(self):
        self.RequestStop()
        self._services.Close()
