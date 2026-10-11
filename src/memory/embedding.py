"""OpenAI-compatible embedding wire driver, bound once by the memory factory."""
import os
from math import isfinite

from ..frame.contracts import DomainError
from .contracts import EmbeddingProfile
from .common import text
from .transport import JSONHTTP


def ValidateProfile(profile):
    if (not isinstance(profile, EmbeddingProfile) or not text(profile.model) or
            type(profile.dimensions) is not int or not 1 <= profile.dimensions <= 4096 or
            any(not isinstance(prefix, str) or len(prefix) > 2000 for prefix in (profile.query_prefix, profile.document_prefix))):
        raise DomainError("CONFIG_INVALID", "Invalid embedding profile")
    return profile


def ValidateVector(vector, dimensions):
    if (not isinstance(vector, (list, tuple)) or len(vector) != dimensions or
            any(type(value) not in (int, float) or not isfinite(value) for value in vector) or
            not any(vector)):
        raise DomainError("EMBEDDING_PROTOCOL", "Invalid embedding vector")
    return tuple(float(value) for value in vector)


def DecodeBatch(payload, count, dimensions):
    try:
        rows = payload["data"]
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError
        indexes = [row["index"] for row in rows]
        if any(type(index) is not int for index in indexes) or sorted(indexes) != list(range(count)):
            raise ValueError
        return tuple(ValidateVector(row["embedding"], dimensions) for row in sorted(rows, key=lambda r: r["index"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DomainError("EMBEDDING_PROTOCOL", "Invalid embedding batch") from None


class HTTPEmbedder:
    def __init__(self, settings, *, transport=None):
        self.profile = ValidateProfile(EmbeddingProfile(settings["model"], settings["dimensions"],
                                        settings.get("query_prefix", ""), settings.get("document_prefix", "")))
        from .transport import ValidateConnection
        ValidateConnection(settings["endpoint"], settings.get("timeout", 30))
        token = os.environ.get(settings.get("credential_env", "EMBEDDING_API_KEY"))
        if not token:
            raise DomainError("CONFIG_CREDENTIAL", "Embedding credential is missing")
        self._dimensions = settings.get("request_dimensions", False)
        if type(self._dimensions) is not bool:
            raise DomainError("CONFIG_INVALID", "Invalid embedding request configuration")
        self._http = JSONHTTP(settings["endpoint"], {"Authorization": "Bearer " + token},
                              settings.get("timeout", 30), transport=transport,
                              protocol="EMBEDDING_PROTOCOL", source="EMBEDDING_SOURCE")

    def Embed(self, texts, purpose, context):
        context.CheckCancelled()
        if (not isinstance(texts, (tuple, list)) or not 1 <= len(texts) <= 128 or
                any(not text(value) or len(value) > 65536 for value in texts) or purpose not in {"query", "document"}):
            raise DomainError("INVALID_REQUEST", "Invalid embedding input")
        prefix = self.profile.query_prefix if purpose == "query" else self.profile.document_prefix
        payload = {"model": self.profile.model, "input": [prefix + value for value in texts], "encoding_format": "float"}
        if self._dimensions:
            payload["dimensions"] = self.profile.dimensions
        return DecodeBatch(self._http.Post("", payload, context), len(texts), self.profile.dimensions)

    def Close(self):
        self._http.Close()
