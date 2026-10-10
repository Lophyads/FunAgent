from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier, Event

import pytest

from funagent.contracts import ControlError, DomainError, RunContext
from funagent.memory import (SQLiteMemoryStore, CallbackMemoryStore, MemoryDraft,
                             MemoryPatch, MemoryRef, MemoryQuery, MemoryListQuery)


@pytest.fixture
def sqlite_memory(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    try:
        yield store
    finally:
        store.Close()


def draft(text="用户喜欢中文", key="turn-1:language", type="profile"):
    return MemoryDraft("facts", text, key, ("session:s1/message:1",), type)


@pytest.mark.parametrize("driver", ["sqlite", "callback"])
def test_search_store_contract_returns_actual_unicode_record_and_reference(tmp_path, driver):
    if driver == "sqlite":
        store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
        saved = store.Write(draft(), RunContext())
        reference = saved.ref
    else:
        store = CallbackMemoryStore("facts", lambda query, ctx: [{
            "record_id": "文档-1", "type": "profile", "content": "用户喜欢中文",
            "citation": "https://kb.example/docs/1", "score": 999,
        }])
        reference = MemoryRef("facts", "文档-1")
    try:
        result = store.Search(MemoryQuery("中文", ("facts",), types=("profile",)), RunContext())
        assert len(result.records) == 1
        record = result.records[0]
        assert record.ref == reference and record.content == "用户喜欢中文"
        assert record.type == "profile" and record.citation
        assert result.source_status == {"facts": "ok"}
        assert not hasattr(record, "connection")
        if driver == "sqlite":
            assert not store.Search(MemoryQuery("不存在", ("facts",)), RunContext()).records
    finally:
        store.Close()


def test_write_key_is_atomic_idempotent_across_connections(tmp_path):
    path = tmp_path / "memory.sqlite3"
    first, second = SQLiteMemoryStore(path), SQLiteMemoryStore(path)
    gate = Barrier(2)
    def write(store):
        gate.wait(timeout=3)
        return store.Write(draft(), RunContext())
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(write, s) for s in (first, second)]
            results = [j.result(timeout=5) for j in jobs]
        assert results[0] == results[1]
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT count(*) FROM memory_records").fetchone()[0] == 1
        with pytest.raises(DomainError) as error:
            first.Write(draft("同一个键不同事实"), RunContext())
        assert error.value.code == "CONFLICT"
    finally:
        first.Close()
        second.Close()


def test_update_delete_compare_and_swap_and_do_not_reuse_deleted_key(sqlite_memory):
    store, context = sqlite_memory, RunContext()
    original = store.Write(draft(), context)
    patch = MemoryPatch("用户喜欢英文", ("session:s1/message:3",))
    updated = store.Update(original.ref, patch, original.version, context)
    assert updated.version != original.version and updated.content == patch.content
    for action in (lambda: store.Update(original.ref, patch, original.version, context),
                   lambda: store.Delete(original.ref, original.version, context)):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.code == "CONFLICT"
    assert store.Search(MemoryQuery("英文"), context).records == (updated,)
    store.Delete(updated.ref, updated.version, context)
    assert not store.Search(MemoryQuery(), context).records
    with pytest.raises(DomainError) as error:
        store.Write(draft(), context)
    assert error.value.code == "CONFLICT"


def test_unicode_category_filter_and_stable_keyset_pages(sqlite_memory):
    context = RunContext()
    records = [sqlite_memory.Write(draft(text, str(n), type), context)
               for n, (text, type) in enumerate((("偏好中文", "profile"), ("Café 与 cafe\u0301", "note"),
                                                ("中文文档", "profile"), ("100%_literal", "note")))]
    assert len(sqlite_memory.Search(MemoryQuery("CAFÉ", types=("note",)), context).records) == 1
    assert sqlite_memory.Search(MemoryQuery("%_"), context).records == (records[3],)
    first = sqlite_memory.List(MemoryListQuery("facts", "profile", limit=1), context)
    assert first.items == (records[0],) and first.next_cursor
    sqlite_memory.Delete(records[0].ref, records[0].version, context)
    second = sqlite_memory.List(MemoryListQuery("facts", "profile", limit=1, cursor=first.next_cursor), context)
    assert second.items == (records[2],) and second.next_cursor is None
    with pytest.raises(DomainError) as error:
        sqlite_memory.List(MemoryListQuery("facts", "note", cursor=first.next_cursor), context)
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("driver", ["sqlite", "callback"])
def test_source_kind_filters_and_control_fail_before_callback(tmp_path, driver):
    invoked = []
    store = (SQLiteMemoryStore(tmp_path / "memory.sqlite3") if driver == "sqlite" else
             CallbackMemoryStore("facts", lambda q, ctx: invoked.append(q) or []))
    try:
        assert not store.Search(MemoryQuery(source_ids=("other",)), RunContext()).records
        wrong_kind = "semantic" if driver == "sqlite" else "episodic"
        assert not store.Search(MemoryQuery(kind=wrong_kind), RunContext()).records
        cancel = Event(); cancel.set()
        with pytest.raises(ControlError) as error:
            store.Search(MemoryQuery(), RunContext(cancel=cancel))
        assert error.value.code == "CANCELLED" and not invoked
    finally:
        store.Close()


@pytest.mark.parametrize("rows", [
    [{"record_id": "fake", "content": "facts without citation"}],
    [{"record_id": "x", "content": "secret", "citation": "x", "source_id": "forged"}],
    [{"record_id": "x", "content": "a", "citation": "x", "type": "note"},
     {"record_id": "x", "content": "b", "citation": "x", "type": "note"}],
])
def test_external_invalid_or_forged_evidence_is_rejected(rows):
    store = CallbackMemoryStore("kb", lambda query, context: rows)
    try:
        with pytest.raises(DomainError) as error:
            store.Search(MemoryQuery(), RunContext())
        assert error.value.code == "MEMORY_PROTOCOL"
        assert "secret" not in str(error.value)
    finally:
        store.Close()


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_cross_connection_cas_has_one_winner_and_conflict_preserves_winner(tmp_path, operation):
    path = tmp_path / "memory.sqlite3"
    first, second = SQLiteMemoryStore(path), SQLiteMemoryStore(path)
    original = first.Write(draft(), RunContext())
    gate = Barrier(2)
    def act(store, label):
        gate.wait(timeout=3)
        try:
            if operation == "update":
                return store.Update(original.ref, MemoryPatch(label, ("manual:" + label,)), original.version, RunContext())
            store.Delete(original.ref, original.version, RunContext())
            return "deleted"
        except DomainError as error:
            return error.code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, store, label) for store, label in ((first, "first"), (second, "second"))]
            outcomes = [f.result(timeout=5) for f in futures]
        if operation == "update":
            winners = [r for r in outcomes if not isinstance(r, str)]
            assert len(winners) == 1 and "CONFLICT" in outcomes
            assert first.Search(MemoryQuery(), RunContext()).records == tuple(winners)
        else:
            assert sorted(outcomes) == ["NOT_FOUND", "deleted"]
            assert not first.Search(MemoryQuery(), RunContext()).records
    finally:
        first.Close(); second.Close()


def test_callback_connection_lock_wait_obeys_deadline_and_cancel_without_new_io():
    import time
    entered, release = Event(), Event()
    requests = []
    def query(q, ctx):
        requests.append(q)
        entered.set()
        assert release.wait(3)
        return []
    store = CallbackMemoryStore("kb", query)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(store.Search, MemoryQuery("hold"), RunContext())
            try:
                assert entered.wait(3)
                second = pool.submit(store.Search, MemoryQuery("blocked"), RunContext(deadline=time.monotonic() + 0.03))
                with pytest.raises(ControlError) as error:
                    second.result(timeout=3)
                assert error.value.code == "DEADLINE"
                cancel = Event(); cancel.set()
                with pytest.raises(ControlError):
                    store.Search(MemoryQuery("cancelled"), RunContext(cancel=cancel))
                assert [q.text for q in requests] == ["hold"]
            finally:
                release.set()
            assert first.result(timeout=3).records == ()
    finally:
        release.set()
        store.Close()


@pytest.mark.parametrize("query", [MemoryQuery(limit=0), MemoryQuery(limit=True), MemoryQuery(kind="invalid")])
def test_invalid_query_is_rejected_by_both_store_drivers(tmp_path, query):
    stores = [SQLiteMemoryStore(tmp_path / "memory.sqlite3"), CallbackMemoryStore("kb", lambda q, ctx: [])]
    try:
        for store in stores:
            with pytest.raises(DomainError) as error:
                store.Search(query, RunContext())
            assert error.value.code == "INVALID_REQUEST"
    finally:
        for store in stores:
            store.Close()


def test_failed_write_or_update_leaves_no_partial_content(sqlite_memory):
    context = RunContext()
    with pytest.raises(DomainError):
        sqlite_memory.Write(MemoryDraft("facts", "untrusted", "x", ()), context)
    assert not sqlite_memory.Search(MemoryQuery(), context).records
    original = sqlite_memory.Write(draft(), context)
    with pytest.raises(DomainError):
        sqlite_memory.Update(original.ref, MemoryPatch("bad", ()), original.version, context)
    assert sqlite_memory.Search(MemoryQuery(), context).records == (original,)
