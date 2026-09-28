"""保存本机工具凭据，使受管 Runtime 在 Web 服务重启后仍能连接。"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import uuid

from filelock import FileLock

from .secrets import protect


def agent_tool_token(private_dir: Path) -> str:
    path = private_dir.resolve() / "agent-tool-auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        if path.exists():
            # 解密失败时不自动覆盖，避免已运行进程再次失去连接。
            value = json.loads(path.read_text(encoding="utf-8"))
            token = protect(base64.b64decode(value["protected"], validate=True), decrypt=True).decode("ascii")
            if len(token) < 40:
                raise ValueError("本机 Agent 工具凭据无效。")
            return token
        token = secrets.token_urlsafe(32)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump({"protected": base64.b64encode(protect(token.encode("ascii"))).decode("ascii")}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return token
