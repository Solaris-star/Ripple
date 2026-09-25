"""Stable content profiles and account bindings.

This domain is intentionally separate from:
- browser login profile_id in accounts.py
- Agent runtime profiles in agent_profiles.py
- Workspace users/authentication

Legacy profiles/<name> Markdown remains the compatibility source for existing
Agent/skill readers. This service adds stable IDs, immutable confirmed revisions,
account-specific overrides and reviewable analysis proposals.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import os
import shutil
import sqlite3
import threading
from typing import Any
import uuid

from .publishing import WorkflowError
from .persona import PROFILES_DIR, _FILE_ORDER, profile_exists


DEFAULT_WORKSPACE_ID = "00000000000000000000000000000001"
TARGET_KINDS = {"account", "blog"}
PROFILE_STATES = {"draft", "confirmed", "archived"}
ANALYSIS_STATES = {"queued", "running", "waiting_user", "succeeded", "failed", "cancelled", "interrupted"}
_ANALYSIS_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def validate_profile_storage_name(value: str) -> str:
    name = str(value or "").strip()
    win = PureWindowsPath(name)
    stem = name.split(".", 1)[0].upper()
    if (
        not name or len(name) > 120 or name in {".", ".."} or name.startswith((".", "_"))
        or name.endswith((" ", ".")) or "/" in name or "\\" in name or ":" in name
        or win.drive or win.is_absolute() or stem in _WINDOWS_RESERVED
    ):
        raise WorkflowError("画像名称无效；不要使用路径、盘符或 Windows 保留名称。", 422)
    root = PROFILES_DIR.resolve()
    candidate = (PROFILES_DIR / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkflowError("画像名称不能跳出画像目录。", 422) from exc
    return name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback):
    if not value:
        return deepcopy(fallback)
    try:
        parsed = json.loads(value)
        return parsed
    except (ValueError, TypeError):
        return deepcopy(fallback)


def _profile_id(workspace_id: str, legacy_name: str) -> str:
    return "cp_" + hashlib.sha256(f"{workspace_id}|{legacy_name}".encode("utf-8")).hexdigest()[:24]


def _read_legacy_files(name: str) -> dict[str, str]:
    directory = PROFILES_DIR / name
    if not directory.is_dir():
        return {}
    ordered = list(_FILE_ORDER)
    ordered.extend(sorted(path.name for path in directory.glob("*.md") if path.name not in _FILE_ORDER))
    return {
        filename: (directory / filename).read_text(encoding="utf-8") if (directory / filename).is_file() else ""
        for filename in ordered
    }


def _write_legacy_files(name: str, files: dict[str, str]) -> None:
    name = validate_profile_storage_name(name)
    directory = PROFILES_DIR / name
    directory.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[Path, Path, Path | None]] = []
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for filename, content in files.items():
            if not filename.endswith(".md") or "/" in filename or "\\" in filename or filename.startswith("."):
                continue
            path = directory / filename
            token = uuid.uuid4().hex
            tmp = directory / f".{filename}.{token}.tmp"
            backup = directory / f".{filename}.{token}.bak" if path.exists() else None
            tmp.write_text(str(content), encoding="utf-8")
            prepared.append((path, tmp, backup))
        for path, tmp, backup in prepared:
            if backup is not None:
                os.replace(path, backup)
            try:
                os.replace(tmp, path)
            except Exception:
                if backup is not None and backup.exists():
                    os.replace(backup, path)
                raise
            replaced.append((path, backup))
        for _path, backup in replaced:
            if backup is not None and backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    # Backup cleanup is best-effort after every target file has been replaced.
                    pass
    except Exception:
        for path, _tmp, backup in reversed(prepared):
            try:
                if backup is not None and backup.exists():
                    if path.exists():
                        path.unlink()
                    os.replace(backup, path)
                elif backup is None and path.exists():
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        for _path, tmp, backup in prepared:
            try:
                if tmp.exists():
                    tmp.unlink()
                if backup is not None and backup.exists():
                    backup.unlink()
            except OSError:
                pass


def _restore_confirmed_mirror(name: str, files: dict[str, str]) -> None:
    """Restore profiles/<name>/ to the exact confirmed Markdown set.

    Callers must durably capture any external differences before invoking this helper.
    """
    name = validate_profile_storage_name(name)
    _write_legacy_files(name, files)
    directory = PROFILES_DIR / name
    confirmed_names = {
        str(filename)
        for filename in files
        if str(filename).endswith(".md") and "/" not in str(filename)
        and "\\" not in str(filename) and not str(filename).startswith(".")
    }
    for path in sorted(directory.glob("*.md")):
        if path.name not in confirmed_names:
            path.unlink()




class ContentProfileService:
    def __init__(self, db_path: Path, *, workspace_id: str = DEFAULT_WORKSPACE_ID):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_id = workspace_id
        self._lock = threading.RLock()
        self._init_schema()
        self.sync_legacy_profiles()
        self._interrupt_runs()

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
                CREATE TABLE IF NOT EXISTS content_profiles (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    legacy_name TEXT NOT NULL UNIQUE,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS content_profiles_workspace_idx
                    ON content_profiles(workspace_id, state, display_name);

                CREATE TABLE IF NOT EXISTS profile_revisions (
                    profile_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    files_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, revision),
                    FOREIGN KEY(profile_id) REFERENCES content_profiles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS account_profile_bindings (
                    workspace_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    profile_revision INTEGER NOT NULL,
                    overrides_json TEXT NOT NULL DEFAULT '{}',
                    binding_revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, target_kind, account_id),
                    FOREIGN KEY(profile_id) REFERENCES content_profiles(id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS account_profile_bindings_profile_idx
                    ON account_profile_bindings(profile_id);

                CREATE TABLE IF NOT EXISTS profile_analysis_runs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    capability_json TEXT NOT NULL DEFAULT '{}',
                    sample_manifest_json TEXT NOT NULL DEFAULT '[]',
                    proposal_json TEXT NOT NULL DEFAULT '{}',
                    model_json TEXT NOT NULL DEFAULT '{}',
                    request_digest TEXT NOT NULL DEFAULT '',
                    apply_result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS profile_analysis_runs_target_idx
                    ON profile_analysis_runs(workspace_id, target_kind, account_id, created_at DESC);
                """
            )
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(profile_analysis_runs)").fetchall()}
            if "request_digest" not in columns:
                db.execute("ALTER TABLE profile_analysis_runs ADD COLUMN request_digest TEXT NOT NULL DEFAULT ''")
            if "apply_result_json" not in columns:
                db.execute("ALTER TABLE profile_analysis_runs ADD COLUMN apply_result_json TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS profile_analysis_runs_request_idx "
                "ON profile_analysis_runs(workspace_id, request_digest) WHERE request_digest<>''"
            )

    def _interrupt_runs(self) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE profile_analysis_runs SET status='interrupted',error=?,updated_at=? "
                "WHERE status IN ('queued','running')",
                ("服务在画像分析期间重启；不会自动重复模型调用。", _now_iso()),
            )

    @staticmethod
    def _profile_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "id": value["id"],
            "workspace_id": value["workspace_id"],
            "display_name": value["display_name"],
            "legacy_name": value["legacy_name"],
            "current_revision": int(value["current_revision"]),
            "state": value["state"],
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
        }

    @staticmethod
    def _binding_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "workspace_id": value["workspace_id"],
            "target_kind": value["target_kind"],
            "account_id": value["account_id"],
            "profile_id": value["profile_id"],
            "profile_revision": int(value["profile_revision"]),
            "overrides": _loads(value["overrides_json"], {}),
            "binding_revision": int(value["binding_revision"]),
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
        }

    def sync_legacy_profiles(self) -> None:
        names = sorted(
            path.name for path in PROFILES_DIR.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        ) if PROFILES_DIR.is_dir() else []

        for name in names:
            try:
                validate_profile_storage_name(name)
            except WorkflowError:
                continue
            files = _read_legacy_files(name)
            confirmed_to_restore: dict[str, str] | None = None

            with self._lock:
                with self._connect() as db:
                    # Serialize legacy import/draft registration across service instances.
                    db.execute("BEGIN IMMEDIATE")
                    pid = _profile_id(self.workspace_id, name)
                    current = db.execute(
                        "SELECT * FROM content_profiles WHERE workspace_id=? AND legacy_name=?",
                        (self.workspace_id, name),
                    ).fetchone()
                    if current:
                        if current["state"] == "archived":
                            continue
                        current_revision = int(current["current_revision"])
                        stored = db.execute(
                            "SELECT files_json FROM profile_revisions WHERE profile_id=? AND revision=?",
                            (current["id"], current_revision),
                        ).fetchone()
                        confirmed_files = _loads(stored["files_json"], {}) if stored else {}
                        if stored and confirmed_files == files:
                            continue
                        encoded = _json(files)
                        duplicate = db.execute(
                            "SELECT revision FROM profile_revisions WHERE profile_id=? AND files_json=? "
                            "ORDER BY revision DESC LIMIT 1",
                            (current["id"], encoded),
                        ).fetchone()
                        if not duplicate:
                            max_revision = int(db.execute(
                                "SELECT COALESCE(MAX(revision),0) AS n FROM profile_revisions WHERE profile_id=?",
                                (current["id"],),
                            ).fetchone()["n"])
                            revision = max(current_revision, max_revision) + 1
                            db.execute(
                                "INSERT INTO profile_revisions(profile_id,revision,files_json,source,status,note,created_at) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (
                                    current["id"], revision, encoded, "legacy_sync", "draft",
                                    "检测到外部 Markdown 变化；未自动生效，等待人工确认。", _now_iso(),
                                ),
                            )
                        # The draft row is committed when this connection context exits.
                        confirmed_to_restore = dict(confirmed_files)
                    else:
                        now = _now_iso()
                        db.execute(
                            "INSERT INTO content_profiles(id,workspace_id,display_name,legacy_name,current_revision,state,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (pid, self.workspace_id, name, name, 1, "confirmed", now, now),
                        )
                        db.execute(
                            "INSERT INTO profile_revisions(profile_id,revision,files_json,source,status,note,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (pid, 1, _json(files), "legacy_import", "confirmed", "从现有 profiles 目录登记。", now),
                        )

            # Restore only after the external content is durably captured as a draft.
            # A filesystem failure leaves the draft intact for a later retry/review.
            if confirmed_to_restore is not None:
                try:
                    _restore_confirmed_mirror(name, confirmed_to_restore)
                except OSError:
                    continue

    def list_profiles(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        self.sync_legacy_profiles()
        query = "SELECT * FROM content_profiles WHERE workspace_id=?"
        params: list[Any] = [self.workspace_id]
        if not include_archived:
            query += " AND state!='archived'"
        query += " ORDER BY display_name"
        with self._connect() as db:
            rows = [self._profile_public(row) for row in db.execute(query, params).fetchall()]
            counts = {
                str(row["profile_id"]): int(row["n"])
                for row in db.execute(
                    "SELECT profile_id,COUNT(*) AS n FROM account_profile_bindings WHERE workspace_id=? GROUP BY profile_id",
                    (self.workspace_id,),
                ).fetchall()
            }
        for row in rows:
            row["account_count"] = counts.get(row["id"], 0)
        return rows

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM content_profiles WHERE id=? AND workspace_id=?",
                (profile_id, self.workspace_id),
            ).fetchone()
            if not row:
                raise WorkflowError("账号画像不存在。", 404)
            profile = self._profile_public(row)
            revision = db.execute(
                "SELECT * FROM profile_revisions WHERE profile_id=? AND revision=?",
                (profile_id, profile["current_revision"]),
            ).fetchone()
            bindings = [
                self._binding_public(value) for value in db.execute(
                    "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND profile_id=? ORDER BY target_kind,account_id",
                    (self.workspace_id, profile_id),
                ).fetchall()
            ]
        profile["files"] = _loads(revision["files_json"], {}) if revision else {}
        profile["revision_source"] = str(revision["source"]) if revision else ""
        profile["bindings"] = bindings
        return profile

    def get_revision(self, profile_id: str, revision: int) -> dict[str, Any]:
        with self._connect() as db:
            profile = db.execute(
                "SELECT * FROM content_profiles WHERE id=? AND workspace_id=?",
                (profile_id, self.workspace_id),
            ).fetchone()
            if not profile:
                raise WorkflowError("账号画像不存在。", 404)
            row = db.execute(
                "SELECT * FROM profile_revisions WHERE profile_id=? AND revision=?",
                (profile_id, int(revision)),
            ).fetchone()
            if not row:
                raise WorkflowError("账号画像版本不存在。", 404)
        return {
            "profile": self._profile_public(profile),
            "revision": int(row["revision"]),
            "status": str(row["status"]),
            "source": str(row["source"]),
            "files": _loads(row["files_json"], {}),
            "created_at": str(row["created_at"]),
        }

    def profile_for_legacy_name(self, name: str) -> dict[str, Any] | None:
        self.sync_legacy_profiles()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM content_profiles WHERE workspace_id=? AND legacy_name=? AND state!='archived'",
                (self.workspace_id, name),
            ).fetchone()
            return self._profile_public(row) if row else None

    def register_legacy_profile(self, name: str) -> dict[str, Any]:
        if not profile_exists(name):
            raise WorkflowError("账号画像不存在。", 404)
        self.sync_legacy_profiles()
        profile = self.profile_for_legacy_name(name)
        if not profile:
            raise WorkflowError("画像登记失败。", 500)
        return self.get_profile(profile["id"])

    def create_profile(self, display_name: str, files: dict[str, str], *, source: str = "manual") -> dict[str, Any]:
        name = validate_profile_storage_name(display_name)
        normalized = {
            str(filename): str(content)
            for filename, content in files.items()
            if str(filename).endswith(".md") and "/" not in str(filename) and "\\" not in str(filename)
        }
        if not normalized:
            raise WorkflowError("画像内容不能为空。", 422)

        directory = PROFILES_DIR / name
        owned_directory: Path | None = None
        with self._lock:
            try:
                with self._connect() as db:
                    # Cross-instance serialization: the existence check, exclusive directory
                    # claim, DB insert and mirror write belong to one write transaction.
                    db.execute("BEGIN IMMEDIATE")
                    existing = db.execute(
                        "SELECT * FROM content_profiles WHERE workspace_id=? AND (display_name=? OR legacy_name=?)",
                        (self.workspace_id, name, name),
                    ).fetchone()
                    if existing and existing["state"] != "archived":
                        raise WorkflowError(f"画像「{name}」已存在。", 409)
                    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                    try:
                        directory.mkdir(exist_ok=False)
                    except FileExistsError as exc:
                        raise WorkflowError(f"画像「{name}」已存在。", 409) from exc
                    owned_directory = directory

                    now = _now_iso()
                    if existing:
                        pid = str(existing["id"])
                        max_revision = int(db.execute(
                            "SELECT COALESCE(MAX(revision),0) AS n FROM profile_revisions WHERE profile_id=?",
                            (pid,),
                        ).fetchone()["n"])
                        revision = max_revision + 1
                        db.execute(
                            "UPDATE content_profiles SET display_name=?,legacy_name=?,current_revision=?,state='confirmed',updated_at=? WHERE id=?",
                            (name, name, revision, now, pid),
                        )
                    else:
                        pid = "cp_" + uuid.uuid4().hex[:24]
                        revision = 1
                        db.execute(
                            "INSERT INTO content_profiles(id,workspace_id,display_name,legacy_name,current_revision,state,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (pid, self.workspace_id, name, name, revision, "confirmed", now, now),
                        )
                    db.execute(
                        "INSERT INTO profile_revisions(profile_id,revision,files_json,source,status,note,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (pid, revision, _json(normalized), source[:40], "confirmed", "新建账号画像。", now),
                    )
                    _write_legacy_files(name, normalized)
            except Exception:
                if owned_directory is not None and owned_directory.parent.resolve() == PROFILES_DIR.resolve():
                    shutil.rmtree(owned_directory, ignore_errors=True)
                raise
        return self.get_profile(pid)

    def rename_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        name = str(display_name or "").strip()
        if not name or len(name) > 120:
            raise WorkflowError("画像名称不能为空且不能超过 120 个字符。", 422)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM content_profiles WHERE id=? AND workspace_id=? AND state!='archived'",
                (profile_id, self.workspace_id),
            ).fetchone()
            if not row:
                raise WorkflowError("账号画像不存在。", 404)
            duplicate = db.execute(
                "SELECT id FROM content_profiles WHERE workspace_id=? AND display_name=? AND state!='archived' AND id<>?",
                (self.workspace_id, name, profile_id),
            ).fetchone()
            if duplicate:
                raise WorkflowError(f"画像名称「{name}」已存在。", 409)
            db.execute(
                "UPDATE content_profiles SET display_name=?,updated_at=? WHERE id=?",
                (name, _now_iso(), profile_id),
            )
        return self.get_profile(profile_id)

    def copy_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        source = self.get_profile(profile_id)
        return self.create_profile(display_name, dict(source.get("files") or {}), source="profile_copy")

    def save_revision(
        self,
        profile_id: str,
        files: dict[str, str],
        *,
        source: str,
        expected_revision: int,
        note: str = "",
        confirm: bool = True,
    ) -> dict[str, Any]:
        normalized = {
            str(name): str(content)
            for name, content in files.items()
            if str(name).endswith(".md") and "/" not in str(name) and "\\" not in str(name)
        }
        if not normalized:
            raise WorkflowError("画像内容不能为空。", 422)
        now = _now_iso()
        status = "confirmed" if confirm else "draft"
        with self._lock:
            profile = self.get_profile(profile_id)
            if int(profile["current_revision"]) != int(expected_revision):
                raise WorkflowError("画像已被修改，请刷新后重试。", 409)
            legacy_written = False
            try:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT current_revision FROM content_profiles WHERE id=? AND workspace_id=?",
                        (profile_id, self.workspace_id),
                    ).fetchone()
                    if not current:
                        raise WorkflowError("账号画像不存在。", 404)
                    if int(current["current_revision"]) != int(expected_revision):
                        raise WorkflowError("画像已被修改，请刷新后重试。", 409)
                    max_revision = int(db.execute(
                        "SELECT COALESCE(MAX(revision),0) AS n FROM profile_revisions WHERE profile_id=?",
                        (profile_id,),
                    ).fetchone()["n"])
                    revision = max(int(expected_revision), max_revision) + 1
                    db.execute(
                        "INSERT INTO profile_revisions(profile_id,revision,files_json,source,status,note,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (profile_id, revision, _json(normalized), source[:40], status, note[:500], now),
                    )
                    if confirm:
                        db.execute(
                            "UPDATE content_profiles SET current_revision=?,updated_at=? WHERE id=?",
                            (revision, now, profile_id),
                        )
                        db.execute(
                            "UPDATE account_profile_bindings SET profile_revision=?,updated_at=? WHERE profile_id=?",
                            (revision, now, profile_id),
                        )
                        _write_legacy_files(profile["legacy_name"], normalized)
                        legacy_written = True
            except Exception:
                if confirm and legacy_written:
                    try:
                        _write_legacy_files(profile["legacy_name"], dict(profile.get("files") or {}))
                    except Exception:
                        pass
                raise
        return self.get_profile(profile_id)

    def bind(
        self,
        *,
        target_kind: str,
        account_id: str,
        profile_id: str,
        overrides: dict[str, Any] | None = None,
        expected_binding_revision: int | None = None,
    ) -> dict[str, Any]:
        if target_kind not in TARGET_KINDS:
            raise WorkflowError("不支持的账号类型。", 422)
        profile = self.get_profile(profile_id)
        now = _now_iso()
        with self._lock, self._connect() as db:
            current = db.execute(
                "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                (self.workspace_id, target_kind, account_id),
            ).fetchone()
            if current and expected_binding_revision is not None and int(current["binding_revision"]) != int(expected_binding_revision):
                raise WorkflowError("账号画像关联已变化，请刷新后重试。", 409)
            revision = int(current["binding_revision"]) + 1 if current else 1
            created_at = str(current["created_at"]) if current else now
            db.execute(
                """
                INSERT INTO account_profile_bindings(
                    workspace_id,target_kind,account_id,profile_id,profile_revision,overrides_json,
                    binding_revision,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id,target_kind,account_id) DO UPDATE SET
                    profile_id=excluded.profile_id,profile_revision=excluded.profile_revision,
                    overrides_json=excluded.overrides_json,binding_revision=excluded.binding_revision,
                    updated_at=excluded.updated_at
                """,
                (
                    self.workspace_id, target_kind, account_id, profile_id,
                    int(profile["current_revision"]), _json(overrides or {}),
                    revision, created_at, now,
                ),
            )
            row = db.execute(
                "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                (self.workspace_id, target_kind, account_id),
            ).fetchone()
        return self._binding_public(row)

    def unbind(self, *, target_kind: str, account_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                (self.workspace_id, target_kind, account_id),
            )

    def bindings_for_profile(self, profile_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [
                self._binding_public(row) for row in db.execute(
                    "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND profile_id=? ORDER BY target_kind,account_id",
                    (self.workspace_id, profile_id),
                ).fetchall()
            ]

    def archive_profile(self, profile_id: str) -> dict[str, Any]:
        bindings = self.bindings_for_profile(profile_id)
        if bindings:
            raise WorkflowError("该画像仍有关联账号，请先解除关联再归档。", 409)
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE content_profiles SET state='archived',updated_at=? WHERE id=? AND workspace_id=?",
                (_now_iso(), profile_id, self.workspace_id),
            )
            if not cur.rowcount:
                raise WorkflowError("账号画像不存在。", 404)
        return self.get_profile(profile_id)

    def binding(self, *, target_kind: str, account_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                (self.workspace_id, target_kind, account_id),
            ).fetchone()
            return self._binding_public(row) if row else None

    def all_bindings(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [
                self._binding_public(row) for row in db.execute(
                    "SELECT * FROM account_profile_bindings WHERE workspace_id=? ORDER BY target_kind,account_id",
                    (self.workspace_id,),
                ).fetchall()
            ]

    def resolve(
        self,
        *,
        profile_id: str = "",
        legacy_name: str = "",
        target_kind: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
        binding = None
        if account_id:
            binding = self.binding(target_kind=target_kind or "account", account_id=account_id)
            if binding:
                profile_id = binding["profile_id"]
        if not profile_id and legacy_name:
            profile = self.profile_for_legacy_name(legacy_name)
            profile_id = profile["id"] if profile else ""
        if not profile_id:
            return {
                "workspace_id": self.workspace_id,
                "profile": None,
                "binding": binding,
                "effective_files": {},
                "scope": "generic",
            }
        profile = self.get_profile(profile_id)
        effective = deepcopy(profile["files"])
        overrides = deepcopy((binding or {}).get("overrides") or {})
        return {
            "workspace_id": self.workspace_id,
            "profile": {
                k: profile[k] for k in (
                    "id", "display_name", "legacy_name", "current_revision", "state"
                )
            },
            "binding": binding,
            "effective_files": effective,
            "overrides": overrides,
            "scope": "account" if binding else "profile",
        }

    def analysis_capability(self, *, target_kind: str, account_id: str, platform: str = "") -> dict[str, Any]:
        # Historical content acquisition is platform-specific and must be explicitly
        # verified. Import/manual samples remain available on every target.
        automatic_supported = platform == "xiaohongshu" and target_kind == "account"
        automatic_planned = platform in {"xiaohongshu", "douyin"}
        return {
            "target_kind": target_kind,
            "account_id": account_id,
            "platform": platform,
            "automatic_history_supported": automatic_supported,
            "automatic_history_planned": automatic_planned,
            "automatic_history_fields": ["title", "url", "metrics"] if automatic_supported else [],
            "import_samples_supported": True,
            "accepted_sample_fields": ["title", "body", "url", "published_at", "kind"],
            "note": (
                "小红书可在你明确确认后只读同步当前账号最多 30 条已发布作品的标题、链接与指标；当前不会读取完整正文。"
                if automatic_supported else
                ("该平台已进入只读历史能力适配范围，但当前尚未启用自动读取；可先导入代表作品。"
                 if automatic_planned else "该平台暂以导入代表作品为主。")
            ),
        }

    def analysis_by_request_digest(self, request_digest: str) -> dict[str, Any] | None:
        digest = str(request_digest or "")[:128]
        if not digest:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM profile_analysis_runs WHERE workspace_id=? AND request_digest=?",
                (self.workspace_id, digest),
            ).fetchone()
        return self.get_analysis(str(row["id"])) if row else None

    def create_analysis(
        self,
        *,
        target_kind: str,
        account_id: str,
        profile_id: str = "",
        capability: dict[str, Any] | None = None,
        samples: list[dict[str, Any]] | None = None,
        model: dict[str, Any] | None = None,
        request_digest: str = "",
    ) -> dict[str, Any]:
        if target_kind not in TARGET_KINDS:
            raise WorkflowError("不支持的账号类型。", 422)
        if profile_id:
            self.get_profile(profile_id)
        request_digest = str(request_digest or "")[:128]
        existing_id = ""
        reused = False
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_digest:
                existing = db.execute(
                    "SELECT id FROM profile_analysis_runs WHERE workspace_id=? AND request_digest=?",
                    (self.workspace_id, request_digest),
                ).fetchone()
                if existing:
                    existing_id = str(existing["id"])
                    reused = True
            if not existing_id:
                run_id = "pa_" + uuid.uuid4().hex
                now = _now_iso()
                db.execute(
                    """
                    INSERT INTO profile_analysis_runs(
                        id,workspace_id,target_kind,account_id,profile_id,status,
                        capability_json,sample_manifest_json,proposal_json,model_json,request_digest,error,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id, self.workspace_id, target_kind, account_id, profile_id,
                        "waiting_user", _json(capability or {}), _json(samples or []), "{}",
                        _json(model or {}), request_digest, "", now, now,
                    ),
                )
                existing_id = run_id
        result = self.get_analysis(existing_id)
        result["reused"] = reused
        return result

    def get_analysis(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM profile_analysis_runs WHERE id=? AND workspace_id=?",
                (run_id, self.workspace_id),
            ).fetchone()
            if not row:
                raise WorkflowError("画像分析任务不存在。", 404)
            value = dict(row)
        return {
            "id": value["id"],
            "workspace_id": value["workspace_id"],
            "target_kind": value["target_kind"],
            "account_id": value["account_id"],
            "profile_id": value["profile_id"],
            "status": value["status"],
            "capability": _loads(value["capability_json"], {}),
            "samples": _loads(value["sample_manifest_json"], []),
            "proposal": _loads(value["proposal_json"], {}),
            "model": _loads(value["model_json"], {}),
            "request_digest": str(value.get("request_digest") or ""),
            "apply_result": _loads(value.get("apply_result_json"), {}),
            "error": value["error"],
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
        }

    def latest_analysis(self, *, target_kind: str, account_id: str) -> dict[str, Any] | None:
        if target_kind not in TARGET_KINDS:
            raise WorkflowError("不支持的账号类型。", 422)
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM profile_analysis_runs WHERE workspace_id=? AND target_kind=? AND account_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (self.workspace_id, target_kind, account_id),
            ).fetchone()
        return self.get_analysis(str(row["id"])) if row else None

    def apply_analysis_result(
        self,
        run_id: str,
        *,
        profile_id: str,
        display_name: str,
        expected_profile_revision: int,
        expected_binding_revision: int,
        expected_binding_profile_id: str,
        overrides: dict[str, Any],
        bind_target: bool,
    ) -> dict[str, Any]:
        with self._lock:
            run = self.get_analysis(run_id)
            if run.get("apply_result"):
                return deepcopy(run["apply_result"])
            if run["status"] != "succeeded" or not run.get("proposal"):
                raise WorkflowError("画像分析尚未形成可应用提案。", 409)

            proposal_files = {
                str(name): str(content)
                for name, content in dict(run["proposal"].get("files") or {}).items()
                if str(name).endswith(".md") and "/" not in str(name) and "\\" not in str(name)
            }
            if not proposal_files:
                raise WorkflowError("画像提案没有可应用的画像内容。", 409)

            previous_files: dict[str, str] | None = None
            legacy_name = ""
            owned_directory: Path | None = None
            legacy_written = False
            try:
                with self._connect() as db:
                    # Serialize apply/replay/create against every other profile writer.
                    db.execute("BEGIN IMMEDIATE")
                    persisted_run = db.execute(
                        "SELECT * FROM profile_analysis_runs WHERE id=? AND workspace_id=?",
                        (run_id, self.workspace_id),
                    ).fetchone()
                    if not persisted_run:
                        raise WorkflowError("画像分析任务不存在。", 404)
                    existing_apply = _loads(persisted_run["apply_result_json"], {})
                    if existing_apply:
                        return existing_apply

                    current_binding = db.execute(
                        "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                        (self.workspace_id, run["target_kind"], run["account_id"]),
                    ).fetchone()
                    current_binding_revision = int(current_binding["binding_revision"]) if current_binding else 0
                    current_binding_profile_id = str(current_binding["profile_id"]) if current_binding else ""
                    if (
                        current_binding_revision != int(expected_binding_revision)
                        or current_binding_profile_id != str(expected_binding_profile_id or "")
                    ):
                        raise WorkflowError("账号画像关联已在分析期间变化，请重新分析后再应用。", 409)

                    now = _now_iso()
                    target_profile_id = str(profile_id or "")
                    if target_profile_id:
                        row = db.execute(
                            "SELECT * FROM content_profiles WHERE id=? AND workspace_id=? AND state!='archived'",
                            (target_profile_id, self.workspace_id),
                        ).fetchone()
                        if not row:
                            raise WorkflowError("账号画像不存在。", 404)
                        if int(row["current_revision"]) != int(expected_profile_revision):
                            raise WorkflowError("画像已在分析期间更新，请重新分析后再应用。", 409)
                        current_revision = db.execute(
                            "SELECT * FROM profile_revisions WHERE profile_id=? AND revision=?",
                            (target_profile_id, int(row["current_revision"])),
                        ).fetchone()
                        previous_files = _loads(current_revision["files_json"], {}) if current_revision else {}
                        files = dict(proposal_files)
                        if previous_files.get("preferences.md"):
                            files["preferences.md"] = previous_files["preferences.md"]
                        max_revision = int(db.execute(
                            "SELECT COALESCE(MAX(revision),0) AS n FROM profile_revisions WHERE profile_id=?",
                            (target_profile_id,),
                        ).fetchone()["n"])
                        revision = max(max_revision, int(row["current_revision"])) + 1
                        db.execute(
                            "INSERT INTO profile_revisions(profile_id,revision,files_json,source,status,note,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (
                                target_profile_id, revision, _json(files), "agent_analysis", "confirmed",
                                "用户确认账号样本分析提案。", now,
                            ),
                        )
                        db.execute(
                            "UPDATE content_profiles SET current_revision=?,updated_at=? WHERE id=?",
                            (revision, now, target_profile_id),
                        )
                        db.execute(
                            "UPDATE account_profile_bindings SET profile_revision=?,updated_at=? WHERE profile_id=?",
                            (revision, now, target_profile_id),
                        )
                        legacy_name = str(row["legacy_name"])
                    else:
                        name = validate_profile_storage_name(display_name)
                        legacy_name = name
                        existing = db.execute(
                            "SELECT * FROM content_profiles WHERE workspace_id=? AND (display_name=? OR legacy_name=?)",
                            (self.workspace_id, name, name),
                        ).fetchone()
                        if existing and existing["state"] != "archived":
                            raise WorkflowError(f"画像「{name}」已存在。", 409)

                        directory = PROFILES_DIR / name
                        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                        try:
                            directory.mkdir(exist_ok=False)
                        except FileExistsError as exc:
                            raise WorkflowError(f"画像「{name}」已存在。", 409) from exc
                        owned_directory = directory
                        files = dict(proposal_files)

                        if existing:
                            target_profile_id = str(existing["id"])
                            max_revision = int(db.execute(
                                "SELECT COALESCE(MAX(revision),0) AS n FROM profile_revisions WHERE profile_id=?",
                                (target_profile_id,),
                            ).fetchone()["n"])
                            revision = max_revision + 1
                            db.execute(
                                "UPDATE content_profiles SET display_name=?,legacy_name=?,current_revision=?,state='confirmed',updated_at=? WHERE id=?",
                                (name, name, revision, now, target_profile_id),
                            )
                        else:
                            target_profile_id = "cp_" + uuid.uuid4().hex[:24]
                            revision = 1
                            db.execute(
                                "INSERT INTO content_profiles(id,workspace_id,display_name,legacy_name,current_revision,state,created_at,updated_at) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (target_profile_id, self.workspace_id, name, name, revision, "confirmed", now, now),
                            )
                        db.execute(
                            "INSERT INTO profile_revisions(profile_id,revision,files_json,source,status,note,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (
                                target_profile_id, revision, _json(files), "agent_analysis", "confirmed",
                                "用户确认账号样本分析提案。", now,
                            ),
                        )

                    binding_public = None
                    if bind_target:
                        if current_binding and str(current_binding["profile_id"]) == target_profile_id:
                            binding_row = db.execute(
                                "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                                (self.workspace_id, run["target_kind"], run["account_id"]),
                            ).fetchone()
                        else:
                            binding_revision = current_binding_revision + 1 if current_binding else 1
                            created_at = str(current_binding["created_at"]) if current_binding else now
                            db.execute(
                                """
                                INSERT INTO account_profile_bindings(
                                    workspace_id,target_kind,account_id,profile_id,profile_revision,overrides_json,
                                    binding_revision,created_at,updated_at
                                ) VALUES(?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(workspace_id,target_kind,account_id) DO UPDATE SET
                                    profile_id=excluded.profile_id,profile_revision=excluded.profile_revision,
                                    overrides_json=excluded.overrides_json,binding_revision=excluded.binding_revision,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    self.workspace_id, run["target_kind"], run["account_id"], target_profile_id,
                                    revision, _json(overrides or {}), binding_revision, created_at, now,
                                ),
                            )
                            binding_row = db.execute(
                                "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND target_kind=? AND account_id=?",
                                (self.workspace_id, run["target_kind"], run["account_id"]),
                            ).fetchone()
                        binding_public = self._binding_public(binding_row) if binding_row else None

                    _write_legacy_files(legacy_name, files)
                    legacy_written = True

                    profile_row = db.execute(
                        "SELECT * FROM content_profiles WHERE id=? AND workspace_id=?",
                        (target_profile_id, self.workspace_id),
                    ).fetchone()
                    profile_public = self._profile_public(profile_row)
                    profile_public["files"] = dict(files)
                    profile_public["revision_source"] = "agent_analysis"
                    profile_public["bindings"] = [
                        self._binding_public(value) for value in db.execute(
                            "SELECT * FROM account_profile_bindings WHERE workspace_id=? AND profile_id=? "
                            "ORDER BY target_kind,account_id",
                            (self.workspace_id, target_profile_id),
                        ).fetchall()
                    ]
                    result = {"profile": profile_public, "binding": binding_public}
                    db.execute(
                        "UPDATE profile_analysis_runs SET apply_result_json=?,updated_at=? "
                        "WHERE id=? AND workspace_id=?",
                        (_json(result), now, run_id, self.workspace_id),
                    )
            except Exception:
                if legacy_written and previous_files is not None:
                    try:
                        _write_legacy_files(legacy_name, previous_files)
                    except Exception:
                        pass
                if owned_directory is not None and owned_directory.parent.resolve() == PROFILES_DIR.resolve():
                    shutil.rmtree(owned_directory, ignore_errors=True)
                raise
            return result

    def set_analysis_status(self, run_id: str, status: str, *, error: str = "") -> dict[str, Any]:
        if status not in ANALYSIS_STATES:
            raise WorkflowError("画像分析状态无效。", 422)
        with self._lock, self._connect() as db:
            current = db.execute(
                "SELECT status FROM profile_analysis_runs WHERE id=? AND workspace_id=?",
                (run_id, self.workspace_id),
            ).fetchone()
            if not current:
                raise WorkflowError("画像分析任务不存在。", 404)
            current_status = str(current["status"])
            if current_status in _ANALYSIS_TERMINAL_STATES:
                return self.get_analysis(run_id)
            db.execute(
                "UPDATE profile_analysis_runs SET status=?,error=?,updated_at=? WHERE id=? AND workspace_id=?",
                (status, error[:1200], _now_iso(), run_id, self.workspace_id),
            )
        return self.get_analysis(run_id)

    def save_analysis_proposal(self, run_id: str, proposal: dict[str, Any], *, error: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            current = db.execute(
                "SELECT status FROM profile_analysis_runs WHERE id=? AND workspace_id=?",
                (run_id, self.workspace_id),
            ).fetchone()
            if not current:
                raise WorkflowError("画像分析任务不存在。", 404)
            current_status = str(current["status"])
            if current_status == "succeeded":
                return self.get_analysis(run_id)
            if current_status in {"cancelled", "interrupted"}:
                raise WorkflowError("画像分析任务已结束，不能再写入提案。", 409)
            db.execute(
                "UPDATE profile_analysis_runs SET status=?,proposal_json=?,error=?,updated_at=? WHERE id=? AND workspace_id=?",
                ("succeeded" if proposal else "failed", _json(proposal), error[:1200], _now_iso(), run_id, self.workspace_id),
            )
        return self.get_analysis(run_id)
