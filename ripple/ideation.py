"""Persistent Agent-driven ideation domain.

The ideation store is intentionally separate from the large workspace JSON. It
keeps recommendation runs, stable ideas, source snapshots, brief revisions and
feedback recoverable across page refreshes and server restarts.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any
import uuid


IDEA_STAGES = {
    "candidate", "saved", "developing", "review", "ready", "production",
    "done", "rejected", "archived",
}
RUN_STATES = {"queued", "running", "waiting_user", "partial", "succeeded", "failed", "cancelled", "interrupted"}


class IdeationError(RuntimeError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback):
    if not value:
        return deepcopy(fallback)
    try:
        parsed = json.loads(value)
        return parsed
    except (ValueError, TypeError):
        return deepcopy(fallback)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_for_stage(stage: str) -> str:
    if stage in {"developing", "production"}:
        return "doing"
    if stage == "done":
        return "done"
    return "pending"


def _stage_for_legacy(status: str) -> str:
    return {"doing": "production", "done": "done"}.get(status, "saved")


class IdeationService:
    def __init__(self, db_path: Path, *, legacy_path: Path | None = None):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path = legacy_path.resolve() if legacy_path else None
        self._lock = threading.RLock()
        self._init_schema()
        self._migrate_legacy_once()
        self._interrupt_orphaned_runs()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _init_schema(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idea_runs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    persona TEXT NOT NULL DEFAULT '',
                    target_platforms_json TEXT NOT NULL DEFAULT '[]',
                    trend_sources_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    event_seq INTEGER NOT NULL DEFAULT 0,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idea_runs_created_idx ON idea_runs(created_at DESC);

                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    legacy_status TEXT NOT NULL DEFAULT 'pending',
                    stage TEXT NOT NULL,
                    persona TEXT NOT NULL DEFAULT '',
                    angle TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    campaign_id TEXT NOT NULL DEFAULT '',
                    campaign_rule_version INTEGER NOT NULL DEFAULT 0,
                    trend_refs_json TEXT NOT NULL DEFAULT '[]',
                    target_platforms_json TEXT NOT NULL DEFAULT '[]',
                    requirements_json TEXT NOT NULL DEFAULT '[]',
                    pending_checks_json TEXT NOT NULL DEFAULT '[]',
                    platform_plans_json TEXT NOT NULL DEFAULT '[]',
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    score INTEGER NOT NULL DEFAULT 0,
                    brief_revision INTEGER NOT NULL DEFAULT 0,
                    brief_status TEXT NOT NULL DEFAULT '',
                    content_id TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ideas_stage_idx ON ideas(stage, updated DESC);
                CREATE INDEX IF NOT EXISTS ideas_run_idx ON ideas(run_id);

                CREATE TABLE IF NOT EXISTS brief_revisions (
                    idea_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    locked_fields_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (idea_id, revision),
                    FOREIGN KEY (idea_id) REFERENCES ideas(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS source_snapshots (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    fetched_at INTEGER NOT NULL DEFAULT 0,
                    version TEXT NOT NULL DEFAULT '',
                    access_scope TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS source_snapshots_run_idx ON source_snapshots(run_id);

                CREATE TABLE IF NOT EXISTS idea_feedback (
                    id TEXT PRIMARY KEY,
                    idea_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (idea_id) REFERENCES ideas(id) ON DELETE CASCADE
                );
                """
            )

    def _meta(self, key: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _migrate_legacy_once(self) -> None:
        if self._meta("legacy_migrated_v1"):
            return
        rows: list[dict[str, Any]] = []
        digest = "none"
        if self.legacy_path and self.legacy_path.is_file():
            raw = self.legacy_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            try:
                parsed = json.loads(raw)
                rows = parsed if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                rows = []
            backup = self.db_path.parent / f"legacy-_ideas-{digest[:12]}.json"
            if not backup.exists():
                shutil.copy2(self.legacy_path, backup)
        with self._lock, self._connect() as db:
            for value in rows:
                if not isinstance(value, dict):
                    continue
                idea_id = str(value.get("id") or uuid.uuid4().hex[:12])[:64]
                status = str(value.get("status") or "pending")
                created = int(value.get("created") or time.time())
                db.execute(
                    """
                    INSERT OR IGNORE INTO ideas(
                        id,run_id,title,note,source,legacy_status,stage,persona,angle,reason,
                        campaign_id,campaign_rule_version,trend_refs_json,target_platforms_json,
                        requirements_json,pending_checks_json,platform_plans_json,source_refs_json,
                        score,brief_revision,brief_status,content_id,plan_id,created,updated
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        idea_id, "", str(value.get("title") or "未命名选题")[:240],
                        str(value.get("note") or "")[:12000], str(value.get("source") or "")[:500],
                        status, _stage_for_legacy(status), "", str(value.get("angle") or "")[:2000],
                        str(value.get("reason") or "")[:2000], str(value.get("campaign_id") or "")[:64],
                        int(value.get("campaign_rule_version") or 0),
                        _json(value.get("trend_refs") if isinstance(value.get("trend_refs"), list) else []),
                        _json(value.get("target_platforms") if isinstance(value.get("target_platforms"), list) else []),
                        _json(value.get("requirements") if isinstance(value.get("requirements"), list) else []),
                        _json(value.get("pending_checks") if isinstance(value.get("pending_checks"), list) else []),
                        "[]", "[]", 0, 0, "", "", "", created, created,
                    ),
                )
            db.execute(
                "INSERT INTO meta(key,value) VALUES('legacy_migrated_v1',?) ON CONFLICT(key) DO NOTHING",
                (digest,),
            )

    def _interrupt_orphaned_runs(self) -> None:
        now = _now_iso()
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE idea_runs
                SET status='interrupted', error='服务曾在任务执行期间重启；没有自动重复调用模型。', updated_at=?,
                    event_seq=event_seq+1
                WHERE status IN ('queued','running')
                """,
                (now,),
            )

    @staticmethod
    def _idea_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        stage = str(value.get("stage") or "saved")
        return {
            "id": value["id"], "run_id": value.get("run_id", ""), "title": value["title"],
            "note": value.get("note", ""), "source": value.get("source", ""),
            "status": _status_for_stage(stage), "stage": stage, "persona": value.get("persona", ""),
            "angle": value.get("angle", ""), "reason": value.get("reason", ""),
            "campaign_id": value.get("campaign_id", ""),
            "campaign_rule_version": int(value.get("campaign_rule_version") or 0),
            "trend_refs": _loads(value.get("trend_refs_json"), []),
            "target_platforms": _loads(value.get("target_platforms_json"), []),
            "requirements": _loads(value.get("requirements_json"), []),
            "pending_checks": _loads(value.get("pending_checks_json"), []),
            "platform_plans": _loads(value.get("platform_plans_json"), []),
            "source_refs": _loads(value.get("source_refs_json"), []),
            "score": int(value.get("score") or 0),
            "brief_revision": int(value.get("brief_revision") or 0),
            "brief_status": value.get("brief_status", ""), "content_id": value.get("content_id", ""),
            "plan_id": value.get("plan_id", ""), "created": int(value.get("created") or 0),
            "updated": int(value.get("updated") or 0),
        }

    @staticmethod
    def _run_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "id": value["id"], "kind": value["kind"], "persona": value.get("persona", ""),
            "target_platforms": _loads(value.get("target_platforms_json"), []),
            "trend_sources": _loads(value.get("trend_sources_json"), []),
            "status": value["status"], "stage": value.get("stage", ""), "error": value.get("error", ""),
            "result": _loads(value.get("result_json"), {}), "event_seq": int(value.get("event_seq") or 0),
            "cancelled": bool(value.get("cancelled")), "request": _loads(value.get("request_json"), {}),
            "created_at": value["created_at"], "updated_at": value["updated_at"],
        }

    def list_ideas(self, *, persona: str = "", include_rejected: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        query = "SELECT * FROM ideas"
        clauses, params = [], []
        if persona:
            clauses.append("(persona='' OR persona=?)")
            params.append(persona)
        if not include_rejected:
            clauses.append("stage NOT IN ('rejected','archived')")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated DESC, created DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            return [self._idea_public(row) for row in db.execute(query, params).fetchall()]

    def get_idea(self, idea_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
            if not row:
                raise IdeationError("选题不存在。", 404)
            value = self._idea_public(row)
        value["brief"] = self.latest_brief(idea_id)
        return value

    def create_idea(self, value: dict[str, Any], *, stage: str | None = None, run_id: str = "") -> dict[str, Any]:
        created = int(value.get("created") or time.time())
        idea_id = str(value.get("id") or uuid.uuid4().hex[:12])[:64]
        legacy_status = str(value.get("status") or "pending")
        chosen_stage = stage or _stage_for_legacy(legacy_status)
        if chosen_stage not in IDEA_STAGES:
            raise IdeationError("选题阶段无效。")
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO ideas(
                    id,run_id,title,note,source,legacy_status,stage,persona,angle,reason,
                    campaign_id,campaign_rule_version,trend_refs_json,target_platforms_json,
                    requirements_json,pending_checks_json,platform_plans_json,source_refs_json,
                    score,brief_revision,brief_status,content_id,plan_id,created,updated
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    idea_id, run_id, str(value.get("title") or "未命名选题")[:240],
                    str(value.get("note") or "")[:12000], str(value.get("source") or "")[:500],
                    legacy_status, chosen_stage, str(value.get("persona") or "")[:100],
                    str(value.get("angle") or "")[:2000], str(value.get("reason") or "")[:2000],
                    str(value.get("campaign_id") or "")[:64], int(value.get("campaign_rule_version") or 0),
                    _json(value.get("trend_refs") if isinstance(value.get("trend_refs"), list) else []),
                    _json(value.get("target_platforms") if isinstance(value.get("target_platforms"), list) else []),
                    _json(value.get("requirements") if isinstance(value.get("requirements"), list) else []),
                    _json(value.get("pending_checks") if isinstance(value.get("pending_checks"), list) else []),
                    _json(value.get("platform_plans") if isinstance(value.get("platform_plans"), list) else []),
                    _json(value.get("source_refs") if isinstance(value.get("source_refs"), list) else []),
                    int(value.get("score") or 0), 0, "", "", "", created, created,
                ),
            )
        return self.get_idea(idea_id)

    def update_idea(self, idea_id: str, value: dict[str, Any]) -> dict[str, Any]:
        current = self.get_idea(idea_id)
        stage = str(value.get("stage") or current["stage"])
        if "status" in value and "stage" not in value:
            stage = _stage_for_legacy(str(value.get("status") or "pending"))
        if stage not in IDEA_STAGES:
            raise IdeationError("选题阶段无效。")
        now = int(time.time())
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE ideas SET
                    title=?,note=?,source=?,legacy_status=?,stage=?,persona=?,angle=?,reason=?,
                    campaign_id=?,campaign_rule_version=?,trend_refs_json=?,target_platforms_json=?,
                    requirements_json=?,pending_checks_json=?,platform_plans_json=?,source_refs_json=?,
                    score=?,updated=?
                WHERE id=?
                """,
                (
                    str(value.get("title", current["title"]) or current["title"])[:240],
                    str(value.get("note", current["note"]) or "")[:12000],
                    str(value.get("source", current["source"]) or "")[:500],
                    str(value.get("status") or current["status"]), stage,
                    str(value.get("persona", current["persona"]) or "")[:100],
                    str(value.get("angle", current["angle"]) or "")[:2000],
                    str(value.get("reason", current["reason"]) or "")[:2000],
                    str(value.get("campaign_id", current["campaign_id"]) or "")[:64],
                    int(value.get("campaign_rule_version", current["campaign_rule_version"]) or 0),
                    _json(value.get("trend_refs", current["trend_refs"])),
                    _json(value.get("target_platforms", current["target_platforms"])),
                    _json(value.get("requirements", current["requirements"])),
                    _json(value.get("pending_checks", current["pending_checks"])),
                    _json(value.get("platform_plans", current["platform_plans"])),
                    _json(value.get("source_refs", current["source_refs"])),
                    int(value.get("score", current["score"]) or 0), now, idea_id,
                ),
            )
        return self.get_idea(idea_id)

    def delete_idea(self, idea_id: str) -> None:
        with self._lock, self._connect() as db:
            cur = db.execute("DELETE FROM ideas WHERE id=?", (idea_id,))
            if not cur.rowcount:
                raise IdeationError("选题不存在。", 404)

    def feedback(self, idea_id: str, action: str, reason: str = "") -> dict[str, Any]:
        action = action.strip()
        stage_map = {"stash": "saved", "select": "saved", "reject": "rejected", "reopen": "candidate"}
        if action not in stage_map:
            raise IdeationError("反馈动作无效。")
        current = self.get_idea(idea_id)
        with self._lock, self._connect() as db:
            db.execute("UPDATE ideas SET stage=?,updated=? WHERE id=?", (stage_map[action], int(time.time()), idea_id))
            db.execute(
                "INSERT INTO idea_feedback(id,idea_id,run_id,action,reason,created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, idea_id, current.get("run_id", ""), action, reason[:500], _now_iso()),
            )
        return self.get_idea(idea_id)

    def create_run(self, kind: str, request: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        if kind not in {"recommend", "develop"}:
            raise IdeationError("未知选题任务类型。")
        normalized = _json(request)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = _now_iso()
        with self._lock, self._connect() as db:
            existing = db.execute("SELECT * FROM idea_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise IdeationError("该任务键已被不同请求使用。", 409)
                return self._run_public(existing), False
            run_id = uuid.uuid4().hex
            db.execute(
                """
                INSERT INTO idea_runs(
                    id,kind,idempotency_key,request_hash,request_json,persona,
                    target_platforms_json,trend_sources_json,status,stage,error,result_json,
                    event_seq,cancelled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, kind, idempotency_key, digest, normalized,
                    str(request.get("persona") or "")[:100],
                    _json(request.get("target_platforms") or []), _json(request.get("trend_sources") or []),
                    "queued", "queued", "", "{}", 0, 0, now, now,
                ),
            )
            row = db.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            return self._run_public(row), True

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise IdeationError("选题任务不存在。", 404)
            value = self._run_public(row)
            value["sources"] = self.list_sources(run_id)
            value["ideas"] = [
                self._idea_public(item)
                for item in db.execute("SELECT * FROM ideas WHERE run_id=? ORDER BY score DESC,created ASC", (run_id,)).fetchall()
            ]
            return value

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            return [self._run_public(row) for row in db.execute(
                "SELECT * FROM idea_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def set_run(self, run_id: str, *, status: str | None = None, stage: str | None = None,
                error: str | None = None, result: dict[str, Any] | None = None) -> dict[str, Any]:
        if status and status not in RUN_STATES:
            raise IdeationError("任务状态无效。")
        fields, params = [], []
        if status is not None:
            fields.append("status=?"); params.append(status)
        if stage is not None:
            fields.append("stage=?"); params.append(stage[:100])
        if error is not None:
            fields.append("error=?"); params.append(error[:2000])
        if result is not None:
            fields.append("result_json=?"); params.append(_json(result))
        fields.extend(["event_seq=event_seq+1", "updated_at=?"])
        params.append(_now_iso()); params.append(run_id)
        with self._lock, self._connect() as db:
            cur = db.execute(f"UPDATE idea_runs SET {','.join(fields)} WHERE id=?", params)
            if not cur.rowcount:
                raise IdeationError("选题任务不存在。", 404)
        return self.get_run(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE idea_runs SET cancelled=1,status='cancelled',stage='cancelled',event_seq=event_seq+1,updated_at=? WHERE id=? AND status IN ('queued','running','partial','waiting_user')",
                (_now_iso(), run_id),
            )
            if not cur.rowcount:
                row = db.execute("SELECT id FROM idea_runs WHERE id=?", (run_id,)).fetchone()
                if not row:
                    raise IdeationError("选题任务不存在。", 404)
        return self.get_run(run_id)

    def run_cancelled(self, run_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT cancelled,status FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            return not row or bool(row["cancelled"]) or row["status"] == "cancelled"

    def replace_sources(self, run_id: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM source_snapshots WHERE run_id=?", (run_id,))
            for source in sources[:200]:
                db.execute(
                    """
                    INSERT INTO source_snapshots(
                        id,run_id,kind,source_id,title,platform,url,fetched_at,version,access_scope,data_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(source["id"])[:160], run_id, str(source.get("kind") or "")[:40],
                        str(source.get("source_id") or "")[:160], str(source.get("title") or "")[:500],
                        str(source.get("platform") or "")[:40], str(source.get("url") or "")[:3000],
                        int(source.get("fetched_at") or 0), str(source.get("version") or "")[:120],
                        str(source.get("access_scope") or "")[:160], _json(source.get("data") or {}),
                    ),
                )
        return self.list_sources(run_id)

    def list_sources(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM source_snapshots WHERE run_id=? ORDER BY kind,title", (run_id,)).fetchall()
            return [{
                "id": row["id"], "kind": row["kind"], "source_id": row["source_id"], "title": row["title"],
                "platform": row["platform"], "url": row["url"], "fetched_at": int(row["fetched_at"]),
                "version": row["version"], "access_scope": row["access_scope"],
                "data": _loads(row["data_json"], {}),
            } for row in rows]

    def store_candidates(self, run_id: str, persona: str, recommendations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for recommendation in recommendations:
            value = dict(recommendation)
            value["persona"] = persona
            value["source"] = str(value.get("source") or f"Agent 推荐 · {persona}")
            try:
                created.append(self.create_idea(value, stage="candidate", run_id=run_id))
            except sqlite3.IntegrityError:
                continue
        return created

    def latest_brief(self, idea_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM brief_revisions WHERE idea_id=? ORDER BY revision DESC LIMIT 1", (idea_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "idea_id": idea_id, "revision": int(row["revision"]), "data": _loads(row["data_json"], {}),
                "locked_fields": _loads(row["locked_fields_json"], []), "source": row["source"],
                "status": row["status"], "created_at": row["created_at"],
            }

    def save_brief(self, idea_id: str, data: dict[str, Any], *, source: str, status: str = "draft",
                   locked_fields: list[str] | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        self.get_idea(idea_id)
        current = self.latest_brief(idea_id)
        current_revision = int(current["revision"]) if current else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise IdeationError("策划单已更新，请刷新后再修改。", 409)
        revision = current_revision + 1
        now = _now_iso()
        stage = "ready" if status == "confirmed" else "review"
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO brief_revisions(idea_id,revision,data_json,locked_fields_json,source,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (idea_id, revision, _json(data), _json(locked_fields or []), source[:40], status[:40], now),
            )
            db.execute(
                "UPDATE ideas SET brief_revision=?,brief_status=?,stage=?,updated=? WHERE id=?",
                (revision, status, stage, int(time.time()), idea_id),
            )
        return self.latest_brief(idea_id) or {}

    def confirm_brief(self, idea_id: str, expected_revision: int) -> dict[str, Any]:
        current = self.latest_brief(idea_id)
        if not current:
            raise IdeationError("尚未生成策划单。", 409)
        if int(current["revision"]) != int(expected_revision):
            raise IdeationError("策划单已更新，请刷新后确认。", 409)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE brief_revisions SET status='confirmed' WHERE idea_id=? AND revision=?",
                (idea_id, expected_revision),
            )
            db.execute(
                "UPDATE ideas SET brief_status='confirmed',stage='ready',updated=? WHERE id=?",
                (int(time.time()), idea_id),
            )
        return self.latest_brief(idea_id) or {}

    def mark_developing(self, idea_id: str) -> None:
        self.get_idea(idea_id)
        with self._lock, self._connect() as db:
            db.execute("UPDATE ideas SET stage='developing',updated=? WHERE id=?", (int(time.time()), idea_id))

    def restore_after_develop_failure(self, idea_id: str) -> None:
        current = self.latest_brief(idea_id)
        stage = "review" if current else "saved"
        with self._lock, self._connect() as db:
            db.execute("UPDATE ideas SET stage=?,updated=? WHERE id=? AND stage='developing'", (stage, int(time.time()), idea_id))

    def attach_content(self, idea_id: str, content_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE ideas SET content_id=?,stage='production',updated=? WHERE id=?",
                (content_id[:64], int(time.time()), idea_id),
            )
        return self.get_idea(idea_id)

    def attach_plan(self, idea_id: str, plan_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute("UPDATE ideas SET plan_id=?,updated=? WHERE id=?", (plan_id[:64], int(time.time()), idea_id))
        return self.get_idea(idea_id)
