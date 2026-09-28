"""仅向聊天界面传递图片工具状态和受控产物路径。"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
import time
from typing import Any, Callable


def _objects(value: Any, depth: int = 0):
    if depth > 5:
        return
    if isinstance(value, str) and len(value) <= 64 * 1024:
        try:
            yield from _objects(json.loads(value), depth + 1)
        except (ValueError, TypeError):
            pass
    elif isinstance(value, dict):
        yield value
        for key in ("result", "output", "structuredContent", "structured_content", "rawOutput", "content", "text"):
            if key in value:
                yield from _objects(value[key], depth + 1)
    elif isinstance(value, list):
        for row in value[:20]:
            yield from _objects(row, depth + 1)


def image_path(value: Any) -> str | None:
    for row in _objects(value):
        raw = row.get("path")
        if row.get("kind") not in {"image", "generated_media"} or not isinstance(raw, str):
            continue
        path = PurePosixPath(raw)
        if (not raw or len(raw) > 1000 or "\\" in raw or ":" in raw or path.is_absolute()
                or any(part in {".", ".."} or part.startswith((".", "_")) for part in raw.split("/"))
                or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}):
            continue
        return raw
    return None


class MediaTaskTracker:
    def __init__(self):
        self.tasks: dict[str, dict] = {}

    def update(self, call_id: str, name: str, status: str, output: Any,
               emit: Callable[[str, str], None], *, error: Any = None) -> None:
        identity = str(call_id or name)[:200]
        previous = self.tasks.get(identity)
        # 后续 ACP 更新可能省略标题，只接受已识别的调用标识。
        if previous is None and not re.search(r"(?:^|[^a-zA-Z0-9])ripple_generate_image(?:$|[^a-zA-Z0-9_])", name):
            return
        now = int(time.time() * 1000)
        state = str(status).lower()
        state = {"in_progress": "running", "success": "completed", "succeeded": "completed",
                 "error": "failed", "cancelled": "interrupted", "canceled": "interrupted"}.get(state, state)
        if state not in {"pending", "running", "completed", "failed", "interrupted"}:
            state = previous["status"] if previous else "running"
        failed_output = any(row.get("isError") is True or row.get("is_error") is True or row.get("error") for row in _objects(output))
        if error or failed_output:
            state = "failed"
        row = {"id": identity, "tool": "ripple_generate_image", "status": state,
               "started_at": previous["started_at"] if previous else now}
        if state in {"completed", "failed", "interrupted"}:
            row["finished_at"] = (previous or {}).get("finished_at", now)
            path = image_path(output) or (previous or {}).get("path")
            if state == "completed" and path:
                row["path"] = path
            elif state == "completed":
                row.update(status="interrupted", error="工具已结束，但没有返回可用的图片产物。")
            elif state == "failed":
                # 工具原始响应可能包含请求参数和凭据，不将其原样写入浏览器。
                row["error"] = "图片工具执行失败，没有生成可用图片。可重试图片步骤，或上传已有图片。"
            else:
                row["error"] = "图片生成已中断，没有收到完成结果。"
        if row != previous:
            self.tasks[identity] = row
            emit("media", json.dumps(row, ensure_ascii=False, sort_keys=True))
