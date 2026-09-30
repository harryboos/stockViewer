"""Bounded shared workers: timeouts must not create another pool of orphaned requests."""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

MAX_WORKERS = 8
BATCH_WORKERS = 4
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="research-market")
_capacity = threading.BoundedSemaphore(MAX_WORKERS)
# Broad comparison/rotation scans cannot occupy every slot needed by forecasts.
_background_capacity = threading.BoundedSemaphore(MAX_WORKERS - 2)
HISTORY_WAITING_KINDS = {"timeout", "busy", "not_started", "unavailable", "failed"}


class BatchResult(dict):
    """Successful values plus scheduling facts; omissions are not missing prices."""
    def __init__(self):
        super().__init__()
        self.attempted: set = set()
        self.errors: dict = {}
        self.paused = False


def history_result(values: dict, item) -> dict:
    if item in values:
        value = values[item]
        if isinstance(value, dict) and not value.get("rows") and value.get("error") and not value.get("errorKind"):
            return {**value, "errorKind": "unavailable"}
        return value
    kind = getattr(values, "errors", {}).get(item, "not_started")
    messages = {
        "timeout": "行情读取超过本批等待时间；请求已发出，将在下次核对重试",
        "busy": "行情后台正在处理其他请求；本项尚未开始查询，将在下次核对继续",
        "not_started": "本批时间已用完；本项尚未开始查询，将在下次核对继续",
        "failed": "行情读取过程失败；未取得可核对日线，将在下次核对重试",
    }
    return {"rows": [], "errorKind": kind, "error": messages.get(kind, messages["failed"])}


def fetch_batch(items: list, fetch, seconds: float = 180, *, background: bool = False, progress=None, stop_when=None) -> BatchResult:
    ordered = list(dict.fromkeys(items))
    if not ordered:
        return BatchResult()
    remaining_items = iter(ordered)
    deadline = time.monotonic() + seconds
    active, values = {}, BatchResult()
    exhausted = False
    completed = 0
    try:
        while time.monotonic() < deadline:
            while not exhausted and len(active) < BATCH_WORKERS:
                if background and not _background_capacity.acquire(blocking=False):
                    break
                if not _capacity.acquire(blocking=False):
                    if background:
                        _background_capacity.release()
                    break
                def release(done=None):
                    _capacity.release()
                    if background:
                        _background_capacity.release()
                try:
                    item = next(remaining_items)
                except StopIteration:
                    exhausted = True
                    release()
                    break
                try:
                    future = _pool.submit(fetch, item)
                except BaseException:
                    release()
                    raise
                values.attempted.add(item)
                exhausted = len(values.attempted) == len(ordered)
                future.add_done_callback(release)
                active[future] = item
            if not active:
                if exhausted:
                    break
                # Wait for a shared slot within this batch's budget, without
                # allocating another pool or queuing hundreds of futures.
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                continue
            done, _ = wait(active, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                item = active.pop(future)
                try:
                    values[item] = future.result()
                except Exception:
                    values.errors[item] = "failed"
                completed += 1
                if progress:
                    progress(completed, len(ordered))
            if stop_when:
                values.paused = bool(stop_when(values))
                exhausted = len(values.attempted) == len(ordered) or values.paused
    finally:
        for future, item in active.items():
            if future.cancel():
                values.attempted.discard(item)
            elif future.done():
                try:
                    values[item] = future.result()
                except Exception:
                    values.errors[item] = "failed"
            else:
                values.errors[item] = "timeout"
            # Running requests keep their slot until they really finish.
        for item in ordered:
            if item not in values and item not in values.errors:
                values.errors[item] = "not_started" if values.attempted else "busy"
    return values
