"""Platform-neutral interaction workspace for Ripple.

The interaction ledger is independent from platform login state. Managed Ripple
interaction flows operate only on capability-gated remote platform adapters.
Legacy imported/local records remain readable for compatibility but cannot create
new drafts or become remote writes. Existing Xiaohongshu interaction records are
migrated lazily without replaying their operation ids.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import re
import uuid
from typing import Any

from .catalog import NAMES
from .publishing import WorkflowError, fingerprint
from .tenancy import DEFAULT_WORKSPACE_ID

ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = ROOT / "skills" / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
import content_guard  # type: ignore  # noqa: E402

MAX_INTERACTIONS = 500
MAX_SOURCES = 24
MAX_COMMENTS = 200

# Capabilities are deliberately explicit. A legacy script existing in the repo is
# not sufficient evidence that the managed Ripple adapter is safe to expose.
_REMOTE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "xiaohongshu": {
        "read_contents": True,
        "read_comments": True,
        "reply": True,
        "comment": True,
        "delete": True,
        "refresh_result": True,
        "platform_verify": False,
        "adapters": {"native"},
    },
}

_QUESTION_RE = re.compile(r"[?？]|(?:怎么|如何|为啥|为什么|能不能|可以吗|多少|哪里|哪儿)")
_DEMAND_RE = re.compile(r"(?:求|想要|想买|链接|教程|同款|参数|价格|多少钱|怎么买|怎么做|推荐|哪里买|在哪买)")
_COMPLAINT_RE = re.compile(r"(?:差|垃圾|坑|避雷|踩雷|翻车|失望|难用|太贵|骗人|假货|后悔|劝退|退款|bug|卡顿)", re.I)
_POSITIVE_RE = re.compile(r"(?:喜欢|好用|推荐|值得|太棒|厉害|满意|惊喜|有用|干货|收藏|支持|谢谢|感谢|爱了|绝了)", re.I)
_NEGATIVE_RE = re.compile(r"(?:差|垃圾|坑|避雷|踩雷|翻车|失望|难用|骗人|假货|后悔|劝退|退款|讨厌|不值|烂)", re.I)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{1,30}|[\u4e00-\u9fff]{2,8}")
_STOPWORDS = {"这个", "那个", "可以", "就是", "真的", "感觉", "怎么", "为什么", "一下", "还是", "没有", "一个", "我们", "你们", "他们", "自己", "现在", "已经", "然后"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_platform(value: str) -> str:
    value = str(value or "").strip()
    if value == "generic":
        return value  # legacy read/filter compatibility only
    if value not in NAMES or value == "blog":
        raise WorkflowError("请选择受支持的社交平台。", 422)
    return value


def _capability(platform: str) -> dict[str, Any]:
    spec = _REMOTE_CAPABILITIES.get(platform, {})
    return {
        "platform": platform,
        "name": "通用 / 未知" if platform == "generic" else NAMES.get(platform, platform),
        "read_contents": bool(spec.get("read_contents")),
        "read_comments": bool(spec.get("read_comments")),
        "reply": bool(spec.get("reply")),
        "comment": bool(spec.get("comment")),
        "delete": bool(spec.get("delete")),
        "refresh_result": bool(spec.get("refresh_result")),
        "platform_verify": bool(spec.get("platform_verify")),
    }


def _analyze(comments: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(row.get("content") or "") for row in comments]
    positive: list[str] = []
    negative: list[str] = []
    neutral = 0
    demand = []
    complaint = []
    questions = []
    comment_labels: dict[str, list[str]] = {}
    tokens: Counter[str] = Counter()
    for row, text in zip(comments, texts):
        pos = len(_POSITIVE_RE.findall(text)); neg = len(_NEGATIVE_RE.findall(text))
        labels: list[str] = []
        if pos > neg:
            positive.append(text)
            labels.append("positive")
        elif neg > pos:
            negative.append(text)
            labels.append("negative")
        else:
            neutral += 1
        if _DEMAND_RE.search(text):
            demand.append(text)
            labels.append("demand")
        if _COMPLAINT_RE.search(text):
            complaint.append(text)
            if "negative" not in labels:
                labels.append("negative")
        if _QUESTION_RE.search(text):
            questions.append(text)
            labels.append("question")
        comment_labels[str(row.get("id") or "")] = labels
        for token in _TOKEN_RE.findall(text.lower()):
            if token not in _STOPWORDS:
                tokens[token] += 1
    total = len(texts)
    dist = {"positive": len(positive), "neutral": neutral, "negative": len(negative)}
    return {
        "engine": "ripple-local-lexicon",
        "approximate": True,
        "total": total,
        "unique_authors": len({str(row.get("nickname") or "") for row in comments if row.get("nickname")}),
        "sentiment": {
            "distribution": dist,
            "ratio": {key: round(value / total, 3) for key, value in dist.items()},
            "positive_examples": positive[:3],
            "negative_examples": negative[:3],
        },
        "keywords": [{"word": word, "count": count} for word, count in tokens.most_common(20)],
        "demands": {"count": len(demand), "examples": demand[:5]},
        "complaints": {"count": len(complaint), "examples": complaint[:5]},
        "questions": {"count": len(questions), "examples": questions[:5]},
        "comment_labels": comment_labels,
        "warning": "样本少于 20 条，比例仅供方向判断。" if total < 20 else "",
    }


class InteractionService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self.accounts = workspace.accounts

    def recover(self) -> int:
        """Reconcile in-flight interaction intent after a service restart, never replay it."""
        changed = 0
        with self.store.transaction() as state:
            rows = self._migrate(state)
            for row in rows.values():
                if row.get("status") != "dispatching":
                    continue
                row.update(status="unknown_result", updated_at=_now())
                changed += 1
                account = state.get("accounts", {}).get(str(row.get("account_id") or ""))
                operation = (account or {}).get("operation") or {}
                if account and operation.get("id") == row.get("operation_id") and operation.get("kind") == "interaction":
                    operation["state"] = "recovery_required"
                    account["operation"] = operation
        return changed

    def capabilities(self) -> dict[str, Any]:
        accounts = self.accounts.list()
        platforms = [p for p in NAMES if p != "blog"]
        items = []
        for platform in platforms:
            row = _capability(platform)
            linked = [a for a in accounts if a.get("platform") == platform]
            row["account_count"] = len(linked)
            row["connected_count"] = sum(a.get("status") == "connected" for a in linked)
            row["note"] = (
                "已接入 Ripple 托管评论同步与写操作；平台复核仍需人工检查。"
                if platform == "xiaohongshu" else
                "互动能力尚未接入；当前账号连接只用于已支持的其他产品能力。"
            )
            items.append(row)
        return {"items": items}

    def _migrate(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = state.setdefault("interactions", {})
        if not isinstance(rows, dict):
            rows = state["interactions"] = {}
        for item in rows.values():
            if isinstance(item, dict):
                item.setdefault("workspace_id", DEFAULT_WORKSPACE_ID)
        legacy = state.get("xhs_interactions", {})
        migrated = 0
        if isinstance(legacy, dict):
            for interaction_id, old in legacy.items():
                if interaction_id in rows or not isinstance(old, dict):
                    continue
                row = deepcopy(old)
                row.update({
                    "platform": "xiaohongshu",
                    "source_kind": "remote",
                    "source_id": "",
                    "target_id": str(old.get("note_id") or "")[:100],
                    "target_url": str(old.get("note_url") or "")[:2048],
                    "delivery": "remote",
                    "legacy_origin": "xhs_interactions",
                })
                rows[interaction_id] = row
                migrated += 1
        if isinstance(legacy, dict) and (migrated or not isinstance(state.get("interaction_migration"), dict)):
            state["interaction_migration"] = {
                "version": 1,
                "legacy_key": "xhs_interactions",
                "legacy_count": len(legacy),
                "last_migrated": migrated,
                "at": _now(),
            }
        return rows

    def _project(self, row: dict[str, Any]) -> dict[str, Any]:
        value = {key: deepcopy(item) for key, item in row.items() if key not in {"creation_key", "digest"}}
        # Compatibility aliases for the previous Xiaohongshu UI/API.
        value.setdefault("note_id", value.get("target_id", ""))
        value.setdefault("note_url", value.get("target_url", ""))
        return value

    @staticmethod
    def _project_source(row: dict[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in row.items() if key not in {"creation_key", "digest"}}

    def _get_row(self, interaction_id: str) -> dict[str, Any]:
        with self.store.transaction() as state:
            row = self._migrate(state).get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            return deepcopy(row)

    def list(self, *, platform: str = "", account_id: str = "", status: str = "", limit: int = 100) -> dict[str, Any]:
        # History remains readable even when the associated account is disconnected.
        if account_id:
            self.accounts.get(account_id)  # existence only; connection is intentionally not required
        if platform:
            platform = _clean_platform(platform)
        with self.store.transaction() as state:
            rows = list(self._migrate(state).values())
        selected = [row for row in rows if (not platform or row.get("platform") == platform)
                    and (not account_id or row.get("account_id") == account_id)
                    and (not status or row.get("status") == status)]
        selected.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        return {"items": [self._project(row) for row in selected[:max(1, min(limit, 200))]]}

    def _source_rows(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = state.setdefault("interaction_sources", {})
        if not isinstance(rows, dict):
            rows = state["interaction_sources"] = {}
        for item in rows.values():
            if isinstance(item, dict):
                item.setdefault("workspace_id", DEFAULT_WORKSPACE_ID)
        return rows

    def sources(self, *, platform: str = "", limit: int = 50) -> dict[str, Any]:
        if platform:
            platform = _clean_platform(platform)
        with self.store.transaction(write=False) as state:
            rows = state.get("interaction_sources", {}) if isinstance(state.get("interaction_sources", {}), dict) else {}
            items = [deepcopy(row) for row in rows.values() if not platform or row.get("platform") == platform]
        items.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        summaries = []
        for row in items[:max(1, min(limit, MAX_SOURCES))]:
            value = self._project_source(row)
            value.pop("comments", None)
            summaries.append(value)
        return {"items": summaries}

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self.store.transaction(write=False) as state:
            row = (state.get("interaction_sources", {}) or {}).get(source_id)
            if not isinstance(row, dict):
                raise WorkflowError("评论数据源不存在。", 404)
            return self._project_source(row)

    def analyze_source(self, source_id: str) -> dict[str, Any]:
        source = self.get_source(source_id)
        report = _analyze(source.get("comments", []))
        return {"source_id": source_id, "platform": source.get("platform", "generic"), "label": source.get("label", ""), **report}

    def _account_for_remote(self, account_id: str, platform: str, action: str, *, require_connected: bool,
                            require_idle: bool = False) -> dict[str, Any]:
        cap = _REMOTE_CAPABILITIES.get(platform) or {}
        if not cap.get(action):
            raise WorkflowError(f"{NAMES.get(platform, platform)} 的托管互动「{action}」尚未接入 Ripple。", 409)
        account = self.accounts.get(account_id)
        if account.get("platform") != platform:
            raise WorkflowError("互动任务的平台与账号不匹配。", 422)
        allowed_adapters = cap.get("adapters") or set()
        if allowed_adapters and account.get("adapter", "native") not in allowed_adapters:
            raise WorkflowError("该账号连接方式暂不支持托管互动。", 409)
        if require_connected and account.get("status") != "connected":
            raise WorkflowError("目标账号当前未连接；本地历史仍可查看，但远端同步/写入需要重新连接。", 409)
        operation = account.get("operation") or {}
        if require_idle and operation.get("state") in {"running", "recovery_required"}:
            if operation.get("state") == "recovery_required":
                raise WorkflowError("该账号有结果待核对的真实操作，请先处理回执后再同步或互动。", 409)
            raise WorkflowError("该账号正在执行登录、发布或互动操作，请稍后再试。", 429)
        return account

    def remote_contents(self, account_id: str, limit: int = 30) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        platform = str(account.get("platform") or "")
        self._account_for_remote(account_id, platform, "read_contents", require_connected=True, require_idle=True)
        if platform == "xiaohongshu":
            data = self.workspace.xhs_ops.notes(account_id, min(limit, 30))
            items = []
            for row in data.get("items", []):
                if not isinstance(row, dict):
                    continue
                items.append({
                    "id": str(row.get("note_id") or "")[:100],
                    "title": str(row.get("title") or "")[:200],
                    "url": str(row.get("url") or "")[:2048],
                    "metrics": deepcopy(row.get("metrics") or {}),
                })
            return {"platform": platform, "source": data.get("source"), "fetched_at": data.get("fetched_at"), "items": items}
        raise WorkflowError("该平台的作品同步尚未接入。", 409)

    def remote_comments(self, *, account_id: str, target_id: str = "", target_url: str = "", target_label: str = "", limit: int = 100) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        platform = str(account.get("platform") or "")
        self._account_for_remote(account_id, platform, "read_comments", require_connected=True, require_idle=True)
        if platform != "xiaohongshu":
            raise WorkflowError("该平台的评论同步尚未接入。", 409)
        data = self.workspace.xhs_ops.comments(account_id, note_id=target_id, url=target_url, limit=min(limit, 100))
        comments = [row for row in data.get("comments", []) if isinstance(row, dict)][:MAX_COMMENTS]
        note_id = str(data.get("note_id") or target_id)[:100]
        locator_public = ""
        try:
            _, locator = self.workspace.xhs_ops._locator(account_id, note_id=note_id, url=target_url)
            from .xhs_ops import _public_url
            locator_public = _public_url(locator)
        except WorkflowError:
            pass
        with self.store.transaction() as state:
            rows = self._source_rows(state)
            existing = next((row for row in rows.values() if row.get("kind") == "remote" and row.get("account_id") == account_id and row.get("target_id") == note_id), None)
            source = existing or {"id": uuid.uuid4().hex, "workspace_id": DEFAULT_WORKSPACE_ID, "created_at": _now()}
            source.update({
                "kind": "remote", "platform": platform, "label": str(target_label or note_id or "平台评论")[:120],
                "account_id": account_id, "account_label": str(account.get("label") or "")[:80],
                "target_id": note_id, "target_url": locator_public, "comments": comments,
                "count": len(comments), "updated_at": _now(),
            })
            rows[source["id"]] = source
            if len(rows) > MAX_SOURCES:
                for row in sorted(rows.values(), key=lambda item: item.get("updated_at", ""))[:len(rows) - MAX_SOURCES]:
                    if row["id"] != source["id"]:
                        rows.pop(row["id"], None)
            return self._project_source(source)

    def _clean_payload(self, kind: str, items: list[dict[str, Any]], text: str) -> dict[str, Any]:
        if kind == "reply":
            if not 1 <= len(items) <= 20:
                raise WorkflowError("一次请选择 1–20 条评论回复。", 422)
            clean = []
            for item in items:
                reply = str(item.get("reply") or "").strip()
                if not reply or len(reply) > 1000:
                    raise WorkflowError("回复内容不能为空且不能超过 1000 字符。", 422)
                findings = content_guard.scan(reply)
                if any(item.category in content_guard.BLOCK_CATEGORIES for item in findings):
                    raise WorkflowError("待发送内容包含疑似凭据或内部配置信息，请修改后再发送。", 422)
                clean.append({
                    "id": str(item.get("id") or "")[:100], "nickname": str(item.get("nickname") or "")[:80],
                    "content": str(item.get("content") or "")[:500], "reply": reply,
                })
            return {"items": clean}
        if kind == "delete":
            if not 1 <= len(items) <= 20:
                raise WorkflowError("一次请选择 1–20 条评论删除。", 422)
            clean = [{"id": str(item.get("id") or "")[:100], "nickname": str(item.get("nickname") or "")[:80],
                      "content": str(item.get("content") or "")[:500]} for item in items]
            if any(not row["id"] and (not row["nickname"] or not row["content"]) for row in clean):
                raise WorkflowError("删除目标需要评论 ID，或昵称 + 原评论内容用于定位。", 422)
            return {"items": clean}
        if kind == "comment":
            text = str(text or "").strip()
            if not text or len(text) > 1000:
                raise WorkflowError("评论内容不能为空且不能超过 1000 字符。", 422)
            findings = content_guard.scan(text)
            if any(item.category in content_guard.BLOCK_CATEGORIES for item in findings):
                raise WorkflowError("待发送内容包含疑似凭据或内部配置信息，请修改后再发送。", 422)
            return {"text": text}
        raise WorkflowError("不支持的互动任务类型。", 422)

    @staticmethod
    def _bind_source_items(source: dict[str, Any], kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if kind not in {"reply", "delete"}:
            return items
        comments = [row for row in source.get("comments", []) if isinstance(row, dict)]
        bound = []
        for item in items:
            item_id = str(item.get("id") or "")[:100]
            nickname = str(item.get("nickname") or "")[:80]
            content = str(item.get("content") or "")[:500]
            if item_id:
                candidates = [row for row in comments if str(row.get("id") or "") == item_id]
            else:
                candidates = [row for row in comments if str(row.get("nickname") or "") == nickname
                              and content and str(row.get("content") or "").startswith(content)]
            if len(candidates) != 1:
                raise WorkflowError("互动目标不属于当前评论数据源，或无法唯一定位。请刷新评论后重新选择。", 409)
            original = candidates[0]
            row = {"id": str(original.get("id") or "")[:100],
                   "nickname": str(original.get("nickname") or "")[:80],
                   "content": str(original.get("content") or "")[:500]}
            if kind == "reply":
                row["reply"] = str(item.get("reply") or "")
            bound.append(row)
        return bound

    def create_draft(self, *, platform: str, account_id: str = "", source_id: str = "", target_id: str = "",
                     target_url: str = "", kind: str, items: list[dict[str, Any]] | None = None, text: str = "",
                     idempotency_key: str) -> dict[str, Any]:
        platform = _clean_platform(platform)
        if platform == "generic":
            raise WorkflowError("通用/未知来源不支持新建互动任务。请选择真实平台账号。", 422)
        source = self.get_source(source_id) if source_id else None
        if source and source.get("kind") != "remote":
            raise WorkflowError("历史导入评论只读保留，不能再创建互动草稿。", 409)
        delivery = "remote"
        account_label = ""
        if source:
            if platform != source.get("platform"):
                raise WorkflowError("互动草稿的平台与评论记录不匹配。", 422)
            items = self._bind_source_items(source, kind, list(items or []))
            account_id = str(source.get("account_id") or "")
            target_id = str(source.get("target_id") or "")
            target_url = str(source.get("target_url") or "")
        if not account_id:
            raise WorkflowError("互动草稿需要绑定具体平台账号。", 422)
        account = self._account_for_remote(account_id, platform, kind, require_connected=False)
        account_label = str(account.get("label") or "")[:80]
        if platform == "xiaohongshu":
            target_id, locator = self.workspace.xhs_ops._locator(account_id, note_id=target_id, url=target_url)
            from .xhs_ops import _public_url
            target_url = _public_url(locator)
        payload = self._clean_payload(kind, list(items or []), text)
        key = str(idempotency_key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", key):
            raise WorkflowError("互动草稿幂等键无效。", 422)
        digest = fingerprint({"platform": platform, "account_id": account_id, "source_id": source_id,
                              "target_id": target_id, "delivery": delivery, "kind": kind, **payload})
        with self.store.transaction() as state:
            rows = self._migrate(state)
            existing = next((row for row in rows.values() if row.get("creation_key") == key), None)
            if existing:
                if existing.get("digest") != digest:
                    raise WorkflowError("同一互动幂等键对应了不同内容。", 409)
                return self._project(existing)
            same = next((row for row in rows.values() if row.get("digest") == digest and row.get("status") == "draft"), None)
            if same:
                return self._project(same)
            if len(rows) >= MAX_INTERACTIONS:
                removable = sorted((row for row in rows.values() if row.get("status") not in {"dispatching", "unknown_result"}), key=lambda row: row.get("created_at", ""))
                if not removable:
                    raise WorkflowError("互动任务已达到上限，请先处理待核对任务。", 422)
                rows.pop(removable[0]["id"], None)
            row = {
                "id": uuid.uuid4().hex, "creation_key": key, "digest": digest,
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "platform": platform, "source_kind": source.get("kind") if source else "remote", "source_id": source_id,
                "delivery": delivery, "account_id": account_id, "account_label": account_label,
                "target_id": str(target_id or "")[:100], "target_url": str(target_url or "")[:2048],
                "kind": kind, "payload": payload, "status": "draft", "attempts": 0, "operation_id": None,
                "result": None, "created_at": _now(), "updated_at": _now(),
            }
            rows[row["id"]] = row
            return self._project(row)

    def update_draft(self, interaction_id: str, *, expected_updated_at: str, items: list[dict[str, Any]] | None = None, text: str = "") -> dict[str, Any]:
        with self.store.transaction() as state:
            rows = self._migrate(state)
            row = rows.get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("status") != "draft":
                raise WorkflowError("只有待确认草稿可以修改。", 409)
            if row.get("delivery") != "remote" or row.get("source_kind") == "import":
                raise WorkflowError("历史本地导入草稿只读保留，不能修改。", 409)
            if expected_updated_at != row.get("updated_at"):
                raise WorkflowError("互动草稿已在其他位置更新，请刷新后再编辑。", 409)
            kind = str(row.get("kind"))
            proposed = list(items or [])
            if kind in {"reply", "delete"}:
                current = list((row.get("payload") or {}).get("items") or [])
                if len(proposed) != len(current):
                    raise WorkflowError("互动草稿的目标评论不能在编辑阶段增删。请重新建立任务。", 409)
                for before, after in zip(current, proposed):
                    if any(str(before.get(key) or "") != str(after.get(key) or "") for key in ("id", "nickname", "content")):
                        raise WorkflowError("互动草稿的目标评论不能在编辑阶段更换。请重新建立任务。", 409)
            payload = self._clean_payload(kind, proposed, text)
            row["payload"] = payload
            row["digest"] = fingerprint({"platform": row.get("platform"), "account_id": row.get("account_id"),
                                          "source_id": row.get("source_id"), "target_id": row.get("target_id"),
                                          "delivery": row.get("delivery"), "kind": row.get("kind"), **payload})
            row["updated_at"] = _now()
            return self._project(row)

    def cancel_draft(self, interaction_id: str, *, expected_updated_at: str) -> dict[str, Any]:
        with self.store.transaction() as state:
            rows = self._migrate(state)
            row = rows.get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("status") != "draft":
                raise WorkflowError("只有待确认草稿可以取消。", 409)
            if row.get("delivery") != "remote" or row.get("source_kind") == "import":
                raise WorkflowError("历史本地导入草稿只读保留，不能修改。", 409)
            if expected_updated_at != row.get("updated_at"):
                raise WorkflowError("互动草稿已变化，请刷新后再取消。", 409)
            row.update(status="cancelled", updated_at=_now())
            return self._project(row)

    def execute(self, interaction_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise WorkflowError("请确认将已审阅的互动草稿发送到所选真实平台账号。", 422)
        seed = self._get_row(interaction_id)
        if seed.get("status") in {"dispatching", "unknown_result", "verified", "partial"}:
            # Idempotent, but never treats a second click as authority to replay.
            return self._project(seed)
        if seed.get("status") != "draft":
            raise WorkflowError("当前互动任务不能执行。", 409)
        if seed.get("delivery") != "remote" or seed.get("source_kind") == "import":
            raise WorkflowError("历史本地导入草稿只读保留，不能执行平台写入。", 409)
        platform = str(seed.get("platform") or "")
        account = self._account_for_remote(str(seed.get("account_id") or ""), platform, str(seed.get("kind") or ""), require_connected=True, require_idle=True)
        if platform != "xiaohongshu":
            raise WorkflowError("该平台的托管互动执行尚未接入。", 409)
        target_id, locator = self.workspace.xhs_ops._locator(account["id"], note_id=str(seed.get("target_id") or ""), url="")
        with self.store.transaction() as state:
            rows = self._migrate(state)
            row = rows.get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("status") in {"dispatching", "unknown_result", "verified", "partial"}:
                return self._project(row)
            if row.get("status") != "draft":
                raise WorkflowError("当前互动任务不能执行。", 409)
            current_account = state.get("accounts", {}).get(account["id"])
            if not current_account or current_account.get("status") != "connected":
                raise WorkflowError("目标账号当前未连接；草稿仍会保留。", 409)
            current_operation = current_account.get("operation") or {}
            if current_operation.get("state") in {"running", "recovery_required"}:
                raise WorkflowError("目标账号当前有未结束或待核对的操作。", 409)
            operation_id = uuid.uuid4().hex
            row.update(status="dispatching", attempts=int(row.get("attempts") or 0) + 1,
                       operation_id=operation_id, updated_at=_now(), account_label=str(account.get("label") or "")[:80])
            current_account["operation"] = {
                "id": operation_id, "kind": "interaction", "state": "running",
                "interaction_id": interaction_id, "started_at": _now(),
            }
            work = deepcopy(row)
        action = {"reply": "reply", "delete": "delete", "comment": "comment"}[str(work["kind"])]
        try:
            result = self.workspace.xhs_ops.execute_interaction_action(account["id"], action, locator, work["payload"], operation_id)
        except Exception as exc:
            # The execution intent was already persisted. An unexpected exception can
            # no longer prove that the platform write did not happen, so fail closed.
            self._finish(interaction_id, {"state": "unknown_result", "data": {}})
            if isinstance(exc, WorkflowError):
                raise
            raise WorkflowError("互动执行意外中断，结果未知；请先核对，Ripple 不会自动重发。", 502) from exc
        return self._finish(interaction_id, result)

    def _finish(self, interaction_id: str, result: dict[str, Any]) -> dict[str, Any]:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        states: list[str] = []
        if isinstance(data.get("results"), list):
            states = [str(item.get("status") or "") for item in data["results"] if isinstance(item, dict)]
        elif data.get("status"):
            states = [str(data.get("status"))]
        if states and all(value == "verified" for value in states):
            status = "verified"
        elif states and all(value == "not_submitted" for value in states):
            status = "not_submitted"
        elif "unknown_result" in states or result.get("state") == "unknown_result":
            status = "unknown_result"
        elif states:
            status = "partial"
        else:
            status = "unknown_result" if result.get("state") == "unknown_result" else "not_submitted"
        with self.store.transaction() as state:
            row = self._migrate(state).get(interaction_id)
            if not row:
                raise WorkflowError("互动任务记录已丢失，请人工核对平台后再操作。", 409)
            row.update(status=status, result=deepcopy(data), updated_at=_now())
            account = state.get("accounts", {}).get(str(row.get("account_id") or ""))
            operation = (account or {}).get("operation") or {}
            if account and operation.get("id") == row.get("operation_id") and operation.get("kind") == "interaction":
                if status == "unknown_result":
                    operation["state"] = "recovery_required"
                    account["operation"] = operation
                else:
                    account["operation"] = None
            return self._project(row)

    def refresh_result(self, interaction_id: str) -> dict[str, Any]:
        row = self._get_row(interaction_id)
        if row.get("status") not in {"dispatching", "unknown_result"} or not row.get("operation_id"):
            return {"source": "local_ledger", "platform_verified": False, "interaction": self._project(row)}
        account_id = str(row.get("account_id") or "")
        if not account_id:
            return {"source": "local_worker", "platform_verified": False, "interaction": self._project(row)}
        result = self.accounts.read_result(account_id, str(row["operation_id"]))
        interaction = self._finish(interaction_id, {"state": result.get("state"), "data": result.get("data") or {}}) if result else self._project(row)
        return {"source": "local_worker", "platform_verified": False, "interaction": interaction,
                "note": "这里只刷新 Ripple 本地 Worker 回执；没有重新查询平台当前状态。"}

    def resolve_unknown(self, interaction_id: str, *, result: str, confirmed: bool, note: str = "") -> dict[str, Any]:
        if not confirmed:
            raise WorkflowError("请确认你已经在对应平台人工检查了这次互动结果。", 422)
        if result not in {"verified", "not_submitted"}:
            raise WorkflowError("人工核对结果无效。", 422)
        with self.store.transaction() as state:
            row = self._migrate(state).get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("status") != "unknown_result":
                raise WorkflowError("只有结果未知的真实互动可以记录人工核对结果。", 409)
            account = state.get("accounts", {}).get(str(row.get("account_id") or ""))
            operation = (account or {}).get("operation") or {}
            if account and operation.get("id") == row.get("operation_id") and operation.get("kind") == "interaction":
                account["operation"] = None
            row.update(
                status=result,
                resolution={"source": "manual_platform_check", "result": result,
                            "note": str(note or "").strip()[:500], "at": _now()},
                updated_at=_now(),
            )
            return self._project(row)

    def verify_platform(self, interaction_id: str) -> dict[str, Any]:
        row = self._get_row(interaction_id)
        cap = _capability(str(row.get("platform") or "generic"))
        if not cap["platform_verify"]:
            raise WorkflowError("该平台的主动远端复核尚未接入；请在平台人工检查，Ripple 不会因此自动重发。", 409)
        raise WorkflowError("平台复核适配器尚未实现。", 409)
