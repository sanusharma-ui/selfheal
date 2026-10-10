"""Developer-run gate regressions; intentionally not executed by the agent."""
from __future__ import annotations

import asyncio
from threading import Event, Thread

import pytest

from selfheal._coordination import InvocationGate
from selfheal import RepairTimeout, SelfHealConfig
from selfheal.timing import RepairDeadline


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout=3)


def waiting_writer(gate):
    # Observe admission under the same state lock used by the gate itself.
    with gate._condition:
        return gate._waiting_writers > 0


def test_concurrent_readers_finish_before_writer_and_new_readers_wait():
    gate = InvocationGate()
    writer_entered, release_writer = Event(), Event()
    order = []

    def writer():
        with gate.sync():
            order.append("writer")
            writer_entered.set()
            assert release_writer.wait(3)

    async def scenario():
        release_readers = asyncio.Event()
        readers_entered = []

        async def reader(name):
            async with gate.async_call():
                readers_entered.append(name)
                if name != "late":
                    await release_readers.wait()
                order.append(name)

        first = asyncio.create_task(reader("first"))
        second = asyncio.create_task(reader("second"))
        await until(lambda: len(readers_entered) == 2)
        thread = Thread(target=writer, daemon=True)
        thread.start()
        try:
            await until(lambda: waiting_writer(gate))
            late = asyncio.create_task(reader("late"))
            await asyncio.sleep(0.03)
            assert readers_entered == ["first", "second"]
            assert not writer_entered.is_set()
            release_readers.set()
            await asyncio.gather(first, second)
            await until(writer_entered.is_set)
            # Polling keeps the event loop alive while the writer owns its lock.
            assert not late.done()
            release_writer.set()
            await asyncio.wait_for(late, timeout=3)
        finally:
            release_readers.set()
            release_writer.set()
            thread.join(timeout=3)
        assert not thread.is_alive()

    asyncio.run(scenario())
    assert set(order[:2]) == {"first", "second"}
    assert order[2:] == ["writer", "late"]


def test_nested_reader_can_finish_while_writer_is_waiting():
    gate = InvocationGate()
    entered = Event()

    def writer():
        with gate.sync():
            entered.set()

    async def scenario():
        thread = None
        try:
            async with gate.async_call():
                assert gate.in_async_call()
                thread = Thread(target=writer, daemon=True)
                thread.start()
                await until(lambda: waiting_writer(gate))

                async def nested():
                    async with gate.async_call():
                        assert gate.in_async_call()
                        assert not entered.is_set()

                await asyncio.wait_for(asyncio.create_task(nested()), timeout=3)
                assert not entered.is_set()
            assert not gate.in_async_call()
            await until(entered.is_set)
        finally:
            if thread is not None:
                thread.join(timeout=3)

    asyncio.run(scenario())


def test_inherited_lease_expires_but_admitted_child_remains_counted():
    gate = InvocationGate()
    writer_entered = Event()

    def writer():
        with gate.sync():
            writer_entered.set()

    async def scenario():
        admitted = asyncio.Event()
        release = asyncio.Event()
        check_stale = asyncio.Event()
        stale_checked = asyncio.Event()

        async def admitted_child():
            async with gate.async_call():
                admitted.set()
                await release.wait()

        async def delayed_child():
            await check_stale.wait()
            assert not gate.in_async_call()
            stale_checked.set()

        async with gate.async_call():
            child = asyncio.create_task(admitted_child())
            stale = asyncio.create_task(delayed_child())
            await admitted.wait()
        thread = Thread(target=writer, daemon=True)
        thread.start()
        try:
            check_stale.set()
            await asyncio.wait_for(stale_checked.wait(), timeout=3)
            await until(lambda: waiting_writer(gate))
            assert not writer_entered.is_set()
            release.set()
            await asyncio.gather(child, stale)
            await until(writer_entered.is_set)
        finally:
            release.set()
            thread.join(timeout=3)

    asyncio.run(scenario())


def test_cancellation_of_waiting_and_active_readers_releases_only_owned_lease():
    gate = InvocationGate()
    writer_entered, release_writer = Event(), Event()

    def writer():
        with gate.sync():
            writer_entered.set()
            assert release_writer.wait(3)

    async def scenario():
        active = asyncio.Event()

        async def reader():
            async with gate.async_call():
                active.set()
                await asyncio.Event().wait()

        first = asyncio.create_task(reader())
        await active.wait()
        thread = Thread(target=writer, daemon=True)
        thread.start()
        try:
            await until(lambda: waiting_writer(gate))
            waiting = asyncio.create_task(reader())
            await asyncio.sleep(0.03)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not writer_entered.is_set()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await until(writer_entered.is_set)
        finally:
            release_writer.set()
            thread.join(timeout=3)
        async with gate.async_call():
            assert gate.in_async_call()
        assert gate._readers == 0

    asyncio.run(scenario())


def test_writer_is_reentrant_and_exception_releases_gate():
    gate = InvocationGate()
    with pytest.raises(ValueError):
        with gate.sync():
            with gate.sync():
                raise ValueError("callback failed")

    async def scenario():
        async with gate.async_call():
            assert gate.in_async_call()

    asyncio.run(scenario())


def test_writer_deadline_does_not_leave_readers_blocked(tmp_path):
    gate = InvocationGate()
    config = SelfHealConfig(tmp_path, hard_timeout=False, repair_timeout_seconds=1)
    outcome = []

    def writer():
        try:
            with RepairDeadline(config):
                with gate.sync():
                    outcome.append("unexpected admission")
        except RepairTimeout:
            outcome.append("timed out")

    async def scenario():
        thread = None
        try:
            async with gate.async_call():
                thread = Thread(target=writer, daemon=True)
                thread.start()
                await until(lambda: bool(outcome))
                assert outcome == ["timed out"]
                assert gate._waiting_writers == 0
                # A fresh context must enter after the timed-out writer exits.
                async def later():
                    async with gate.async_call():
                        return True
                from contextvars import Context
                later_task = asyncio.create_task(later(), context=Context())
                assert await asyncio.wait_for(later_task, timeout=3)
        finally:
            if thread is not None:
                thread.join(timeout=3)

    asyncio.run(scenario())


def test_waiting_writer_cancellation_check_releases_writer_preference():
    gate = InvocationGate()
    cancelled, stopped = Event(), Event()

    class CancelledRepair(Exception):
        pass

    def check():
        if cancelled.is_set():
            raise CancelledRepair()

    def writer():
        try:
            with gate.sync(check=check):
                pytest.fail("Cancelled writer must not enter")
        except CancelledRepair:
            stopped.set()

    async def scenario():
        thread = None
        try:
            async with gate.async_call():
                thread = Thread(target=writer, daemon=True)
                thread.start()
                await until(lambda: waiting_writer(gate))
                cancelled.set()
                # Reader remains active: cancellation polling must wake itself.
                await until(stopped.is_set)
                assert gate._waiting_writers == 0
        finally:
            cancelled.set()
            if thread is not None:
                thread.join(timeout=3)
        async with gate.async_call():
            assert gate.in_async_call()

    asyncio.run(scenario())
