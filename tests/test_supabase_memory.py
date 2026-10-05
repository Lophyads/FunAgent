from __future__ import annotations

from dataclasses import replace
import importlib
import json
from types import SimpleNamespace

import pytest

from memory import EmbeddingConfig, MemoryConfig, MemoryFeature, SqliteConfig, SupabaseConfig, load_memory_config
from memory.embedding import EmbeddingClient
from memory.sqlite import SqliteMemoryStore
from memory.store import MemoryBackendError, MemoryDraft, MemoryError, MemoryPatch, MemoryQuery, MemorySearch, MemoryVersionConflict
from memory.supabase import SupabaseMemoryStore


def config(**changes):
    return SupabaseConfig("https://test.supabase.co", "env:SUPABASE_TEST_KEY",
                          EmbeddingConfig("http://127.0.0.1:1234/v1", "local-model", 3), **changes)


def row(entry_id="opaque:one", **changes):
    return {"id": entry_id, "kind": "user", "subject": "Tea", "content": "Prefers tea", "version": 1,
            "source": {"session_id": "real-session"}, "event_at": None,
            "created_at": "2026-10-05T10:00:00.123456+00:00", "updated_at": "2026-10-05T10:00:00.123456+00:00", **changes}


class RpcClient:
    def __init__(self, *responses):
        self.responses = [{"status": "ok", "version": 1, "dimensions": 3}, *responses]
        self.calls = []
        self.closed = 0

    def rpc(self, name, params):
        self.calls.append((name, params))
        def execute():
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(data=result)
        return SimpleNamespace(execute=execute)

    def close(self):
        self.closed += 1


class Embedder:
    def __init__(self, vectors=None):
        self.calls, self.closed = [], 0
        self.vectors = [[1.0, 0.0, 0.0]] if vectors is None else vectors

    def embed(self, texts, *, purpose):
        self.calls.append((texts, purpose))
        if isinstance(self.vectors, Exception):
            raise self.vectors
        return self.vectors

    def close(self):
        self.closed += 1


def sdk(response):
    calls, closed = [], []
    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(data=response)
    return SimpleNamespace(embeddings=SimpleNamespace(create=create), close=lambda: closed.append(True)), calls, closed


def test_embedding_batch_indexes_prefixes_and_local_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = replace(config().embedding, query_prefix="query: ", document_prefix="passage: ")
    client, calls, closed = sdk([SimpleNamespace(index=1, embedding=[0, 1, 0]), SimpleNamespace(index=0, embedding=[1, 0, 0])])
    embed = EmbeddingClient(cfg, client=client)
    assert embed.embed(["one", "two"], purpose="query") == [[1, 0, 0], [0, 1, 0]]
    assert calls[0] == {"model": "local-model", "input": ["query: one", "query: two"], "encoding_format": "float"}
    assert embed.embed([]) == []
    embed.close(); embed.close()
    assert closed == [True]
    with pytest.raises(MemoryBackendError, match="closed"):
        embed.embed(["one"])
    captures = []
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: captures.append(kwargs) or client)
    EmbeddingClient(cfg).close()
    assert captures[0]["api_key"] == "unused" and captures[0]["base_url"].startswith("http://127.0.0.1")
    assert captures[0]["max_retries"] == 0
    monkeypatch.setenv("EMBEDDING_TEST_KEY", "test-secret")
    EmbeddingClient(replace(cfg, credential_ref="env:EMBEDDING_TEST_KEY")).close()
    assert captures[-1]["api_key"] == "test-secret"


@pytest.mark.parametrize("data", [[], [SimpleNamespace(index=1, embedding=[1, 0, 0])],
    [SimpleNamespace(index=0, embedding=[0, 0, 0])], [SimpleNamespace(index=0, embedding=[1, 0])],
    [SimpleNamespace(index=0, embedding=[float("nan"), 0, 1])], [SimpleNamespace(index=0, embedding=[True, 0, 1])]])
def test_embedding_rejects_invalid_service_output(data):
    client, _, _ = sdk(data)
    embed = EmbeddingClient(config().embedding, client=client)
    with pytest.raises(MemoryBackendError):
        embed.embed(["one"])
    embed.close()


def test_rpc_crud_versions_sources_and_document_query_routing():
    original, updated = row(), row(version=2, content="Prefers coffee")
    client = RpcClient({"status":"ok", "entry":original}, {"status":"duplicate", "entry":original},
        {"status":"ok", "entries":[original]}, {"status":"ok", "entries":[original], "total":1},
        {"status":"ok", "entry":updated}, {"status":"conflict"}, {"status":"ok", "entry":updated},
        {"status":"missing"}, {"status":"ok", "entries":[], "total":0})
    embed = Embedder()
    store = SupabaseMemoryStore(config(), client=client, embedding=embed)
    draft = MemoryDraft("user", "Prefers tea", "Tea", {"session_id":"real-session"})
    assert store.add(draft).entry.source == draft.source
    assert store.add(draft).reason == "duplicate"
    assert store.search(MemorySearch("a hot drink"))[0].id == original["id"]
    changed = store.update(original["id"], MemoryPatch("Prefers coffee"), expected_version=1)
    assert changed.entry.version == 2
    assert client.calls[-1][1]["p_data"]["source"] == original["source"]
    with pytest.raises(MemoryVersionConflict):
        store.delete(original["id"], expected_version=1)
    assert store.delete(original["id"], expected_version=2).changed
    assert store.delete(original["id"], expected_version=2).reason == "missing"
    assert store.update("missing", MemoryPatch("x"), expected_version=1).reason == "missing"
    assert [purpose for _, purpose in embed.calls] == ["document", "document", "query", "document"]
    assert all(params["p_namespace"] == "personal-agent" for _, params in client.calls)
    store.close(); store.close()
    assert client.closed == embed.closed == 1
    with pytest.raises(MemoryBackendError, match="closed"):
        store.list()


def test_pagination_preserves_timestamp_and_rejects_other_targets():
    first, second, third = row("one"), row("two"), row("three")
    client = RpcClient({"entries":[first, second, third], "total":3}, {"entries":[third], "total":3})
    store = SupabaseMemoryStore(config(), client=client, embedding=Embedder())
    page = store.list(MemoryQuery("user", 2))
    assert len(page.entries) == 2 and page.next_cursor and page.total == 3
    assert store.list(MemoryQuery("user", 2, page.next_cursor)).next_cursor is None
    assert client.calls[-1][1]["p_after"] == {"updated_at": second["updated_at"], "id":"two"}
    for query in (MemoryQuery("note", 2, page.next_cursor), MemoryQuery(cursor="broken"), MemoryQuery(limit=True)):
        with pytest.raises(MemoryError):
            store.list(query)
    other = SupabaseMemoryStore(config(namespace="other"), client=RpcClient(), embedding=Embedder())
    with pytest.raises(MemoryError):
        other.list(MemoryQuery("user", 2, page.next_cursor))
    other.close(); store.close()


def test_failed_embedding_and_atomic_conflict_never_commit_partial_data():
    client, embed = RpcClient(), Embedder([[1, 0]])
    store = SupabaseMemoryStore(config(), client=client, embedding=embed)
    with pytest.raises(MemoryBackendError):
        store.add(MemoryDraft("user", "fact"))
    assert len(client.calls) == 1
    store.close()
    client = RpcClient({"entries":[row()], "total":1}, {"status":"conflict"})
    store = SupabaseMemoryStore(config(), client=client, embedding=Embedder())
    with pytest.raises(MemoryVersionConflict):
        store.update("opaque:one", MemoryPatch("new content"), expected_version=1)
    assert client.calls[-1][1]["p_data"]["expected_version"] == 1
    store.close()


@pytest.mark.parametrize("failure", [{"status":"profile_mismatch"}, {"version":1, "dimensions":1024}, RuntimeError("private secret body")])
def test_initialization_failure_cleans_resources_and_redacts_body(failure):
    client, embed = RpcClient(), Embedder()
    client.responses = [failure]
    with pytest.raises(MemoryError) as raised:
        SupabaseMemoryStore(config(), client=client, embedding=embed)
    assert "private secret" not in str(raised.value)
    assert client.closed == embed.closed == 1


def test_config_active_backend_env_and_optional_credentials(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[memory.supabase]\nnonsense=true\n', encoding="utf-8")
    assert load_memory_config(path).backend == "sqlite"
    content = '''[memory]
backend="supabase"
review_every_turns=0
[memory.supabase]
url="https://test.supabase.co"
credential_ref="env:SUPABASE_TEST_KEY"
[memory.supabase.embedding]
base_url="http://localhost:11434/v1"
model="local"
dimensions=3
'''
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("FUNAGENT_MEMORY_CFG_KEY", "existing-env")
    path.with_name(".env").write_text("FUNAGENT_MEMORY_CFG_KEY=file-value\n", encoding="utf-8")
    cfg = load_memory_config(path)
    import os
    assert os.environ["FUNAGENT_MEMORY_CFG_KEY"] == "existing-env"
    monkeypatch.delenv("FUNAGENT_MEMORY_CFG_KEY")
    load_memory_config(path)
    assert os.environ["FUNAGENT_MEMORY_CFG_KEY"] == "file-value"
    assert cfg.supabase.embedding.credential_ref is None
    assert cfg.supabase.embedding.dimensions == 3
    for bad in (content.replace('dimensions=3', 'dimensions=true'), content + 'provider="ollama"\n', content.replace('http://localhost:11434/v1', 'http://user:password@localhost/v1')):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(MemoryError):
            load_memory_config(path)


def test_supabase_feature_reuses_local_progress_and_protects_actual_files(tmp_path, config_factory, context_factory):
    settings = MemoryConfig(backend="supabase", supabase=config(), sqlite=SqliteConfig(tmp_path / "progress.db"))
    remote = SupabaseMemoryStore(config(), client=RpcClient(), embedding=Embedder())
    with context_factory() as context:
        feature = MemoryFeature(settings, config_factory(), store=remote)
        from tools.runtime import get_tool_registry
        feature.install_tool(get_tool_registry())
        progress = feature.runner.progress
        assert isinstance(progress, SqliteMemoryStore) and feature.runner.store is remote
        assert progress.observe_turn("s", "t", 2) == 1
        assert progress.list().total == 0
        assert context.services.is_protected_path(progress.path)
        plain = SqliteMemoryStore(progress.path)
        other = SqliteMemoryStore(progress.path, review_scope=("supabase", settings.supabase.url, "other"))
        assert plain.review_state("s").completed_count == other.review_state("s").completed_count == 0
        plain.close(); other.close()
        assert feature.close()
        assert not context.services.is_protected_path(progress.path)
        with pytest.raises(MemoryBackendError):
            progress.review_state("s")
    disabled_path = tmp_path / "unused.db"
    feature = MemoryFeature(replace(settings, review_every_turns=0, sqlite=SqliteConfig(disabled_path)), config_factory(),
        store=SupabaseMemoryStore(config(), client=RpcClient(), embedding=Embedder()))
    assert not disabled_path.exists()
    assert feature.close()


def test_real_sdk_uses_owned_mock_http_and_environment_key(monkeypatch):
    supabase = pytest.importorskip("supabase")
    import httpx
    module = importlib.import_module("memory.supabase")
    original = httpx.Client
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status":"ok", "version":1, "dimensions":3})
    owned = original(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: owned)
    monkeypatch.setenv("SUPABASE_TEST_KEY", "sb_secret_test_placeholder")
    store = module.SupabaseMemoryStore(config(), embedding=Embedder())
    assert len(requests) == 1 and requests[0].url.path.endswith("/rpc/funagent_memory_schema")
    assert requests[0].headers["apikey"] == "sb_secret_test_placeholder"
    store.close()
    assert owned.is_closed


def test_empty_search_and_faults_are_distinct_and_invalid_versions_do_not_embed():
    client, embed = RpcClient({"entries":[]}, TimeoutError("private request body")), Embedder()
    store = SupabaseMemoryStore(config(), client=client, embedding=embed)
    assert store.search(MemorySearch("  ")) == []
    assert embed.calls == [] and len(client.calls) == 1
    assert store.search(MemorySearch("unrelated")) == []
    with pytest.raises(MemoryBackendError) as raised:
        store.search(MemorySearch("another"))
    assert "private request body" not in str(raised.value)
    count = len(embed.calls)
    with pytest.raises(MemoryError):
        store.update("one", MemoryPatch("x"), expected_version=True)
    assert len(embed.calls) == count
    store.close()


def test_factory_selects_cloud_and_sqlite_has_no_remote_dependencies(tmp_path, monkeypatch):
    from memory import create_store
    import sys
    selected = []
    monkeypatch.setattr("memory.supabase.SupabaseMemoryStore", lambda cfg: selected.append(cfg) or "cloud")
    settings = MemoryConfig(backend="supabase", supabase=config())
    assert create_store(settings) == "cloud" and selected == [settings.supabase]
    monkeypatch.setitem(sys.modules, "supabase", None)
    monkeypatch.setattr("memory.embedding.EmbeddingClient", lambda *args, **kwargs: pytest.fail("SQLite constructed an embedding client"))
    local = create_store(MemoryConfig(sqlite=SqliteConfig(tmp_path / "sqlite.db")))
    assert local.add(MemoryDraft("user", "local fact")).changed
    local.close()
