"""Configuration-local defaults; explicit process values always win."""
import os

from dotenv import load_dotenv

from .contracts import DomainError


class Environment:
    def __init__(self, conf_dir):
        self._added = {}
        file = conf_dir / ".env"
        previous = set(os.environ)
        try:
            if file.is_file():
                load_dotenv(file, override=False, encoding="utf-8")
        except (OSError, UnicodeError):
            raise DomainError("CONFIG_INVALID", "Unable to read configuration environment") from None
        self._added = {key: value for key, value in os.environ.items() if key not in previous}

    def Close(self):
        for key, value in self._added.items():
            if os.environ.get(key) == value:
                del os.environ[key]
        self._added.clear()
