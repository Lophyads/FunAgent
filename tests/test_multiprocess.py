from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from agentcore import SQLiteStore


def _contend_for_lease(
    database: str, owner_id: str, turn_id: str, barrier, release, outcomes,
) -> None:
    store = SQLiteStore(database)
    try:
        store.open()
        barrier.wait(timeout=10)
        lease = store.acquire_lease(
            session_id="shared-session", owner_id=owner_id, turn_id=turn_id,
            ttl_seconds=5,
        )
        outcomes.put((owner_id, lease.fencing_token if lease else None))
        release.wait(timeout=10)
        if lease is not None:
            store.release_lease(lease)
    finally:
        store.close()


@pytest.mark.multiprocess
def test_windows_spawn_allows_only_one_active_session_lease(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    release = context.Event()
    outcomes = context.Queue()
    database = str(tmp_path / "shared.db")
    bootstrap = SQLiteStore(database)
    bootstrap.open()
    bootstrap.close()
    processes = [
        context.Process(
            target=_contend_for_lease,
            args=(database, f"owner-{index}", f"turn-{index}", barrier, release, outcomes),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        claimed = [outcomes.get(timeout=15) for _ in processes]
    finally:
        release.set()
        for process in processes:
            process.join(timeout=15)

    assert all(process.exitcode == 0 for process in processes)
    tokens = [token for _, token in claimed if token is not None]
    assert tokens == [1]