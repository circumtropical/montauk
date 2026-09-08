"""Per-workspace public-id allocation: monotonic, no reuse, concurrent-safe,
self-advances past migrated ids (spec 8.5, 32.2)."""

from __future__ import annotations

import concurrent.futures

from sqlalchemy.orm import sessionmaker

from montauk.db.ids import allocate_public_id, ensure_high_water_at_least, high_water


def test_allocates_sequentially(db_session, workspace):
    ids = [allocate_public_id(db_session, workspace.id) for _ in range(5)]
    assert ids == ["P0001", "P0002", "P0003", "P0004", "P0005"]


def test_workspaces_have_independent_sequences(db_session, workspace, other_workspace):
    assert allocate_public_id(db_session, workspace.id) == "P0001"
    assert allocate_public_id(db_session, other_workspace.id) == "P0001"
    assert allocate_public_id(db_session, workspace.id) == "P0002"


def test_ensure_high_water_advances_but_never_lowers(db_session, workspace):
    allocate_public_id(db_session, workspace.id)  # -> 1
    assert ensure_high_water_at_least(db_session, workspace.id, 50) is True
    assert high_water(db_session, workspace.id) == 50
    assert ensure_high_water_at_least(db_session, workspace.id, 10) is False
    assert allocate_public_id(db_session, workspace.id) == "P0051"


def test_concurrent_allocation_never_duplicates(engine, workspace, _truncate):
    from montauk.services.workspace import get_or_create_workspace

    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with maker() as s:
        ws = get_or_create_workspace(s, "Concurrent")
        s.commit()
        ws_id = ws.id

    def worker() -> list[str]:
        out = []
        with maker() as s:
            for _ in range(10):
                out.append(allocate_public_id(s, ws_id))
                s.commit()
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = [f.result() for f in [pool.submit(worker) for _ in range(6)]]

    allocated = [pid for r in results for pid in r]
    assert len(allocated) == len(set(allocated)) == 60
    assert max(int(p[1:]) for p in allocated) == 60
