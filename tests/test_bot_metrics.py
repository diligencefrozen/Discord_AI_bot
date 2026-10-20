from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from bot_metrics import MetricsStore


class MetricsStoreTests(unittest.TestCase):
    def test_aggregate_counters_never_store_prompt_data(self):
        root = Path.cwd() / f".test_metrics_{uuid.uuid4().hex}"
        root.mkdir()
        try:
            store = MetricsStore(root / "metrics.json")
            store.increment("intro_viewed")
            store.increment("feature_succeeded", 2)
            self.assertEqual(store.snapshot(), {"feature_succeeded": 2, "intro_viewed": 1})
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_invalid_counter_is_rejected(self):
        root = Path.cwd() / f".test_metrics_{uuid.uuid4().hex}"
        root.mkdir()
        try:
            store = MetricsStore(root / "metrics.json")
            with self.assertRaises(ValueError):
                store.increment("", 1)
            with self.assertRaises(ValueError):
                store.increment("feature_failed", -1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
