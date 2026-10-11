"""Application-owned handlers with safe output and reversible configuration."""
import copy
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re

from .contracts import DomainError


CONTEXT_FIELDS = ("session_id", "task_id", "run_id", "purpose", "error_code")
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s session=%(session_id)s task=%(task_id)s run=%(run_id)s purpose=%(purpose)s %(message)s"
_SENSITIVE = re.compile(r"password|secret|token|api[_-]?key|authorization|credential", re.I)
_ASSIGNMENT = re.compile(r"(?i)([\"']?(?:password|secret|token|api[_-]?key|authorization|credential)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[^\s,;\"']+")
_USERINFO = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.I)


def _secrets(conf):
    values = {value for key, value in os.environ.items() if _SENSITIVE.search(key) and len(value) >= 4}
    def visit(data):
        if hasattr(data, "items"):
            for key, value in data.items():
                if key == "credential_env":
                    token = os.environ.get(value)
                    if token:
                        values.add(token)
                elif _SENSITIVE.search(key) and isinstance(value, str) and value:
                    values.add(value)
                visit(value)
    visit(conf.Model().Providers())
    visit(conf.Memory().Sources())
    visit(conf.Tool().Backends())
    return tuple(sorted(values, key=len, reverse=True))


class SafeFormatter(logging.Formatter):
    def __init__(self, fmt, secrets):
        super().__init__(fmt)
        self._secrets = secrets

    def format(self, record):
        safe = copy.copy(record)
        for field in CONTEXT_FIELDS:
            if not hasattr(safe, field):
                setattr(safe, field, "-")
        output = super().format(safe)
        return self.Redact(output)

    def Redact(self, output):
        for secret in self._secrets:
            output = output.replace(secret, "[REDACTED]")
        output = _BEARER.sub(r"\1[REDACTED]", output)
        output = _ASSIGNMENT.sub(r"\1[REDACTED]", output)
        return _USERINFO.sub(r"\1[REDACTED]@", output)


class LogSession:
    def __init__(self, conf, conf_dir):
        self._logger = logging.getLogger("funagent")
        self._previous = (tuple(self._logger.handlers), self._logger.level, self._logger.propagate)
        self._handlers = []
        self._files = {}
        options = conf.Log().Options()
        try:
            formatter = SafeFormatter(options.get("format", DEFAULT_FORMAT), _secrets(conf))
            self._formatter = formatter
            formatter.format(logging.LogRecord("funagent", logging.INFO, "", 0, "configured", (), None))
            for key, level in (("file", logging.NOTSET), ("error_file", logging.ERROR)):
                if options.get(key):
                    path = Path(options[key])
                    path = path if path.is_absolute() else conf_dir / path
                    self._files["normal" if key == "file" else "error"] = path.resolve()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handler = RotatingFileHandler(path, maxBytes=options.get("max_bytes", 1048576),
                                                  backupCount=options.get("backup_count", 2), encoding="utf8")
                    self._handlers.append(handler)
                    handler.setLevel(level)
            if options.get("console", not options.get("file")):
                self._handlers.append(logging.StreamHandler())
            for handler in self._handlers:
                handler.setFormatter(formatter)
        except Exception:
            self._CloseHandlers()
            raise DomainError("CONFIG_INVALID", "Invalid logging configuration") from None
        self._logger.handlers = list(self._handlers)
        self._logger.setLevel(options["level"])
        self._logger.propagate = False

    def Files(self):
        return dict(self._files)

    def Redact(self, text):
        return self._formatter.Redact(text)

    def _CloseHandlers(self):
        failed = False
        for handler in self._handlers:
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                failed = True
        self._handlers.clear()
        return failed

    def Close(self):
        previous_handlers, level, propagate = self._previous
        failed = self._CloseHandlers()
        for handler in previous_handlers:
            if handler not in self._logger.handlers:
                self._logger.addHandler(handler)
        self._logger.setLevel(level)
        self._logger.propagate = propagate
        if failed:
            raise DomainError("CLOSE_FAILED", "Application log handler close failed") from None
