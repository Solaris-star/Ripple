"""Machine-local encryption for Ripple connector secrets.

Windows keeps DPAPI compatibility. POSIX systems use an AES-256-GCM key stored in a
0600 machine-user file outside the repository. Ciphertexts are deliberately not
portable between machines; moving a Ripple workspace requires reconnecting accounts.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import secrets
import stat

from .publishing import WorkflowError

_PORTABLE_MAGIC = b"RIPPLE-AESGCM1\0"
_KEY_BYTES = 32


def _portable_key_path() -> Path:
    override = (os.environ.get("RIPPLE_SECRET_KEY_FILE") or "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise WorkflowError("RIPPLE_SECRET_KEY_FILE 必须是绝对路径。", 503)
        return path
    return Path.home() / ".config" / "ripple" / "secret.key"


def _portable_key() -> bytes:
    path = _portable_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            raw = path.read_bytes()
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            raise WorkflowError("Ripple 本机密钥文件无法读取。", 503) from None
        if len(raw) != _KEY_BYTES:
            raise WorkflowError("Ripple 本机密钥文件格式无效；请保留文件并人工排查。", 503)
        if os.name != "nt" and mode & 0o077:
            raise WorkflowError("Ripple 本机密钥文件权限过宽；请设置为 0600。", 503)
        return raw
    raw = secrets.token_bytes(_KEY_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short write while creating Ripple secret key")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except FileExistsError:
        return _portable_key()
    except OSError:
        raise WorkflowError("Ripple 无法创建本机密钥文件。", 503) from None
    return raw


def _portable_protect(data: bytes, decrypt: bool = False) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise WorkflowError("当前系统缺少 pycryptodome，无法安全保存连接凭据。", 503) from None
    key = _portable_key()
    if decrypt:
        if not data.startswith(_PORTABLE_MAGIC):
            raise WorkflowError("此凭据不是当前平台支持的加密格式，请重新连接账号。", 503)
        body = data[len(_PORTABLE_MAGIC):]
        if len(body) < 28:
            raise WorkflowError("加密凭据已损坏，请重新连接账号。", 503)
        nonce, tag, encrypted = body[:12], body[12:28], body[28:]
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(encrypted, tag)
        except (ValueError, KeyError):
            raise WorkflowError("加密凭据无法由当前机器解密，请重新连接账号。", 503) from None
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    encrypted, tag = cipher.encrypt_and_digest(data)
    return _PORTABLE_MAGIC + nonce + tag + encrypted


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _windows_dpapi(data: bytes, decrypt: bool = False) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise WorkflowError("Windows DPAPI 不可用，配置未保存。", 503)
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def protect(data: bytes, decrypt: bool = False) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("secret payload must be bytes")
    raw = bytes(data)
    if os.name == "nt":
        return _windows_dpapi(raw, decrypt)
    return _portable_protect(raw, decrypt)
