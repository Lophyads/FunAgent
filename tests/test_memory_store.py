from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memory.feature import MemoryConfig, load_memory_config
from memory.prompt import export_markdown, import_legacy
from memory.sqlite import SqliteMemoryStore
from memory.store import (
    MemoryBackendError, MemoryDraft, MemoryError, MemoryPatch, MemoryQuery,
    MemorySearch, MemoryStore, MemoryVersionConflict,
)


@pytest.fixture
def store(tmp_path):
    value = SqliteMemoryStore(tmp_path / "memory.db")
    yield value
    value.close()


def test_record_crud_dedupe_and_compare_and_swap(store):
    assert isinstance(store, MemoryStore)
    entry = store.add(MemoryDraft("user", "Prefers tea")).entry
    assert isinstance(entry.id, str) and entry.version == 1
    duplicate = store.add(MemoryDraft("note", "  PREFERS   TEA  "))
    assert duplicate.reason == "duplicate" and duplicate.entry.id == entry.id
    other = SqliteMemoryStore(store.path)
    try:
        changed = other.update(entry.id, MemoryPatch("Prefers coffee"), expected_version=1).entry
        assert changed.version == 2 and changed.created_at == entry.created_at
        with pytest.raises(MemoryVersionConflict):
            store.update(entry.id, MemoryPatch("stale"), expected_version=1)
        with pytest.raises(MemoryVersionConflict):
            store.delete(entry.id, expected_version=1)
        assert store.delete(entry.id, expected_version=2).changed
        assert store.delete(entry.id, expected_version=2).reason == "missing"
        assert store.update("opaque:missing", MemoryPatch("x"), expected_version=1).reason == "missing"
    finally:
        other.close()


def test_fts_tracks_add_update_delete_unicode_and_safe_queries(store):
    latin = store.add(MemoryDraft("note", "A café serves tea next to a carpet")).entry
    chinese = store.add(MemoryDraft("user", "我通常喜欢喝咖啡并研究分布式数据库")).entry
    assert store.search(MemorySearch("cafe"))[0].id == latin.id
    assert store.search(MemorySearch("car")) == []
    assert store.search(MemorySearch("咖啡"))[0].id == chinese.id
    assert store.search(MemorySearch("分布式数据库", "note")) == []
    assert store.search(MemorySearch("   ")) == []
    assert store.search(MemorySearch('"tea" OR * NEAR(foo)'))[0].id == latin.id
    store.update(latin.id, MemoryPatch("A café serves juice"), expected_version=1)
    assert store.search(MemorySearch("tea")) == []
    assert store.search(MemorySearch("juice"))[0].version == 2
    store.delete(latin.id, expected_version=2)
    assert store.search(MemorySearch("juice")) == []
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO entries_fts(entries_fts,rank) VALUES('integrity-check',1)")


def test_paging_kind_filters_and_query_validation(store):
    for i in range(7):
        store.add(MemoryDraft("user" if i % 2 else "note", f"fact {i}"))
    page = store.list(MemoryQuery("note", 2))
    assert page.total == 4 and len(page.entries) == 2 and page.next_cursor
    more = store.list(MemoryQuery("note", 2, page.next_cursor))
    assert more.total == 4 and len(more.entries) == 2 and not more.next_cursor
    assert not {entry.id for entry in page.entries} & {entry.id for entry in more.entries}
    for query in (MemoryQuery("user", 2, page.next_cursor), MemoryQuery(limit=0), MemoryQuery(cursor="broken")):
        with pytest.raises(MemoryError):
            store.list(query)
    with pytest.raises(MemoryError):
        store.add(MemoryDraft("invalid", "text"))
    with pytest.raises(MemoryError):
        store.add(MemoryDraft("user", "x", source={"nan": float("nan")}))
    with pytest.raises(MemoryError):
        store.delete(page.entries[0].id, expected_version=True)


def test_episode_identity_and_explicit_date_clearing(store):
    source = {"session_id": "s", "turn_id": "t", "range_start": 0, "range_end": 2}
    draft = MemoryDraft("episode", "Release completed", source=source, event_at="2026-10-01")
    entry = store.add(draft).entry
    assert not store.add(draft).changed
    assert store.add(MemoryDraft("episode", draft.content, source=source, event_at="2026-10-02")).changed
    assert store.add(MemoryDraft("episode", draft.content, source={**source, "turn_id": "other"}, event_at=draft.event_at)).changed
    changed = store.update(entry.id, MemoryPatch("Release confirmed"), expected_version=1).entry
    assert changed.event_at == "2026-10-01"
    cleared = store.update(entry.id, MemoryPatch(changed.content, event_at=None), expected_version=2).entry
    assert cleared.event_at is None
    assert not store.add(draft).changed  # Replay of the original batch stays idempotent.
    with pytest.raises(MemoryError):
        store.add(MemoryDraft("episode", "bad date", event_at="yesterday"))


def test_concurrent_initialization_and_cross_process_writes(tmp_path):
    path = tmp_path / "memory.db"
    def write(i):
        adapter = SqliteMemoryStore(path)
        try:
            return adapter.add(MemoryDraft("note", "same fact")).changed
        finally:
            adapter.close()
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(write, range(12))) == 1
    store = SqliteMemoryStore(path)
    script = ("import sys\nfrom memory.sqlite import SqliteMemoryStore\nfrom memory.store import MemoryDraft\n"
              "store=SqliteMemoryStore(sys.argv[1])\n"
              "for i in range(20): store.add(MemoryDraft('note',f'child {i}'))\nstore.close()\n")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    child = subprocess.Popen([sys.executable, "-c", script, str(path)], env=env)
    try:
        for i in range(20):
            store.add(MemoryDraft("note", f"parent {i}"))
        assert child.wait(timeout=15) == 0
        assert store.list(MemoryQuery()).total == 41
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)
        store.close()


def test_schema_checks_and_backend_errors_are_visible(tmp_path):
    path = tmp_path / "different.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing(value)")
    before = path.read_bytes()
    with pytest.raises(MemoryError, match="different database"):
        SqliteMemoryStore(path)
    assert path.read_bytes() == before
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(MemoryError, match="schema version"):
        SqliteMemoryStore(path)
    store = SqliteMemoryStore(tmp_path / "corrupt.db")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE entries_fts")
    with pytest.raises(MemoryBackendError):
        store.search(MemorySearch("anything"))
    store.close()
    with pytest.raises(MemoryBackendError, match="closed"):
        store.list(MemoryQuery())


def test_config_default_on_path_resolution_and_legacy_rejection(tmp_path):
    path = tmp_path / "agentcore.toml"
    path.write_text("", encoding="utf-8")
    config = load_memory_config(path)
    assert config.enabled and config.backend == "sqlite"
    assert config.sqlite.path == tmp_path / "memories" / "memory.db"
    path.write_text('[memory]\nreview_every_turns=0\n[memory.sqlite]\npath="notes.db"\n', encoding="utf-8")
    assert load_memory_config(path).sqlite.path == tmp_path / "notes.db"
    with pytest.raises(MemoryError):
        MemoryConfig(backend="unknown")
    path.write_text('[file_memory]\nenabled=true\n', encoding="utf-8")
    with pytest.raises(MemoryError, match="import-legacy"):
        load_memory_config(path)
    assert load_memory_config(path, allow_legacy=True).enabled


def test_explicit_import_and_export_never_create_a_second_source(store, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    original = b"prefers tea\n\xc2\xa7\nuses Python"
    (legacy / "USER.md").write_bytes(original)
    (legacy / "MEMORY.md").write_bytes(b"SQLite is the memory backend")
    assert import_legacy(store, legacy) == {"imported": 3, "duplicates": 0}
    assert import_legacy(store, legacy) == {"imported": 0, "duplicates": 3}
    assert (legacy / "USER.md").read_bytes() == original
    output = tmp_path / "export"
    assert export_markdown(store, output) == {"USER.md": 2, "MEMORY.md": 1}
    (output / "USER.md").write_text("edited export", encoding="utf-8")
    assert store.search(MemorySearch("edited")) == []
    with pytest.raises(MemoryError, match="overwrite"):
        export_markdown(store, output)
    partial = tmp_path / "partial"
    partial.mkdir(); (partial / "MEMORY.md").write_text("keep", encoding="utf-8")
    with pytest.raises(MemoryError):
        export_markdown(store, partial)
    assert not (partial / "USER.md").exists()
    (legacy / "USER.md").write_bytes(b"valid")
    (legacy / "MEMORY.md").write_bytes(b"broken\n\xc2\xa7\n")
    before = store.list(MemoryQuery()).total
    with pytest.raises(MemoryError, match="malformed"):
        import_legacy(store, legacy)
    assert store.list(MemoryQuery()).total == before


def test_maintenance_cli_needs_no_model_config(tmp_path, capsys):
    from agentcore.cli import run_cli
    config = tmp_path / "agentcore.toml"
    config.write_text('[file_memory]\nenabled=true\n', encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir(); (legacy / "USER.md").write_bytes(b"prefers tea")
    assert run_cli(["memory", "import-legacy", "--config", str(config), "--directory", str(legacy)]) == 0
    assert '"imported": 1' in capsys.readouterr().out
    config.write_text("", encoding="utf-8")
    assert run_cli(["memory", "export", "--config", str(config), "--directory", str(tmp_path / "view")]) == 0
    assert (tmp_path / "view" / "USER.md").exists()
