"""Bounded, cross-process locked JSON persistence, matching Ripple's local storage.

A corrupt store is never replaced with an empty workspace. Readers and writers
share the same OS lock; atomic replacement avoids partial JSON after a crash.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
MAX_STORE_BYTES = 32 * 1024 * 1024


class StoreError(RuntimeError):
    pass


class JsonStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.path = self.directory / "state.json"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    @contextmanager
    def transaction(self, *, write: bool = True):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock, (self.directory / "state.lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            deadline = time.monotonic() + 10
            while True:
                lock.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise StoreError("工作区正忙，请稍后重试。") from None
                    time.sleep(0.025)
            try:
                state = {"schema": 1, "tasks": {}}
                if self.path.exists():
                    try:
                        if self.path.stat().st_size > MAX_STORE_BYTES:
                            raise ValueError("too large")
                        state = json.loads(self.path.read_text(encoding="utf-8"))
                        if state.get("schema") != 1 or not isinstance(state.get("tasks"), dict):
                            raise ValueError("invalid schema")
                    except (OSError, ValueError, TypeError, AttributeError):
                        raise StoreError("任务数据无法读取；已停止写入，请保留 state.json 排查。") from None
                previous = json.dumps(state, ensure_ascii=False, indent=2) if write and self.path.exists() else None
                yield state
                if write:
                    text = json.dumps(state, ensure_ascii=False, indent=2)
                    if text == previous:
                        return
                    if len(text.encode("utf-8")) > MAX_STORE_BYTES:
                        raise StoreError("本地任务数据已达到容量上限，请先归档。")
                    fd, name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.directory)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                            stream.write(text)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(name, self.path)
                    finally:
                        if os.path.exists(name):
                            os.unlink(name)
            finally:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
