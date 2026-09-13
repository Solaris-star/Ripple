"""Minimal fail-closed ACP stdio client shared by Ripple Agent runtimes."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable

from .agent_runtime import AgentRuntimeError

_CONTENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_VERSION_ID_RE = re.compile(r"^[a-f0-9]{64}$")

class AcpProcessClient:
    """Small JSON-RPC stdio client for one isolated ACP turn."""

    def __init__(self, command: list[str], cwd: Path, *, env: dict[str, str] | None = None, label: str = "ACP Runtime"):
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.label = label
        self.process: subprocess.Popen | None = None
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._emit: Callable[[str, str], None] | None = None
        self._session_id = ""
        self._stderr: deque[str] = deque(maxlen=100)
        self._closed = threading.Event()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = dict(self.env) if self.env is not None else os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise AgentRuntimeError(f"无法启动 {self.label}。", 503) from exc
        threading.Thread(target=self._reader, daemon=True, name="ripple-acp-out").start()
        threading.Thread(target=self._stderr_reader, daemon=True, name="ripple-acp-err").start()
        result = self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "Ripple", "version": "0.2.7"},
        }, timeout=30)
        if not isinstance(result, dict):
            self.close()
            raise AgentRuntimeError(f"{self.label} 初始化失败。", 503)

    def _stderr_reader(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                line = line.strip()
                if line:
                    self._stderr.append(line[:800])
        except (OSError, ValueError):
            pass

    def _reader(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message) and "method" not in message:
                    try:
                        request_id = int(message["id"])
                    except (TypeError, ValueError):
                        continue
                    with self._pending_lock:
                        waiter = self._pending.get(request_id)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                method = str(message.get("method") or "")
                if "id" in message and method:
                    self._handle_agent_request(message)
                elif method == "session/update":
                    self._handle_session_update(message.get("params") if isinstance(message.get("params"), dict) else {})
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()

    def _send(self, value: dict) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AgentRuntimeError(f"{self.label} 已退出。", 502)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                process.stdin.write(raw)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AgentRuntimeError(f"{self.label} 通信失败。", 502) from exc

    def _handle_agent_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method == "session/request_permission":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": {"outcome": "cancelled"}}}
        else:
            # Ripple advertises no terminal/filesystem client capabilities. Any
            # unexpected reverse RPC is denied rather than delegated to the host.
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Ripple client capability unavailable"}}
        try:
            self._send(response)
        except AgentRuntimeError:
            pass

    @staticmethod
    def _content_draft(value: Any) -> dict | None:
        candidates: list[dict] = []
        if isinstance(value, dict):
            candidates.append(value)
            for key in ("result", "output", "structuredContent", "rawOutput"):
                child = value.get(key)
                if isinstance(child, dict):
                    candidates.append(child)
        elif isinstance(value, str) and len(value) <= 64 * 1024:
            try:
                child = json.loads(value)
                if isinstance(child, dict):
                    candidates.append(child)
            except (ValueError, TypeError):
                pass
        for row in candidates:
            if row.get("kind") != "content_draft":
                continue
            content_id = str(row.get("id") or "")
            version_id = str(row.get("version_id") or "")
            if _CONTENT_ID_RE.fullmatch(content_id) and _VERSION_ID_RE.fullmatch(version_id):
                return {"kind": "content_draft", "id": content_id, "version_id": version_id,
                        "title": str(row.get("title") or "")[:200], "status": "draft"}
        return None

    def _handle_session_update(self, params: dict) -> None:
        if self._session_id and str(params.get("sessionId") or "") not in {"", self._session_id}:
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = str(update.get("sessionUpdate") or "")
        emit = self._emit
        if emit is None:
            return
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update.get("content") if isinstance(update.get("content"), dict) else {}
            text = str(content.get("text") or "") if content.get("type") == "text" else ""
            if text:
                emit("token" if kind == "agent_message_chunk" else "thinking", text)
            return
        if kind in {"tool_call", "tool_call_update"}:
            title = str(update.get("title") or update.get("kind") or "Ripple MCP")[:160]
            status = str(update.get("status") or ("running" if kind == "tool_call" else "updated"))
            emit("activity", f"调用 {title} · {status}")
            artifact = self._content_draft(update.get("rawOutput"))
            if artifact:
                emit("artifact", json.dumps(artifact, ensure_ascii=False, sort_keys=True))

    def request(self, method: str, params: dict, *, timeout: float = 60) -> Any:
        self._next_id += 1
        request_id = self._next_id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + max(1.0, timeout)
            while time.monotonic() < deadline:
                try:
                    message = waiter.get(timeout=min(.25, max(.01, deadline - time.monotonic())))
                    if isinstance(message.get("error"), dict):
                        error = message["error"]
                        detail = str(error.get("message") or "").replace("\r", " ").replace("\n", " ")[:300]
                        raise AgentRuntimeError(f"{self.label} 请求失败：{method}" + (f"（{detail}）" if detail else ""), 502)
                    return message.get("result")
                except queue.Empty:
                    process = self.process
                    if process is None or process.poll() is not None:
                        raise AgentRuntimeError(f"{self.label} 在请求期间退出。", 502)
            raise AgentRuntimeError(f"{self.label} 请求超时：{method}", 504)
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def prompt(self, session_id: str, prompt: list[dict], emit: Callable[[str, str], None], *, timeout: float) -> dict:
        self._session_id = session_id
        self._emit = emit
        try:
            result = self.request("session/prompt", {"sessionId": session_id, "prompt": prompt}, timeout=timeout)
            return result if isinstance(result, dict) else {}
        finally:
            self._emit = None

    def cancel(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except AgentRuntimeError:
            pass

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=4)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

