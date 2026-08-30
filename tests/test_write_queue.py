import asyncio
import time

import pytest

from montauk.write_queue import WriteQueue


class TestWriteQueue:
    @pytest.mark.asyncio
    async def test_returns_operation_result(self, tmp_path):
        queue = WriteQueue(tmp_path)
        result = await queue.submit(lambda: 42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_exception_propagates(self, tmp_path):
        queue = WriteQueue(tmp_path)

        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            await queue.submit(boom)

    @pytest.mark.asyncio
    async def test_concurrent_submissions_never_overlap(self, tmp_path):
        queue = WriteQueue(tmp_path)
        active = 0
        max_concurrent = 0
        order: list[int] = []

        def make_op(n: int):
            def op():
                nonlocal active, max_concurrent
                active += 1
                max_concurrent = max(max_concurrent, active)
                time.sleep(0.02)  # simulate file/sqlite IO
                order.append(n)
                active -= 1
                return n

            return op

        results = await asyncio.gather(*(queue.submit(make_op(i)) for i in range(8)))

        assert max_concurrent == 1  # never more than one operation running at once
        assert sorted(results) == list(range(8))
        assert sorted(order) == list(range(8))

    @pytest.mark.asyncio
    async def test_creates_lock_file_under_data_dir(self, tmp_path):
        queue = WriteQueue(tmp_path)
        await queue.submit(lambda: None)
        assert (tmp_path / ".montauk.lock").exists()

    @pytest.mark.asyncio
    async def test_serializes_across_two_independent_queue_instances_pointing_at_same_dir(self, tmp_path):
        # Defense-in-depth check: even two separate WriteQueue objects (each
        # with their own asyncio.Lock) sharing a data_dir still serialize
        # via the OS-level flock underneath.
        queue_a = WriteQueue(tmp_path)
        queue_b = WriteQueue(tmp_path)
        active = 0
        max_concurrent = 0

        def make_op():
            def op():
                nonlocal active, max_concurrent
                active += 1
                max_concurrent = max(max_concurrent, active)
                time.sleep(0.02)
                active -= 1

            return op

        await asyncio.gather(
            *(queue_a.submit(make_op()) for _ in range(4)),
            *(queue_b.submit(make_op()) for _ in range(4)),
        )
        assert max_concurrent == 1
