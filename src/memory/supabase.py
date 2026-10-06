"""Supabase memory records; local session progress remains in SQLite."""
from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import time
import uuid

import httpx

from .embedding import EmbeddingClient, validate_vectors
from .store import (
    UNSET, MemoryBackendError, MemoryDraft, MemoryEntry, MemoryError, MemoryPage,
    MemoryPatch, MemoryQuery, MemorySearch, MemoryVersionConflict, MemoryWriteResult,
    memory_key, validate_draft, validate_query, validate_text, validate_version,
)


class SupabaseMemoryStore:
    def __init__(self, config, *, client=None, embedding=None):
        self.config, self._client, self._http = config, client, None
        self._embedding = embedding
        self._closed = False
        self._scope = hashlib.sha256(json.dumps([config.url, config.namespace]).encode()).hexdigest()
        try:
            if self._embedding is None:
                self._embedding = EmbeddingClient(config.embedding, timeout_seconds=config.timeout_seconds)
            if self._client is None:
                try:
                    from supabase import ClientOptions, create_client
                except ImportError:
                    raise MemoryError("Supabase backend requires 'uv sync --extra supabase'") from None
                from agentcore.provider_runtime import EnvironmentCredentialResolver
                key = EnvironmentCredentialResolver().resolve(config.credential_ref)
                self._http = httpx.Client(timeout=config.timeout_seconds)
                self._client = create_client(config.url, key, options=ClientOptions(
                    httpx_client=self._http, postgrest_client_timeout=config.timeout_seconds,
                    persist_session=False, auto_refresh_token=False,
                ))
            schema = self._rpc("schema")
            if schema.get("version") != 1 or schema.get("dimensions") != config.embedding.dimensions:
                raise MemoryError("Supabase memory schema/version/dimensions do not match; deploy sql/supabase/001_memory.sql")
        except BaseException:
            self.close()
            raise

    def _rpc(self, name, **params):
        if self._closed:
            raise MemoryBackendError("memory store is closed")
        for attempt in range(3):
            try:
                response = self._client.rpc("funagent_memory_" + name, {
                    "p_namespace": self.config.namespace, "p_profile": self.config.embedding.profile, **params,
                }).execute()
                data = response.data
                break
            except Exception as exc:
                # Only connection establishment is safe to retry; a read/write
                # failure can leave the result of a mutation unconfirmed.
                connection_error = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if connection_error and attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                hint = "; check network, proxy and TLS connectivity" if connection_error else ""
                detail = "; write result may be unconfirmed" if name == "write" else ""
                raise MemoryBackendError(f"Supabase {name} request failed ({type(exc).__name__}){hint}{detail}") from None
        if not isinstance(data, dict):
            raise MemoryBackendError("Supabase memory RPC returned an invalid response")
        status = data.get("status")
        if status == "profile_mismatch":
            raise MemoryBackendError("Supabase embedding profile changed; explicitly rebuild vectors before using this model")
        if status == "conflict":
            raise MemoryVersionConflict("memory changed; reload the entry before saving")
        if status == "invalid":
            raise MemoryError("invalid Supabase memory operation")
        if status == "duplicate_replacement":
            raise MemoryError("replacement duplicates another entry")
        return data

    @staticmethod
    def _entry(row):
        try:
            return MemoryEntry(row["id"], row["kind"], row["subject"], row["content"], row["version"],
                               row["source"], row["event_at"],
                               datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp(),
                               datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).timestamp())
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MemoryBackendError("Supabase returned an invalid memory entry") from None

    def _write_result(self, data):
        status = data.get("status")
        if status not in {"ok", "duplicate", "missing"}:
            raise MemoryBackendError("Supabase returned an invalid write status")
        entry = self._entry(data["entry"]) if data.get("entry") is not None else None
        if status != "missing" and entry is None:
            raise MemoryBackendError("Supabase write response is missing the entry")
        return MemoryWriteResult(status == "ok", entry, None if status == "ok" else status)

    def _vector(self, text, purpose):
        if self._closed:
            raise MemoryBackendError("memory store is closed")
        vectors = self._embedding.embed([text], purpose=purpose)
        return validate_vectors(vectors, 1, self.config.embedding.dimensions)[0]

    def add(self, draft: MemoryDraft):
        draft = validate_draft(draft)
        data = asdict(draft)
        data.update(id=str(uuid.uuid4()), dedupe_key=memory_key(draft),
                    embedding=self._vector(f"{draft.subject}: {draft.content}", "document"))
        return self._write_result(self._rpc("write", p_action="add", p_data=data))

    def list(self, query: MemoryQuery = MemoryQuery()):
        validate_query(query.kind, query.limit)
        after = None
        if query.cursor:
            try:
                if not isinstance(query.cursor, str) or len(query.cursor) > 1000:
                    raise ValueError()
                target, kind, stamp, entry_id = json.loads(base64.urlsafe_b64decode(query.cursor))
                if target != self._scope or kind != query.kind or not isinstance(entry_id, str) or not entry_id:
                    raise ValueError()
                datetime.fromisoformat(stamp)
                after = {"updated_at": stamp, "id": entry_id}
            except (ValueError, TypeError, UnicodeError):
                raise MemoryError("invalid memory cursor") from None
        data = self._rpc("list", p_kind=query.kind, p_limit=query.limit, p_after=after, p_id=None)
        try:
            rows, total = data["entries"], data["total"]
            entries = tuple(self._entry(row) for row in rows[:query.limit])
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise ValueError()
            cursor = None
            if len(rows) > query.limit:
                last = rows[query.limit - 1]
                cursor = base64.urlsafe_b64encode(json.dumps([
                    self._scope, query.kind, last["updated_at"], last["id"],
                ]).encode()).decode()
            return MemoryPage(entries, total, cursor)
        except (KeyError, TypeError, ValueError):
            raise MemoryBackendError("Supabase returned an invalid memory page") from None

    def search(self, query: MemorySearch):
        validate_query(query.kind, query.limit)
        text = validate_text(query.query, blank=True, limit=1000)
        if self._closed:
            raise MemoryBackendError("memory store is closed")
        if not text:
            return []
        data = self._rpc("search", p_kind=query.kind, p_limit=query.limit,
                         p_embedding=self._vector(text, "query"), p_min_similarity=self.config.min_similarity)
        if not isinstance(data.get("entries"), list):
            raise MemoryBackendError("Supabase returned invalid search results")
        return [self._entry(row) for row in data["entries"]]

    def update(self, entry_id: str, patch: MemoryPatch, *, expected_version: int):
        validate_version(expected_version)
        self._id(entry_id)
        rows = self._rpc("list", p_kind=None, p_limit=1, p_after=None, p_id=entry_id).get("entries")
        if not isinstance(rows, list):
            raise MemoryBackendError("Supabase returned invalid lookup results")
        if not rows:
            return MemoryWriteResult(False, reason="missing")
        old = self._entry(rows[0])
        if old.version != expected_version:
            raise MemoryVersionConflict("memory changed; reload the entry before saving")
        draft = validate_draft(MemoryDraft(old.kind, patch.content, old.subject if patch.subject is None else patch.subject,
            old.source if patch.source is None else patch.source, old.event_at if patch.event_at is UNSET else patch.event_at))
        data = asdict(draft)
        data.update(id=entry_id, expected_version=expected_version,
                    dedupe_key=memory_key(draft) if old.kind != "episode" else None,
                    embedding=self._vector(f"{draft.subject}: {draft.content}", "document"))
        return self._write_result(self._rpc("write", p_action="update", p_data=data))

    def delete(self, entry_id: str, *, expected_version: int):
        validate_version(expected_version)
        self._id(entry_id)
        return self._write_result(self._rpc("write", p_action="delete",
                                  p_data={"id": entry_id, "expected_version": expected_version}))

    @staticmethod
    def _id(entry_id):
        if not isinstance(entry_id, str) or not entry_id:
            raise MemoryError("id must be a nonblank string")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._http is not None:
                self._http.close()
            elif self._client is not None and callable(getattr(self._client, "close", None)):
                self._client.close()
        finally:
            if self._embedding is not None:
                self._embedding.close()
