"""Bounded shared workers: timeouts must not create another pool of orphaned requests."""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

MAX_WORKERS = 8
BATCH_WORKERS = 4
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="research-market")
_capacity = threading.BoundedSemaphore(MAX_WORKERS)


def fetch_batch(items: list, fetch, seconds: float = 180) -> dict:
    remaining_items = iter(dict.fromkeys(items))
    deadline = time.monotonic() + seconds
    active, values = {}, {}
    exhausted = False
    try:
        while time.monotonic() < deadline:
            while not exhausted and len(active) < BATCH_WORKERS and _capacity.acquire(blocking=False):
                try:
                    item = next(remaining_items)
                except StopIteration:
                    exhausted = True
                    _capacity.release()
                    break
                try:
                    future = _pool.submit(fetch, item)
                except BaseException:
                    _capacity.release()
                    raise
                future.add_done_callback(lambda done: _capacity.release())
                active[future] = item
            if not active:
                # All shared workers are occupied. The next scheduled batch retries
                # missing items; no unbounded work queue or new thread pool is created.
                break
            done, _ = wait(active, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                item = active.pop(future)
                try:
                    values[item] = future.result()
                except Exception:
                    continue
    finally:
        for future in active:
            future.cancel()  # Running requests keep their slot until they really finish.
    return values
