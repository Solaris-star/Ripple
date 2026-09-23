from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from ripple.store import JsonStore


def seed(store: JsonStore, value: int = 1) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"schema": 1, "tasks": {}, "value": value}), encoding="utf-8")


def test_read_only_transactions_reuse_parsed_snapshot(tmp_path, monkeypatch):
    store = JsonStore(tmp_path / "state")
    seed(store)
    calls = 0
    original = Path.read_text

    def counted(self, *args, **kwargs):
        nonlocal calls
        if self == store.path:
            calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    with store.transaction(write=False) as state:
        assert state["value"] == 1
    with store.transaction(write=False) as state:
        assert state["value"] == 1
    assert calls == 1


def test_write_invalidates_read_cache_and_preserves_new_value(tmp_path):
    store = JsonStore(tmp_path / "state")
    seed(store)
    with store.transaction(write=False) as state:
        assert state["value"] == 1
    assert store._read_state is not None
    with store.transaction() as state:
        state["value"] = 2
    assert store._read_state is None
    with store.transaction(write=False) as state:
        assert state["value"] == 2


def test_external_atomic_replace_invalidates_cached_signature(tmp_path):
    store = JsonStore(tmp_path / "state")
    seed(store)
    with store.transaction(write=False) as state:
        assert state["value"] == 1

    replacement = store.path.with_name("external.tmp")
    replacement.write_text(json.dumps({"schema": 1, "tasks": {}, "value": 3, "pad": "changed"}), encoding="utf-8")
    os.replace(replacement, store.path)

    with store.transaction(write=False) as state:
        assert state["value"] == 3


def test_read_cache_is_shared_only_by_read_contract(tmp_path):
    store = JsonStore(tmp_path / "state")
    seed(store)
    with store.transaction(write=False) as first:
        first_id = id(first)
    with store.transaction(write=False) as second:
        assert id(second) == first_id
        assert second["value"] == 1
