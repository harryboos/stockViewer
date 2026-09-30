"""Coalesce overlapping start requests before their first asynchronous status read."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

_starts: dict[tuple, asyncio.Task] = {}


async def shared_start(scope: tuple, launch: Callable[[], Awaitable[dict]], tasks: set[asyncio.Task]) -> dict:
    key = (asyncio.get_running_loop(), *scope)
    task = _starts.get(key)
    if task is None or task.done():
        task = asyncio.create_task(launch())
        _starts[key] = task
        tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            tasks.discard(done)
            if _starts.get(key) is done:
                _starts.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
    # Losing one browser connection must not cancel another caller's launch,
    # or interrupt the handoff between a durable claim and its worker.
    return await asyncio.shield(task)
