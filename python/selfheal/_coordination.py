"""Coordinate coroutine traffic with synchronous activation transactions.

Coroutine calls are concurrent readers. A synchronous transaction waits for
every admitted call to finish, then owns the gate until confirmation/recovery
finishes. Waiting writers stop new unrelated readers; nested calls can join an
active reader so a parent awaiting its child cannot deadlock that writer.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Condition, RLock, get_ident

from .timing import current_deadline


@dataclass(slots=True)
class _ReaderLease:
    active: bool = True


class InvocationGate:
    """One runtime's reader/writer gate; synchronous owners are reentrant.

    ``sync`` must not be entered on an application event-loop thread. The public
    runtime rejects that usage before reaching this gate. Async admission never
    occupies an executor thread or waits synchronously for a writer.
    """

    def __init__(self):
        self._condition = Condition(RLock())
        self._readers = 0
        self._waiting_writers = 0
        self._writer_thread = None
        self._lease = ContextVar("selfheal_reader_lease", default=None)

    def in_async_call(self) -> bool:
        """Whether this context descends from a still-running protected call.

        A child task inherits the lease object, not permanent permission. Once
        that invocation exits, a child which has not entered must queue normally.
        An already-admitted child owns its own counted lease until it exits.
        """
        lease = self._lease.get()
        return lease is not None and lease.active

    def owns_sync(self) -> bool:
        """Same-thread sync dispatch may reenter its existing exclusive lease."""
        return self._writer_thread == get_ident()

    @contextmanager
    def sync(self, check=None):
        budget = current_deadline()
        owner = get_ident()

        def check_wait():
            if check is not None:
                check()
            if budget is not None:
                budget.check("application gate")

        if self._writer_thread == owner or (budget is None and check is None):
            self._condition.acquire()
        else:
            while True:
                check_wait()
                delay = 0.05 if budget is None else min(0.05, budget.remaining())
                if self._condition.acquire(timeout=delay):
                    break
        try:
            if self._writer_thread == owner:
                yield
                return
            self._waiting_writers += 1
            try:
                while self._readers:
                    check_wait()
                    delay = 0.05 if budget is None else min(0.05, budget.remaining())
                    self._condition.wait(delay)
                check_wait()
            finally:
                self._waiting_writers -= 1
                self._condition.notify_all()
            self._writer_thread = owner
            try:
                yield
            finally:
                self._writer_thread = None
                self._condition.notify_all()
        finally:
            self._condition.release()

    @asynccontextmanager
    async def async_call(self):
        budget = current_deadline()
        lease = None
        while lease is None:
            if budget is not None:
                budget.check("application gate")
            # RLock admission must be nonblocking even when a writer performs
            # native verification. No executor slot is consumed by a waiter.
            if self._condition.acquire(blocking=False):
                try:
                    nested = self.in_async_call()
                    if self._writer_thread is None and (not self._waiting_writers or nested):
                        lease = _ReaderLease()
                        self._readers += 1
                finally:
                    self._condition.release()
            if lease is None:
                delay = 0.01 if budget is None else min(0.01, budget.remaining())
                await asyncio.sleep(delay)
        token = self._lease.set(lease)
        try:
            yield
        finally:
            # A writer cannot own the gate while this counted reader is active.
            # Release synchronously so cancellation cannot strand a reservation.
            with self._condition:
                lease.active = False
                self._readers -= 1
                self._condition.notify_all()
            self._lease.reset(token)
