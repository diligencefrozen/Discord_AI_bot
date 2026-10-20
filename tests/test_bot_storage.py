from __future__ import annotations

import pickle
import shutil
import uuid
import threading
import unittest
from pathlib import Path

from bot_storage import PickleStore


class PickleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / f".test_storage_{uuid.uuid4().hex}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_save_is_atomic_and_keeps_legacy_pickle(self):
        path = self.root / "daily_xp_data.pkl"
        store = PickleStore(path, max_backups=2, validator=lambda value: isinstance(value, dict))
        first = {1: {"xp": 10}}
        second = {1: {"xp": 20}}
        store.save(first)
        store.save(second)
        self.assertEqual(path.read_bytes()[:2], b"\x80\x04")
        with path.open("rb") as handle:
            self.assertEqual(pickle.load(handle), second)
        self.assertTrue(store._backup_path(1).exists())
        with store._backup_path(1).open("rb") as handle:
            self.assertEqual(pickle.load(handle), first)

    def test_corrupt_primary_recovers_backup_and_quarantines(self):
        path = self.root / "achievements_data.pkl"
        store = PickleStore(path, max_backups=2, validator=lambda value: isinstance(value, dict))
        store.save({"user": {"unlocked": []}})
        store.save({"user": {"unlocked": ["first"]}})
        path.write_bytes(b"not a pickle")
        self.assertEqual(store.load(), {"user": {"unlocked": []}})
        self.assertTrue(list(self.root.glob("achievements_data.pkl.corrupt.*")))

    def test_invalid_schema_falls_back_to_default(self):
        path = self.root / "daily_xp_data.pkl"
        store = PickleStore(path, validator=lambda value: isinstance(value, dict))
        path.write_bytes(pickle.dumps(["wrong schema"]))
        self.assertEqual(store.load(default={}), {})

    def test_invalid_current_does_not_replace_good_backup(self):
        path = self.root / "daily_xp_data.pkl"
        store = PickleStore(path, max_backups=2, validator=lambda value: isinstance(value, dict))
        store.save({"value": 1})
        store.save({"value": 2})
        path.write_bytes(b"truncated")
        store.save({"value": 3})
        with store._backup_path(1).open("rb") as handle:
            self.assertEqual(pickle.load(handle), {"value": 1})

    def test_single_writer_serializes_concurrent_updates(self):
        path = self.root / "daily_xp_data.pkl"
        store = PickleStore(path, max_backups=4)
        errors: list[Exception] = []

        def write(value: int) -> None:
            try:
                store.save({"value": value})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIn(store.load()["value"], range(12))
        self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
