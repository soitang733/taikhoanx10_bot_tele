"""Lock recovery checks for the scheduled daily pipeline."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily_data_pipeline


class DailyPipelineLockTests(unittest.TestCase):
    def test_dead_process_lock_is_recovered_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "update.lock"
            lock.write_text("pid=999999 started=now", encoding="utf-8")
            with patch.object(daily_data_pipeline.os, "kill", side_effect=OSError):
                with daily_data_pipeline.pipeline_lock(lock):
                    self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

    def test_live_process_lock_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "update.lock"
            lock.write_text(f"pid={os.getpid()} started=now", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Another daily update"):
                with daily_data_pipeline.pipeline_lock(lock, stale_hours=0):
                    pass


if __name__ == "__main__":
    unittest.main()
