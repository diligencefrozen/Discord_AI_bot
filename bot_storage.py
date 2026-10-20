"""Durable, backward-compatible Pickle storage helpers.

The bot keeps its historical ``daily_xp_data.pkl`` and
``achievements_data.pkl`` formats.  :class:`PickleStore` adds atomic writes,
rotating backups, validation and recovery without changing the serialized
objects or introducing a database.
"""
from __future__ import annotations

import logging
import os
import pickle
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


# A process-wide lock serializes all writes made by this module.  RLock also
# allows a validator or caller to perform a nested store operation safely.
_WRITE_LOCK = threading.RLock()


class PickleStore:
    """Read and atomically write one legacy Pickle file.

    ``path`` remains the original file name.  Backups are adjacent files named
    ``<path>.bak.1`` through ``<path>.bak.N``.  The newest backup is the copy
    of the file that was replaced by the latest successful write.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_backups: int = 3,
        validator: Callable[[Any], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_backups < 0:
            raise ValueError("max_backups must be non-negative")
        self.path = Path(path)
        self.max_backups = max_backups
        self.validator = validator
        self.logger = logger or logging.getLogger(__name__)

    def _backup_path(self, generation: int) -> Path:
        return Path(f"{self.path}.bak.{generation}")

    def _valid(self, value: Any) -> bool:
        return self.validator is None or bool(self.validator(value))

    def save(self, value: Any) -> None:
        """Atomically persist *value*, retaining prior generations.

        The temporary file is created beside the target so ``os.replace`` is
        atomic on the same filesystem.  ``fsync`` is performed before and
        after replacement when supported by the platform.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            temp_name: str | None = None
            try:
                temp_path = self.path.with_name(
                    f".{self.path.name}.{uuid.uuid4().hex}.tmp"
                )
                temp_name = str(temp_path)
                with temp_path.open("wb") as handle:
                    pickle.dump(value, handle, protocol=4)
                    handle.flush()
                    os.fsync(handle.fileno())

                # Preserve the current file only when it is a valid Pickle.  A
                # truncated/invalid current file must never overwrite a good
                # backup generation during a later save.
                current_valid = False
                if self.path.exists():
                    try:
                        self._read(self.path)
                        current_valid = True
                    except (OSError, EOFError, pickle.PickleError, ValueError, TypeError, AttributeError) as exc:
                        self.logger.error("Skipping invalid current pickle backup %s: %s", self.path, exc)

                if current_valid:
                    for generation in range(self.max_backups, 1, -1):
                        older = self._backup_path(generation - 1)
                        newer = self._backup_path(generation)
                        if older.exists():
                            os.replace(older, newer)
                    if self.max_backups:
                        shutil.copy2(self.path, self._backup_path(1))

                os.replace(temp_name, self.path)
                temp_name = None
                directory_fd = None
                # Directory fsync is useful on POSIX, but opening a directory
                # this way can block or fail on Windows filesystems.
                if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                    try:
                        directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
                    except OSError:
                        directory_fd = None
                if directory_fd is not None:
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                if temp_name:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass

    def _read(self, candidate: Path) -> Any:
        with candidate.open("rb") as handle:
            value = pickle.load(handle)
        if not self._valid(value):
            raise ValueError(f"validation failed for {candidate}")
        return value

    def _quarantine(self, candidate: Path) -> None:
        if not candidate.exists():
            return
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        quarantined = candidate.with_name(f"{candidate.name}.corrupt.{stamp}.{os.getpid()}")
        try:
            os.replace(candidate, quarantined)
            self.logger.warning("Quarantined corrupt pickle %s as %s", candidate, quarantined)
        except OSError:
            self.logger.exception("Could not quarantine corrupt pickle %s", candidate)

    def load(self, default: Any = None) -> Any:
        """Load the primary file, falling back to the newest valid backup.

        Corrupt or schema-invalid files are quarantined.  A recovered backup is
        copied back through :meth:`save`, creating a fresh valid primary while
        preserving the original Pickle object semantics.
        """
        with _WRITE_LOCK:
            candidates = [self.path] + [
                self._backup_path(generation)
                for generation in range(1, self.max_backups + 1)
            ]
            primary_error: Exception | None = None
            for index, candidate in enumerate(candidates):
                if not candidate.exists():
                    continue
                try:
                    value = self._read(candidate)
                    if index:
                        self.logger.warning("Recovered %s from backup %s", self.path, candidate)
                        # Save outside _read but inside the process-wide lock.
                        self.save(value)
                    return value
                except (OSError, EOFError, pickle.PickleError, ValueError, TypeError, AttributeError) as exc:
                    self.logger.error("Failed to load pickle %s: %s", candidate, exc)
                    if index == 0:
                        primary_error = exc
                    self._quarantine(candidate)
            if primary_error is not None:
                self.logger.error("No valid backup found for %s; using default", self.path)
            return default


def load_pickle(
    path: str | os.PathLike[str],
    default: Any = None,
    *,
    validator: Callable[[Any], bool] | None = None,
    max_backups: int = 3,
) -> Any:
    """Convenience wrapper for loading a legacy Pickle file."""
    return PickleStore(path, max_backups=max_backups, validator=validator).load(default)


def save_pickle(
    path: str | os.PathLike[str],
    value: Any,
    *,
    max_backups: int = 3,
) -> None:
    """Convenience wrapper for atomically saving a legacy Pickle file."""
    PickleStore(path, max_backups=max_backups).save(value)
