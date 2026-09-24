"""Durable proactive idea discovery.

This module never fetches platform data and never invokes a model.  It only
normalizes cached source snapshots, admits bounded discovery jobs, accounts for
budget reservations, and tracks inbox notices.  Model execution remains in the
existing IdeaRun pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


POLICY_MODES = {"balanced", "combo_only"}
JOB_STATES = {
    "queued", "claimed", "running", "succeeded", "failed", "cancelled",
    "outcome_unknown", "skipped",
}
TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled", "outcome_unknown", "skipped"}
NOTICE_STATES = {"unread", "read", "dismissed", "scheduled"}


class DiscoveryError(RuntimeError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def _now_iso(now: float | None = None) -> str:
    stamp = time.time() if now is None else now
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _local_date(now: float, zone: str) -> str:
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise DiscoveryError("通知时区无效。") from exc
    return datetime.fromtimestamp(now, tz).date().isoformat()


def _safe_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise DiscoveryError("通知时区无效。") from exc
    return value


def _clock(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise DiscoveryError("安静时段必须使用 HH:MM。")
    return value


def _policy_id(persona: str) -> str:
    return "dp_" + hashlib.sha256(("idea-discovery:" + persona).encode("utf-8")).hexdigest()[:24]


def _compact_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " ".join(_compact_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_compact_text(item) for item in value.values())
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _grams(value: str) -> set[str]:
    text = _compact_text(value).casefold()
    result = set(re.findall(r"[a-z0-9][a-z0-9+._-]{1,24}", text))
    for segment in re.findall(r"[\u4e00-\u9fff]{2,24}", text):
        if len(segment) <= 6:
            result.add(segment)
        for size in (2, 3, 4):
            result.update(segment[index:index + size] for index in range(max(0, len(segment) - size + 1)))
    return {token for token in result if token not in {"内容", "活动", "用户", "平台", "创作", "推荐", "相关", "这个", "可以"}}


def _match_score(text: str, terms: set[str]) -> int:
    if not terms:
        return 0
    tokens = _grams(text)
    if not tokens:
        return 0
    overlap = tokens & terms
    if not overlap:
        return 0
    longest = max((len(value) for value in overlap), default=0)
    return min(100, len(overlap) * 8 + longest * 4)


def _direct_overlap(left: str, right: str) -> int:
    a, b = _grams(left), _grams(right)
    overlap = a & b
    return min(100, len(overlap) * 10 + max((len(value) for value in overlap), default=0) * 5)


def _deadline_seconds(value: str) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            dt = datetime.fromisoformat(raw + "T23:59:59")
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def source_digest(trend_groups: list[dict[str, Any]], campaigns: list[dict[str, Any]]) -> str:
    """Digest only meaningful fields; fetched_at and minor heat changes do not retrigger."""
    payload = {
        "trends": sorted([
            {
                "platform": str(group.get("platform") or ""),
                "items": sorted([
                    {
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                    }
                    for item in group.get("items", []) if isinstance(item, dict) and item.get("title")
                ], key=lambda row: (row["title"], row["url"])),
            }
            for group in trend_groups
        ], key=lambda row: row["platform"]),
        "campaigns": sorted([
            {
                "id": str(row.get("id") or ""),
                "platform": str(row.get("platform") or ""),
                "title": str(row.get("title") or ""),
                "rule_version": int(row.get("rule_version") or 0),
                "status": str(row.get("status") or ""),
                "deadline": str(row.get("submit_deadline") or ""),
                "qualification": str(row.get("qualification_state") or ""),
                "requirements": list(row.get("content_requirements") or []),
                "topics": list(row.get("required_topics") or []),
                "ai_policy": str(row.get("ai_policy") or ""),
            }
            for row in campaigns if isinstance(row, dict) and row.get("id")
        ], key=lambda row: row["id"]),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def source_health(trend_groups: list[dict[str, Any]], campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {str(group.get("platform") or ""): str(group.get("status") or "unknown") for group in trend_groups}
    newest = max((int(group.get("fetched_at") or 0) for group in trend_groups), default=0)
    return {
        "trend_statuses": statuses,
        "trend_newest_at": newest,
        "campaign_count": len(campaigns),
        "degraded": any(value in {"error", "missing", "expired"} for value in statuses.values()),
    }


def opportunity_key(kind: str, trend: dict[str, Any] | None, campaign: dict[str, Any] | None,
                    targets: list[str]) -> str:
    trend_identity = ""
    if trend:
        trend_identity = str(trend.get("url") or trend.get("title") or "")
    campaign_identity = str((campaign or {}).get("id") or "")
    raw = "|".join([kind, trend_identity, campaign_identity, ",".join(sorted(targets))])
    return "opp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _evidence_digest(kind: str, trend: dict[str, Any] | None, campaign: dict[str, Any] | None) -> str:
    payload = {
        "kind": kind,
        "trend": ({"title": trend.get("title"), "url": trend.get("url")} if trend else None),
        "campaign": ({
            "id": campaign.get("id"), "rule_version": int(campaign.get("rule_version") or 0),
            "deadline": campaign.get("submit_deadline"), "qualification": campaign.get("qualification_state"),
            "requirements": campaign.get("content_requirements") or [], "topics": campaign.get("required_topics") or [],
            "ai_policy": campaign.get("ai_policy") or "",
        } if campaign else None),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def build_opportunities(
    *,
    profile_text: str,
    focus_keywords: list[str],
    target_platforms: list[str],
    trend_groups: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
    mode: str,
    effort_minutes: int,
    now: float,
    existing_keys: set[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Local, deterministic recall before any model invocation."""
    if mode not in POLICY_MODES:
        raise DiscoveryError("主动发现模式无效。")
    existing_keys = existing_keys or set()
    profile_terms = _grams(profile_text + " " + " ".join(focus_keywords))
    minimum_window = max(30, int(effort_minutes or 120)) * 60 + 2 * 60 * 60

    trends: list[dict[str, Any]] = []
    for group in trend_groups:
        if str(group.get("status") or "") not in {"fresh", "stale"}:
            continue
        for item in group.get("items", []):
            if not isinstance(item, dict) or not item.get("title"):
                continue
            text = _compact_text(item.get("title"))
            relevance = _match_score(text, profile_terms)
            trends.append({
                "platform": str(group.get("platform") or ""),
                "title": str(item.get("title") or "")[:500],
                "url": str(item.get("url") or "")[:3000],
                "hot": str(item.get("hot") or "")[:160],
                "relevance": relevance,
            })

    valid_campaigns: list[dict[str, Any]] = []
    for row in campaigns:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if target_platforms and row.get("platform") not in target_platforms:
            continue
        if str(row.get("status") or "active") != "active":
            continue
        if str(row.get("qualification_state") or "unknown") == "ineligible":
            continue
        ai_policy = str(row.get("ai_policy") or "")
        if re.search(r"(?:禁止|不允许|不得).{0,20}(?:AI|人工智能)|(?:AI|人工智能).{0,20}(?:禁止|不允许|不得)", ai_policy, re.I):
            continue
        deadline = _deadline_seconds(str(row.get("submit_deadline") or ""))
        if deadline is not None and deadline - now < minimum_window:
            continue
        campaign_text = _compact_text([
            row.get("title"), row.get("summary"), row.get("activity_type"),
            row.get("content_requirements"), row.get("required_topics"), row.get("reward_summary"),
        ])
        valid_campaigns.append({
            **row,
            "_text": campaign_text,
            "_relevance": _match_score(campaign_text, profile_terms),
        })

    candidates: list[dict[str, Any]] = []
    for trend in trends:
        for campaign in valid_campaigns:
            direct = _direct_overlap(trend["title"], campaign["_text"])
            relevant_pair = direct >= 20 or (trend["relevance"] >= 16 and campaign["_relevance"] >= 16)
            if not relevant_pair:
                continue
            key = opportunity_key("combo", trend, campaign, target_platforms)
            if key in existing_keys:
                continue
            score = direct * 2 + trend["relevance"] + campaign["_relevance"]
            candidates.append({
                "key": key, "kind": "combo", "score": score,
                "evidence_digest": _evidence_digest("combo", trend, campaign),
                "trend_titles": [trend["title"]], "trend_sources": [trend["platform"]],
                "campaign_ids": [str(campaign["id"])],
                "trigger_summary": f"热点「{trend['title']}」与活动「{campaign.get('title') or campaign['id']}」出现自然关联，且当前制作窗口可行。",
                "source_reason": "热点与活动组合",
            })

    if mode == "balanced":
        for trend in trends:
            if trend["relevance"] < 20:
                continue
            key = opportunity_key("trend", trend, None, target_platforms)
            if key not in existing_keys:
                candidates.append({
                    "key": key, "kind": "trend", "score": trend["relevance"],
                    "evidence_digest": _evidence_digest("trend", trend, None),
                    "trend_titles": [trend["title"]], "trend_sources": [trend["platform"]],
                    "campaign_ids": [],
                    "trigger_summary": f"热点「{trend['title']}」与账号方向匹配，暂未找到自然适配的活动。",
                    "source_reason": "热点机会",
                })
        for campaign in valid_campaigns:
            if campaign["_relevance"] < 20:
                continue
            key = opportunity_key("campaign", None, campaign, target_platforms)
            if key not in existing_keys:
                candidates.append({
                    "key": key, "kind": "campaign", "score": campaign["_relevance"],
                    "evidence_digest": _evidence_digest("campaign", None, campaign),
                    "trend_titles": [], "trend_sources": [],
                    "campaign_ids": [str(campaign["id"])],
                    "trigger_summary": f"活动「{campaign.get('title') or campaign['id']}」与账号方向匹配，当前没有必要强行绑定热点。",
                    "source_reason": "活动机会",
                })

    candidates.sort(key=lambda value: (-int(value["score"]), value["key"]))
    # Preserve source diversity instead of sending many variants of one event.
    selected, used_trends, used_campaigns = [], set(), set()
    for candidate in candidates:
        trend_id = tuple(candidate["trend_titles"])
        campaign_id = tuple(candidate["campaign_ids"])
        if trend_id and trend_id in used_trends and campaign_id and campaign_id in used_campaigns:
            continue
        selected.append(candidate)
        if trend_id:
            used_trends.add(trend_id)
        if campaign_id:
            used_campaigns.add(campaign_id)
        if len(selected) >= max(1, min(int(limit), 20)):
            break
    return selected


class IdeaDiscoveryService:
    def __init__(self, db_path):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()
        self._recover_jobs()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _init_schema(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS discovery_policies (
                    id TEXT PRIMARY KEY,
                    persona TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    target_platforms_json TEXT NOT NULL DEFAULT '[]',
                    trend_sources_json TEXT NOT NULL DEFAULT '[]',
                    account_ids_json TEXT NOT NULL DEFAULT '[]',
                    profile_id TEXT NOT NULL DEFAULT '',
                    profile_revision INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'balanced',
                    focus_keywords_json TEXT NOT NULL DEFAULT '[]',
                    effort_minutes INTEGER NOT NULL DEFAULT 120,
                    max_daily_runs INTEGER NOT NULL DEFAULT 6,
                    max_daily_candidates INTEGER NOT NULL DEFAULT 6,
                    max_daily_notices INTEGER NOT NULL DEFAULT 2,
                    min_candidate_score INTEGER NOT NULL DEFAULT 68,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    quiet_start TEXT NOT NULL DEFAULT '22:00',
                    quiet_end TEXT NOT NULL DEFAULT '08:00',
                    important_notifications INTEGER NOT NULL DEFAULT 0,
                    last_scan_at INTEGER NOT NULL DEFAULT 0,
                    last_daily_review TEXT NOT NULL DEFAULT '',
                    last_source_digest TEXT NOT NULL DEFAULT '',
                    pending_source_digest TEXT NOT NULL DEFAULT '',
                    pending_since INTEGER NOT NULL DEFAULT 0,
                    source_health_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_jobs (
                    id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL,
                    trigger_key TEXT NOT NULL UNIQUE,
                    trigger_type TEXT NOT NULL,
                    trigger_json TEXT NOT NULL,
                    opportunity_key TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until INTEGER NOT NULL DEFAULT 0,
                    provider_started INTEGER NOT NULL DEFAULT 0,
                    budget_date TEXT NOT NULL,
                    reserved_runs INTEGER NOT NULL DEFAULT 1,
                    reserved_candidates INTEGER NOT NULL DEFAULT 0,
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(policy_id) REFERENCES discovery_policies(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS discovery_jobs_policy_idx
                    ON discovery_jobs(policy_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS discovery_jobs_state_idx
                    ON discovery_jobs(status, lease_until);
                CREATE TABLE IF NOT EXISTS discovery_notices (
                    id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    idea_id TEXT NOT NULL,
                    opportunity_key TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'inbox',
                    state TEXT NOT NULL DEFAULT 'unread',
                    scheduled_at INTEGER NOT NULL DEFAULT 0,
                    sent_at INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(policy_id, idea_id, channel),
                    FOREIGN KEY(policy_id) REFERENCES discovery_policies(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS discovery_notices_state_idx
                    ON discovery_notices(policy_id, state, created_at DESC);
                """
            )
            policy_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(discovery_policies)").fetchall()}
            if "pending_source_digest" not in policy_columns:
                db.execute("ALTER TABLE discovery_policies ADD COLUMN pending_source_digest TEXT NOT NULL DEFAULT ''")
            if "pending_since" not in policy_columns:
                db.execute("ALTER TABLE discovery_policies ADD COLUMN pending_since INTEGER NOT NULL DEFAULT 0")
            if "profile_id" not in policy_columns:
                db.execute("ALTER TABLE discovery_policies ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''")
            if "profile_revision" not in policy_columns:
                db.execute("ALTER TABLE discovery_policies ADD COLUMN profile_revision INTEGER NOT NULL DEFAULT 0")
            notice_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(discovery_notices)").fetchall()}
            if "budget_date" not in notice_columns:
                db.execute("ALTER TABLE discovery_notices ADD COLUMN budget_date TEXT NOT NULL DEFAULT ''")

    def _recover_jobs(self) -> None:
        now = int(time.time())
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE discovery_jobs SET status='queued',lease_owner='',lease_until=0,updated_at=? "
                "WHERE status='claimed' AND provider_started=0 AND lease_until<?",
                (_now_iso(now), now),
            )
            db.execute(
                "UPDATE discovery_jobs SET status='outcome_unknown',error=?,lease_owner='',lease_until=0,updated_at=? "
                "WHERE status IN ('claimed','running') AND provider_started=1",
                ("服务在模型调用可能已经开始后重启；保留预算预占，不自动重试。", _now_iso(now)),
            )

    @staticmethod
    def _policy_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "id": value["id"], "persona": value["persona"], "enabled": bool(value["enabled"]),
            "revision": int(value["revision"]),
            "target_platforms": _loads(value["target_platforms_json"], []),
            "trend_sources": _loads(value["trend_sources_json"], []),
            "account_ids": _loads(value["account_ids_json"], []),
            "profile_id": str(value.get("profile_id") or ""),
            "profile_revision": int(value.get("profile_revision") or 0),
            "mode": value["mode"], "focus_keywords": _loads(value["focus_keywords_json"], []),
            "effort_minutes": int(value["effort_minutes"]),
            "max_daily_runs": int(value["max_daily_runs"]),
            "max_daily_candidates": int(value["max_daily_candidates"]),
            "max_daily_notices": int(value["max_daily_notices"]),
            "min_candidate_score": int(value["min_candidate_score"]),
            "timezone": value["timezone"], "quiet_start": value["quiet_start"], "quiet_end": value["quiet_end"],
            "important_notifications": bool(value["important_notifications"]),
            "last_scan_at": int(value["last_scan_at"]),
            "last_daily_review": value["last_daily_review"],
            "last_source_digest": value["last_source_digest"],
            "pending_source_digest": str(value.get("pending_source_digest") or ""),
            "pending_since": int(value.get("pending_since") or 0),
            "source_health": _loads(value["source_health_json"], {}),
            "created_at": value["created_at"], "updated_at": value["updated_at"],
        }

    @staticmethod
    def _job_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "id": value["id"], "policy_id": value["policy_id"], "policy_revision": int(value["policy_revision"]),
            "trigger_key": value["trigger_key"], "trigger_type": value["trigger_type"],
            "trigger": _loads(value["trigger_json"], {}), "opportunity_key": value["opportunity_key"],
            "source_digest": value["source_digest"], "status": value["status"], "run_id": value["run_id"],
            "lease_until": int(value["lease_until"]), "provider_started": bool(value["provider_started"]),
            "budget_date": value["budget_date"], "reserved_runs": int(value["reserved_runs"]),
            "reserved_candidates": int(value["reserved_candidates"]), "usage": _loads(value["usage_json"], {}),
            "error": value["error"], "created_at": value["created_at"], "updated_at": value["updated_at"],
        }

    def get_policy(self, persona: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM discovery_policies WHERE persona=?", (persona,)).fetchone()
            return self._policy_public(row) if row else None

    def get_policy_by_id(self, policy_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM discovery_policies WHERE id=?", (policy_id,)).fetchone()
            return self._policy_public(row) if row else None

    def list_enabled(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [self._policy_public(row) for row in db.execute(
                "SELECT * FROM discovery_policies WHERE enabled=1 ORDER BY persona"
            ).fetchall()]

    def configure(self, persona: str, value: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        persona = persona.strip()
        if not persona:
            raise DiscoveryError("必须选择账号画像。")
        mode = str(value.get("mode") or "balanced")
        if mode not in POLICY_MODES:
            raise DiscoveryError("主动发现模式无效。")
        zone = _safe_timezone(str(value.get("timezone") or "UTC"))
        quiet_start, quiet_end = _clock(str(value.get("quiet_start") or "22:00")), _clock(str(value.get("quiet_end") or "08:00"))
        targets = list(dict.fromkeys(str(item)[:40] for item in value.get("target_platforms", []) if str(item).strip()))[:12]
        trends = list(dict.fromkeys(str(item)[:40] for item in value.get("trend_sources", []) if str(item).strip()))[:12]
        accounts = list(dict.fromkeys(str(item)[:64] for item in value.get("account_ids", []) if str(item).strip()))[:30]
        profile_id = str(value.get("profile_id") or "")[:80]
        profile_revision = max(0, int(value.get("profile_revision") or 0))
        focus = list(dict.fromkeys(str(item).strip()[:80] for item in value.get("focus_keywords", []) if str(item).strip()))[:30]
        effort = max(30, min(int(value.get("effort_minutes") or 120), 1440))
        max_runs = max(1, min(int(value.get("max_daily_runs") or 6), 24))
        max_candidates = max(1, min(int(value.get("max_daily_candidates") or 6), 40))
        max_notices = max(0, min(int(value.get("max_daily_notices") or 2), 12))
        min_score = max(0, min(int(value.get("min_candidate_score") or 68), 100))
        enabled = bool(value.get("enabled"))
        stamp = time.time() if now is None else now
        now_iso = _now_iso(stamp)
        pid = _policy_id(persona)
        with self._lock, self._connect() as db:
            current = db.execute("SELECT * FROM discovery_policies WHERE id=?", (pid,)).fetchone()
            revision = int(current["revision"]) + 1 if current else 1
            last_digest = "" if enabled and (not current or not bool(current["enabled"])) else (str(current["last_source_digest"]) if current else "")
            created = str(current["created_at"]) if current else now_iso
            db.execute(
                """
                INSERT INTO discovery_policies(
                    id,persona,enabled,revision,target_platforms_json,trend_sources_json,account_ids_json,
                    profile_id,profile_revision,mode,focus_keywords_json,effort_minutes,max_daily_runs,max_daily_candidates,max_daily_notices,
                    min_candidate_score,timezone,quiet_start,quiet_end,important_notifications,last_scan_at,
                    last_daily_review,last_source_digest,pending_source_digest,pending_since,
                    source_health_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled=excluded.enabled,revision=excluded.revision,
                    target_platforms_json=excluded.target_platforms_json,trend_sources_json=excluded.trend_sources_json,
                    account_ids_json=excluded.account_ids_json,profile_id=excluded.profile_id,
                    profile_revision=excluded.profile_revision,mode=excluded.mode,
                    focus_keywords_json=excluded.focus_keywords_json,effort_minutes=excluded.effort_minutes,
                    max_daily_runs=excluded.max_daily_runs,max_daily_candidates=excluded.max_daily_candidates,
                    max_daily_notices=excluded.max_daily_notices,min_candidate_score=excluded.min_candidate_score,
                    timezone=excluded.timezone,quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,
                    important_notifications=excluded.important_notifications,last_source_digest=?,
                    pending_source_digest='',pending_since=0,updated_at=excluded.updated_at
                """,
                (
                    pid, persona, int(enabled), revision, _json(targets), _json(trends), _json(accounts),
                    profile_id, profile_revision, mode, _json(focus), effort, max_runs, max_candidates, max_notices, min_score,
                    zone, quiet_start, quiet_end, int(bool(value.get("important_notifications"))), 0,
                    "", last_digest, "", 0, "{}", created, now_iso, last_digest,
                ),
            )
            db.execute(
                "UPDATE discovery_jobs SET status='cancelled',error=?,lease_owner='',lease_until=0,updated_at=? "
                "WHERE policy_id=? AND policy_revision<>? AND status IN ('queued','claimed') AND provider_started=0",
                ("策略已更新或暂停，旧的未执行任务已取消。", now_iso, pid, revision),
            )
        policy = self.get_policy(persona)
        assert policy is not None
        return policy

    def source_change_due(self, policy: dict[str, Any], digest: str, now: float, *, coalesce_seconds: int = 120) -> bool:
        if digest == str(policy.get("last_source_digest") or ""):
            return False
        pending = str(policy.get("pending_source_digest") or "")
        since = int(policy.get("pending_since") or 0)
        if pending != digest:
            with self._lock, self._connect() as db:
                db.execute(
                    "UPDATE discovery_policies SET pending_source_digest=?,pending_since=?,updated_at=? WHERE id=?",
                    (digest, int(now), _now_iso(now), policy["id"]),
                )
            return False
        return since > 0 and now - since >= max(0, coalesce_seconds)

    def daily_due(self, policy: dict[str, Any], now: float) -> bool:
        return str(policy.get("last_daily_review") or "") != _local_date(now, str(policy.get("timezone") or "UTC"))

    def record_health(self, policy_id: str, health: dict[str, Any], *, now: float) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE discovery_policies SET source_health_json=?,updated_at=? WHERE id=?",
                (_json(health), _now_iso(now), policy_id),
            )

    def record_scan(self, policy_id: str, *, digest: str, health: dict[str, Any],
                    daily_review: bool, now: float) -> None:
        with self._lock, self._connect() as db:
            policy = db.execute("SELECT timezone FROM discovery_policies WHERE id=?", (policy_id,)).fetchone()
            if not policy:
                return
            review = _local_date(now, str(policy["timezone"])) if daily_review else ""
            if daily_review:
                db.execute(
                    "UPDATE discovery_policies SET last_scan_at=?,last_daily_review=?,last_source_digest=?,"
                    "pending_source_digest='',pending_since=0,source_health_json=?,updated_at=? WHERE id=?",
                    (int(now), review, digest, _json(health), _now_iso(now), policy_id),
                )
            else:
                db.execute(
                    "UPDATE discovery_policies SET last_scan_at=?,last_source_digest=?,pending_source_digest='',"
                    "pending_since=0,source_health_json=?,updated_at=? WHERE id=?",
                    (int(now), digest, _json(health), _now_iso(now), policy_id),
                )

    def existing_opportunity_keys(self, policy_id: str) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT opportunity_key FROM discovery_jobs WHERE policy_id=? AND status NOT IN ('failed','cancelled','skipped')",
                (policy_id,),
            ).fetchall()
            return {str(row[0]) for row in rows if row[0]}

    def admit(self, policy: dict[str, Any], opportunity: dict[str, Any], *, source_digest_value: str,
              trigger_type: str, now: float) -> dict[str, Any] | None:
        if not policy.get("enabled"):
            return None
        budget_date = _local_date(now, policy["timezone"])
        trigger_key = hashlib.sha256(
            f"{policy['id']}|{policy['revision']}|{opportunity['key']}|{opportunity.get('evidence_digest') or source_digest_value}".encode("utf-8")
        ).hexdigest()
        now_iso = _now_iso(now)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM discovery_policies WHERE id=?", (policy["id"],)).fetchone()
            if not current or not bool(current["enabled"]) or int(current["revision"]) != int(policy["revision"]):
                db.rollback()
                return None
            duplicate = db.execute("SELECT id FROM discovery_jobs WHERE trigger_key=?", (trigger_key,)).fetchone()
            if duplicate:
                db.rollback()
                return None
            reserved = db.execute(
                "SELECT COALESCE(SUM(reserved_runs),0),COALESCE(SUM(reserved_candidates),0) "
                "FROM discovery_jobs WHERE policy_id=? AND budget_date=? "
                "AND status!='cancelled'",
                (policy["id"], budget_date),
            ).fetchone()
            if int(reserved[0]) >= int(policy["max_daily_runs"]) or int(reserved[1]) >= int(policy["max_daily_candidates"]):
                db.rollback()
                return None
            remaining_candidates = int(policy["max_daily_candidates"]) - int(reserved[1])
            reserve_candidates = max(1, min(remaining_candidates, 1))
            job_id = uuid.uuid4().hex
            db.execute(
                """
                INSERT INTO discovery_jobs(
                    id,policy_id,policy_revision,trigger_key,trigger_type,trigger_json,opportunity_key,
                    source_digest,status,run_id,lease_owner,lease_until,provider_started,budget_date,
                    reserved_runs,reserved_candidates,usage_json,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id, policy["id"], int(policy["revision"]), trigger_key, trigger_type,
                    _json(opportunity), opportunity["key"], source_digest_value, "queued", "", "", 0, 0,
                    budget_date, 1, reserve_candidates, _json({"status": "unknown"}), "", now_iso, now_iso,
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM discovery_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_public(row)

    def claim_next(self, *, owner: str, now: float, lease_seconds: int = 180) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT j.* FROM discovery_jobs j
                JOIN discovery_policies p ON p.id=j.policy_id
                WHERE j.status='queued' AND p.enabled=1 AND p.revision=j.policy_revision
                ORDER BY j.created_at ASC LIMIT 1
                """
            ).fetchone()
            if not row:
                db.rollback()
                return None
            db.execute(
                "UPDATE discovery_jobs SET status='claimed',lease_owner=?,lease_until=?,updated_at=? WHERE id=? AND status='queued'",
                (owner[:80], int(now) + max(30, lease_seconds), _now_iso(now), row["id"]),
            )
            db.commit()
            return self._job_public(db.execute("SELECT * FROM discovery_jobs WHERE id=?", (row["id"],)).fetchone())

    def attach_run(self, job_id: str, run_id: str, *, now: float) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE discovery_jobs SET status='running',run_id=?,provider_started=1,updated_at=? "
                "WHERE id=? AND status='claimed'",
                (run_id, _now_iso(now), job_id),
            )
            if not cur.rowcount:
                raise DiscoveryError("主动发现任务租约已失效。", 409)
            return self._job_public(db.execute("SELECT * FROM discovery_jobs WHERE id=?", (job_id,)).fetchone())

    def release_claim(self, job_id: str, error: str, *, now: float) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE discovery_jobs SET status='queued',lease_owner='',lease_until=0,error=?,updated_at=? "
                "WHERE id=? AND status='claimed' AND provider_started=0",
                (error[:1000], _now_iso(now), job_id),
            )

    def running_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [self._job_public(row) for row in db.execute(
                "SELECT * FROM discovery_jobs WHERE status='running' ORDER BY created_at ASC LIMIT ?", (limit,)
            ).fetchall()]

    def finish(self, job_id: str, *, status: str, idea_ids: list[str] | None = None,
               error: str = "", usage: dict[str, Any] | None = None, now: float | None = None) -> dict[str, Any]:
        if status not in TERMINAL_JOB_STATES:
            raise DiscoveryError("主动发现任务结束状态无效。")
        stamp = time.time() if now is None else now
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM discovery_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise DiscoveryError("主动发现任务不存在。", 404)
            db.execute(
                "UPDATE discovery_jobs SET status=?,usage_json=?,error=?,lease_owner='',lease_until=0,updated_at=? WHERE id=?",
                (status, _json(usage or {"status": "unknown"}), error[:1200], _now_iso(stamp), job_id),
            )
            if status == "succeeded":
                policy = db.execute("SELECT max_daily_notices FROM discovery_policies WHERE id=?", (row["policy_id"],)).fetchone()
                notice_limit = int(policy[0]) if policy else 0
                used_notices = int(db.execute(
                    "SELECT COUNT(*) FROM discovery_notices WHERE policy_id=? AND budget_date=?",
                    (row["policy_id"], row["budget_date"]),
                ).fetchone()[0])
                for idea_id in idea_ids or []:
                    if used_notices < notice_limit:
                        db.execute(
                            """
                            INSERT OR IGNORE INTO discovery_notices(
                                id,policy_id,idea_id,opportunity_key,channel,state,scheduled_at,sent_at,created_at,budget_date
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (uuid.uuid4().hex, row["policy_id"], idea_id, row["opportunity_key"],
                             "inbox", "unread", 0, int(stamp), _now_iso(stamp), row["budget_date"]),
                        )
                        db.execute("UPDATE ideas SET unread=1 WHERE id=?", (idea_id,))
                        used_notices += 1
                    else:
                        db.execute("UPDATE ideas SET unread=0 WHERE id=?", (idea_id,))
            result = db.execute("SELECT * FROM discovery_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_public(result)

    def mark_notice_read(self, policy_id: str, idea_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE discovery_notices SET state='read' WHERE policy_id=? AND idea_id=? AND state='unread'",
                (policy_id, idea_id),
            )

    def mark_idea_read(self, idea_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE discovery_notices SET state='read' WHERE idea_id=? AND state='unread'",
                (idea_id,),
            )

    def expire_candidates(self, policy_id: str, active_keys: set[str]) -> int:
        with self._lock, self._connect() as db:
            run_ids = [str(row[0]) for row in db.execute(
                "SELECT id FROM idea_runs WHERE policy_id=? AND origin='automatic'", (policy_id,)
            ).fetchall()]
            if not run_ids:
                return 0
            placeholders = ",".join("?" for _ in run_ids)
            params: list[Any] = [*run_ids]
            condition = ""
            if active_keys:
                condition = " AND opportunity_key NOT IN (" + ",".join("?" for _ in active_keys) + ")"
                params.extend(sorted(active_keys))
            cur = db.execute(
                f"UPDATE ideas SET validity='expired',updated=? WHERE origin='automatic' "
                f"AND stage='candidate' AND run_id IN ({placeholders}){condition}",
                [int(time.time()), *params],
            )
            return int(cur.rowcount)

    def state(self, persona: str, *, now: float | None = None, jobs_limit: int = 12) -> dict[str, Any]:
        stamp = time.time() if now is None else now
        policy = self.get_policy(persona)
        if not policy:
            return {
                "policy": None, "today": {"runs": 0, "candidates": 0, "notices": 0},
                "unread": 0, "waiting": 0, "jobs": [], "source_health": {},
                "server_now": int(stamp),
            }
        budget_date = _local_date(stamp, policy["timezone"])
        with self._connect() as db:
            budget = db.execute(
                "SELECT COALESCE(SUM(reserved_runs),0),COALESCE(SUM(reserved_candidates),0) "
                "FROM discovery_jobs WHERE policy_id=? AND budget_date=? AND status!='cancelled'",
                (policy["id"], budget_date),
            ).fetchone()
            notices = db.execute(
                "SELECT COUNT(*) FROM discovery_notices WHERE policy_id=? AND state='unread'", (policy["id"],)
            ).fetchone()[0]
            today_notices = db.execute(
                "SELECT COUNT(*) FROM discovery_notices WHERE policy_id=? AND budget_date=?",
                (policy["id"], budget_date),
            ).fetchone()[0]
            waiting = db.execute(
                "SELECT COUNT(*) FROM discovery_jobs WHERE policy_id=? AND policy_revision=? "
                "AND status IN ('queued','claimed','running')",
                (policy["id"], int(policy["revision"])),
            ).fetchone()[0]
            jobs = [self._job_public(row) for row in db.execute(
                "SELECT * FROM discovery_jobs WHERE policy_id=? ORDER BY created_at DESC LIMIT ?",
                (policy["id"], max(1, min(jobs_limit, 50))),
            ).fetchall()]
        return {
            "policy": policy,
            "today": {"runs": int(budget[0]), "candidates": int(budget[1]), "notices": int(today_notices)},
            "unread": int(notices), "waiting": int(waiting), "jobs": jobs,
            "source_health": policy.get("source_health") or {}, "server_now": int(stamp),
        }
