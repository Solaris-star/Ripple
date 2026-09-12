from __future__ import annotations

import os
from pathlib import Path

import pytest

from ripple.publishing import WorkflowError
from ripple import secrets as secret_store


def test_portable_secret_store_roundtrip_and_permissions(tmp_path, monkeypatch):
    key_path = tmp_path / "keys" / "ripple.key"
    monkeypatch.setenv("RIPPLE_SECRET_KEY_FILE", str(key_path))
    raw = b"fixture-secret-never-log"
    encrypted = secret_store._portable_protect(raw)
    assert encrypted != raw and encrypted.startswith(secret_store._PORTABLE_MAGIC)
    assert secret_store._portable_protect(encrypted, decrypt=True) == raw
    assert key_path.is_file() and key_path.read_bytes() != raw
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o077 == 0


def test_portable_secret_key_is_written_as_binary_bytes(tmp_path, monkeypatch):
    key_path = tmp_path / "binary" / "ripple.key"
    monkeypatch.setenv("RIPPLE_SECRET_KEY_FILE", str(key_path))
    expected = (b"line\n" * 6) + b"xy"
    assert len(expected) == secret_store._KEY_BYTES
    monkeypatch.setattr(secret_store.secrets, "token_bytes", lambda size: expected if size == secret_store._KEY_BYTES else b"n" * size)
    assert secret_store._portable_key() == expected
    assert key_path.read_bytes() == expected


def test_portable_secret_store_detects_tamper(tmp_path, monkeypatch):
    key_path = tmp_path / "ripple.key"
    monkeypatch.setenv("RIPPLE_SECRET_KEY_FILE", str(key_path))
    encrypted = bytearray(secret_store._portable_protect(b"fixture"))
    encrypted[-1] ^= 1
    with pytest.raises(WorkflowError, match="无法由当前机器解密"):
        secret_store._portable_protect(bytes(encrypted), decrypt=True)


def test_portable_secret_store_refuses_relative_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIPPLE_SECRET_KEY_FILE", "relative-secret.key")
    with pytest.raises(WorkflowError, match="绝对路径"):
        secret_store._portable_key()
