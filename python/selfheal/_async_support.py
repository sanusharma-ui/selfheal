"""Bridge bounded repair workers to the application's owning asyncio loop."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar, copy_context
import inspect
from threading import Event, Thread

from .timing import RepairDeadline, RepairTimeout, current_deadline

operation = ContextVar("selfheal_async_operation", default=None)
callback_runtime = ContextVar("selfheal_callback_runtime", default=None)


class RepairCancelled(Exception):
    """Internal transaction abort; the caller still receives CancelledError."""


class AsyncUsageError(RuntimeError):
    """Integration errors must not be treated as application bugs to repair."""


def require_sync_api(async_name):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(f"This operation blocks; use {async_name} on an asyncio event loop")


def close_coroutine(value):
    if inspect.iscoroutine(value):
        value.close()


class AsyncOperation:
    def __init__(self, loop, runtime):
        self.loop = loop
        self.runtime = runtime
        self.cancelled = Event()
        # Accessed only by the owning loop, including request_cancel().
        self._callback = None

    def checkpoint(self):
        budget = current_deadline()
        if self.cancelled.is_set() and not (budget and budget.cleaning):
            raise RepairCancelled("Async caller cancelled; repair stopped safely.")

    def request_cancel(self):
        # Cancel the actual callback task once. Cancelling the cross-thread
        # Future can acknowledge cancellation BEFORE coroutine finally exits.
        first = not self.cancelled.is_set()
        self.cancelled.set()
        if first and self._callback is not None:
            self._callback.cancel()

    async def _consume(self, awaitable, budget, stage):
        token = callback_runtime.set(self.runtime)
        self._callback = asyncio.current_task()
        entered = False
        try:
            self.checkpoint()
            budget.check(stage)
            timeout = asyncio.timeout(budget.remaining())
            try:
                async with timeout:
                    entered = True
                    value = await awaitable
            except TimeoutError:
                if timeout.expired():
                    raise RepairTimeout(f"Repair deadline exceeded during {stage}") from None
                raise
            self.checkpoint()
            budget.check(stage)
            return value
        except asyncio.CancelledError:
            raise RepairCancelled("Async callback cancelled; repair stopped safely.") from None
        finally:
            if not entered:
                close_coroutine(awaitable)
            self._callback = None
            callback_runtime.reset(token)

    def await_result(self, awaitable, budget, stage):
        if not self.loop.is_running() or self.loop.is_closed():
            close_coroutine(awaitable)
            raise RuntimeError("The application's async loop is not running")
        consume = self._consume(awaitable, budget, stage)
        try:
            future = asyncio.run_coroutine_threadsafe(consume, self.loop)
        except BaseException:
            consume.close()
            close_coroutine(awaitable)
            raise
        # No thread-wide opcode trace may wrap this wait. The coroutine owns its
        # cooperative timeout; the independent watchdog handles a blocked loop.
        # Wait for actual coroutine cleanup before rollback or gate release.
        return future.result()


async def run_worker(runtime, callback):
    """At most one async API worker per runtime; never consume the app executor.

    Cancellation asks the transaction to stop at a checkpoint and drains it,
    even if the caller is cancelled again while rollback is in progress.
    """
    # Deliver an already-pending cancellation before starting any repair work.
    await asyncio.sleep(0)
    budget = RepairDeadline(runtime.config)
    while not runtime._async_worker_slot.acquire(blocking=False):
        budget.check("async worker admission")
        await asyncio.sleep(min(0.01, budget.remaining()))
    try:
        loop = asyncio.get_running_loop()
        bridge = AsyncOperation(loop, runtime)
        finished = loop.create_future()
        context = copy_context()

        def deliver(value, error):
            if error is None:
                finished.set_result(value)
            else:
                finished.set_exception(error)

        def work():
            token = operation.set(bridge)
            try:
                with budget:
                    value, error = callback(), None
            except BaseException as exc:
                value, error = None, exc
            finally:
                operation.reset(token)
            loop.call_soon_threadsafe(deliver, value, error)

        Thread(target=context.run, args=(work,), name="selfheal-async-repair", daemon=True).start()
        cancellation = None
        while not finished.done():
            try:
                await asyncio.shield(finished)
            except asyncio.CancelledError as exc:
                cancellation = exc
                bridge.request_cancel()
            except BaseException:
                break  # Retrieve the worker's exception below.
        if cancellation is not None:
            finished.exception()  # Mark any transaction failure as retrieved.
            raise cancellation
        return finished.result()
    finally:
        runtime._async_worker_slot.release()
