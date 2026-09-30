from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import concept_ai, concept_forecast, database
from backend.forecast_data import forecast_window


class ConcurrentStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target in (patch.object(database, "DATABASE_PATH", Path(directory.name) / "test.sqlite3"),
                       patch.dict(os.environ, {"GLM_API_KEY": "test-only"})):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()

    async def check_shared_start(self, module, namespace, getter, start):
        original_claim = database.start_ai_run
        initial = getter()
        first_finished = threading.Event()
        guard = threading.Lock()
        claims = 0

        def delayed_claim(*args, **kwargs):
            nonlocal claims
            with guard:
                claims += 1
                position = claims
            if position > 1:
                first_finished.wait(2)
            return original_claim(*args, **kwargs)

        async def finish_fast(day, key, token):
            await asyncio.to_thread(database.finish_ai_run, namespace, day,
                                   {"summary": "测试快速完成的结果", "concepts": [], "window": forecast_window(day)},
                                   None, token)
            first_finished.set()

        # Both overlapping HTTP requests have observed the old status. The
        # second SQLite claim runs after the first worker has already finished.
        with (patch.object(module, getter.__name__, return_value=initial),
              patch.object(database, "start_ai_run", side_effect=delayed_claim),
              patch.object(module, "_execute", side_effect=finish_fast) as worker):
            await asyncio.gather(start(True), start(True))
            while module._tasks:
                await asyncio.gather(*list(module._tasks))
            self.assertEqual(worker.call_count, 1)
            with database.connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_attempts").fetchone()[0], 1)
            # A later explicit regeneration is still a new request.
            await start(True)
            while module._tasks:
                await asyncio.gather(*list(module._tasks))
            self.assertEqual(worker.call_count, 2)

    async def test_overlapping_forecast_refreshes_do_not_launch_again_after_fast_completion(self):
        await self.check_shared_start(concept_forecast, "forecast:glm", concept_forecast.get_forecast_run,
                                      concept_forecast.start_forecast_run)

    async def test_overlapping_concept_refreshes_do_not_launch_again_after_fast_completion(self):
        await self.check_shared_start(concept_ai, "concept:glm", concept_ai.get_concept_run,
                                      concept_ai.start_concept_run)
