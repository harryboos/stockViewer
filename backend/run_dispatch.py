"""Shared cache eligibility and cancellation-safe dispatch for AI research runs."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import database

_starts: dict[tuple, asyncio.Task] = {}
logger = logging.getLogger(__name__)


def current_run(namespace: str, run_date: str, model: str, prompt_version: str, *,
                include_result: bool = True) -> dict | None:
    current = database.read_ai_run(namespace, run_date, include_result=include_result)
    if current and (current["status"] == "running" or (
        current.get("promptVersion") == prompt_version and current.get("model") == model
    )):
        return current
    return None


def track_task(task: asyncio.Task, tasks: set[asyncio.Task]) -> None:
    """Keep a worker alive and retrieve failures even if every browser disconnects."""
    tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        tasks.discard(done)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                # Provider/database details can contain private inputs; log only the type.
                logger.warning("research_task_failed error_type=%s", type(error).__name__)

    task.add_done_callback(finished)


async def launch_claimed_run(namespace: str, model: str, run_date: str, prompt_version: str, force: bool,
                             execute: Callable[[str], Awaitable[None]], tasks: set[asyncio.Task]) -> None:
    """Called inside shared_start's shield so a durable claim always gets its worker."""
    token = await asyncio.to_thread(database.start_ai_run, namespace, model, run_date, prompt_version, force)
    if token:
        track_task(asyncio.create_task(execute(token)), tasks)


async def shared_start(scope: tuple, launch: Callable[[], Awaitable[dict]], tasks: set[asyncio.Task]) -> dict:
    key = (asyncio.get_running_loop(), *scope)
    task = _starts.get(key)
    if task is None or task.done():
        task = asyncio.create_task(launch())
        _starts[key] = task
        track_task(task, tasks)

        def finished(done: asyncio.Task) -> None:
            if _starts.get(key) is done:
                _starts.pop(key, None)

        task.add_done_callback(finished)
    # Losing one browser connection must not cancel another caller's launch,
    # or interrupt the handoff between a durable claim and its worker.
    return await asyncio.shield(task)
