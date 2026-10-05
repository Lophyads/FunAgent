"""A small embeddings client shared by local and remote compatible HTTP services."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Sequence
from urllib.parse import urlsplit

from .store import MemoryBackendError, MemoryError, validate_text


def http_url(value: str) -> str:
    value = validate_text(value, limit=2000)
    try:
        parts = urlsplit(value)
        parts.port  # Validate the port without logging the original URL.
    except ValueError:
        raise MemoryError("invalid HTTP service URL") from None
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise MemoryError("HTTP service URL must contain a host and no credentials, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimensions: int
    credential_ref: str | None = None
    query_prefix: str = ""
    document_prefix: str = ""

    def __post_init__(self):
        object.__setattr__(self, "base_url", http_url(self.base_url))
        object.__setattr__(self, "model", validate_text(self.model, limit=200))
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 16000:
            raise MemoryError("embedding dimensions must be an integer from 1 to 16000")
        if self.credential_ref is not None and (not isinstance(self.credential_ref, str) or not self.credential_ref.startswith("env:") or len(self.credential_ref) <= 4):
            raise MemoryError("embedding credential_ref must be an env reference")
        for prefix in (self.query_prefix, self.document_prefix):
            if not isinstance(prefix, str) or len(prefix) > 2000:
                raise MemoryError("embedding prefixes must be text of at most 2000 characters")

    @property
    def profile(self) -> str:
        identity = [self.model, self.dimensions, self.query_prefix, self.document_prefix]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def validate_vectors(vectors, count: int, dimensions: int) -> list[list[float]]:
    try:
        if len(vectors) != count:
            raise ValueError()
        result = []
        for vector in vectors:
            if len(vector) != dimensions or any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in vector):
                raise ValueError()
            values = [float(n) for n in vector]
            if not all(math.isfinite(n) for n in values) or not any(values):
                raise ValueError()
            result.append(values)
        return result
    except (TypeError, ValueError, OverflowError):
        raise MemoryBackendError("embedding service returned invalid vectors (count, dimensions or numeric values)") from None


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig, *, timeout_seconds: float = 20, client=None):
        self.config = config
        self._closed = False
        if client is None:
            from openai import OpenAI
            from agentcore.provider_runtime import EnvironmentCredentialResolver
            key = EnvironmentCredentialResolver().resolve(config.credential_ref) if config.credential_ref else "unused"
            client = OpenAI(api_key=key, base_url=config.base_url, timeout=timeout_seconds, max_retries=0)
        self._client = client

    def embed(self, texts: Sequence[str], *, purpose: str = "document") -> list[list[float]]:
        if self._closed:
            raise MemoryBackendError("embedding client is closed")
        if purpose not in {"query", "document"} or isinstance(texts, (str, bytes)):
            raise MemoryError("embedding requires a sequence of texts and query/document purpose")
        prefix = self.config.query_prefix if purpose == "query" else self.config.document_prefix
        inputs = [prefix + validate_text(text, limit=6000) for text in texts]
        if not inputs:
            return []
        try:
            response = self._client.embeddings.create(model=self.config.model, input=inputs, encoding_format="float")
        except Exception as exc:
            # SDK exception bodies can include input text/headers; do not expose them to the UI.
            raise MemoryBackendError(f"embedding request failed ({type(exc).__name__})") from None
        try:
            ordered = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in ordered] != list(range(len(inputs))):
                raise ValueError()
            vectors = [item.embedding for item in ordered]
        except (AttributeError, TypeError, ValueError):
            raise MemoryBackendError("embedding service returned invalid batch indexes") from None
        return validate_vectors(vectors, len(inputs), self.config.dimensions)

    def close(self):
        if not self._closed:
            self._closed = True
            self._client.close()
