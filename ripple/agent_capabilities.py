"""Ripple-owned Agent model/session/capability registry.

The registry is authoritative for what the managed Agent may see.  Skills are
instructions, tools are permissions, MCP entries are declarative metadata, and
plugins are bundles of already-registered capabilities.  Nothing here executes
arbitrary commands or widens OpenCode's generic permissions.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import urllib.parse

from .publishing import WorkflowError

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
CAP_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
RUNTIME_ID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
PROFILE_ID_RE = re.compile(r"^[a-z0-9_]+:[A-Za-z0-9._-]{1,80}$")
EFFORTS = ("auto", "low", "medium", "high", "extra_high")
GENERIC_DENY = (
    "bash", "edit", "write", "read", "grep", "glob", "webfetch", "websearch",
    "question", "lsp", "patch", "skill", "todowrite",
)

INTERNAL_TOOLS: dict[str, dict] = {
    "ripple_trends": {"category": "research", "label": "热点雷达", "description": "读取 Ripple 热点数据", "risk": "read", "default_enabled": True},
    "ripple_xhs_read": {"category": "research", "label": "小红书读取", "description": "读取已连接账号的推荐流、搜索、作品与评论", "risk": "read_account", "default_enabled": True},
    "ripple_ideas_list": {"category": "ideas", "label": "读取选题", "description": "读取 Ripple 选题库", "risk": "read", "default_enabled": True},
    "ripple_ideas_add": {"category": "ideas", "label": "新增选题", "description": "向选题库增加候选", "risk": "local_write", "default_enabled": True},
    "ripple_persona": {"category": "profile", "label": "读取画像", "description": "读取当前账号画像", "risk": "read", "default_enabled": True},
    "ripple_operation": {"category": "analysis", "label": "结构化操作", "description": "运行 Ripple 已注册的分析/预览操作", "risk": "analysis", "default_enabled": True},
    "ripple_content_draft": {"category": "content", "label": "内容主稿", "description": "创建或版本化更新内容主稿", "risk": "local_write", "default_enabled": True},
    "ripple_content_read": {"category": "content", "label": "读取内容", "description": "列出或读取 Ripple 内容主稿", "risk": "read", "default_enabled": True},
    "ripple_media_capabilities": {"category": "media", "label": "媒体能力", "description": "读取媒体 Provider/模型能力（不含密钥）", "risk": "read", "default_enabled": True},
    "ripple_generate_image": {"category": "media", "label": "AI 生图", "description": "通过 Ripple 默认或指定媒体模型生成图片", "risk": "external_cost", "default_enabled": True},
    "ripple_generate_video": {"category": "media", "label": "AI 生视频", "description": "通过 Ripple 默认或指定媒体模型生成视频", "risk": "external_cost", "default_enabled": True},
    "ripple_publish_draft": {"category": "publish", "label": "发布草稿", "description": "创建待审核发布任务，不执行真实发布", "risk": "draft_external", "default_enabled": True},
    "ripple_publish_status": {"category": "publish", "label": "发布状态", "description": "读取 Ripple 发布任务状态与回执", "risk": "read", "default_enabled": True},
    "ripple_interaction_draft": {"category": "interaction", "label": "互动草稿", "description": "创建待确认互动草稿，不执行发送或删除", "risk": "draft_external", "default_enabled": True},
    "ripple_interactions_read": {"category": "interaction", "label": "互动读取", "description": "读取已同步评论源、洞察与互动草稿状态", "risk": "read_account", "default_enabled": True},
    "ripple_mcp": {"category": "mcp", "label": "MCP 适配器", "description": "调用 Ripple 已注册并在当前会话固定的 MCP Tool", "risk": "extension", "default_enabled": False, "user_selectable": False},
    "ripple_accounts": {"category": "account", "label": "账号能力", "description": "读取已连接账号公开状态和渠道能力", "risk": "read_account", "default_enabled": False},
    "ripple_analytics": {"category": "analytics", "label": "发布分析", "description": "读取 Ripple 已有发布/表现数据，不抓取未授权页面", "risk": "read", "default_enabled": False},
}

# Product Skills are business capabilities. This managed manifest overlay maps
# those capabilities to the atomic Ripple tools the Agent runtime may need.
# Users select Skills; raw tool ids stay internal to the Broker/Runtime.
SKILL_TOOL_BINDINGS: dict[str, tuple[str, ...]] = {
    "skill-trending-topics": ("ripple_trends", "ripple_ideas_list"),
    "skill-trend-rider": ("ripple_trends", "ripple_ideas_list", "ripple_ideas_add"),
    "skill-news-intelligence": ("ripple_trends",),
    "skill-topic-evaluator": ("ripple_operation", "ripple_ideas_list"),
    "skill-content-matrix": ("ripple_ideas_list", "ripple_ideas_add"),
    "skill-content-calendar": ("ripple_ideas_list",),
    "skill-content-strategy": ("ripple_trends", "ripple_ideas_list", "ripple_persona"),
    "skill-my-account": ("ripple_accounts",),
    "skill-account-diagnosis": ("ripple_accounts", "ripple_analytics"),
    "skill-publish-analytics": ("ripple_analytics", "ripple_publish_status"),
    "skill-social-performance-review": ("ripple_analytics", "ripple_publish_status"),
    "skill-publish-checklist": ("ripple_operation", "ripple_publish_status"),
    "skill-cross-platform-publish": ("ripple_content_read", "ripple_publish_draft", "ripple_publish_status"),
    "skill-xhs-analyzer": ("ripple_xhs_read", "ripple_interactions_read"),
    "skill-xhs-comment-reply": ("ripple_xhs_read", "ripple_interactions_read", "ripple_interaction_draft"),
    "skill-comment-insights": ("ripple_interactions_read", "ripple_operation"),
    "skill-community-ops": ("ripple_interactions_read", "ripple_interaction_draft"),
    "skill-competitor-analysis": ("ripple_xhs_read",),
    "xhs-note-creator": ("ripple_content_read", "ripple_content_draft", "ripple_media_capabilities", "ripple_generate_image"),
    "text-polisher": ("ripple_operation", "ripple_content_read", "ripple_content_draft"),
    "template-library": ("ripple_operation", "ripple_content_read", "ripple_content_draft"),
    "ai-image-gen": ("ripple_media_capabilities", "ripple_generate_image", "ripple_content_draft"),
    "ai-video-gen": ("ripple_media_capabilities", "ripple_generate_video", "ripple_content_draft"),
    "copywriting": ("ripple_persona", "ripple_content_read", "ripple_content_draft"),
    "skill-content-repurposing": ("ripple_content_read", "ripple_content_draft"),
    "skill-cross-platform-diff": ("ripple_content_read",),
    "skill-persona-check": ("ripple_persona",),
    "skill-xhs-publisher": ("ripple_content_read", "ripple_publish_draft", "ripple_publish_status"),
    "skill-bilibili-upload": ("ripple_content_read", "ripple_publish_draft", "ripple_publish_status"),
    "skill-channels-upload": ("ripple_content_read", "ripple_publish_draft", "ripple_publish_status"),
    "skill-wechat-publisher": ("ripple_content_read", "ripple_publish_draft", "ripple_publish_status"),
    "skill-zhihu-publisher": ("ripple_content_read", "ripple_publish_draft", "ripple_publish_status"),
}

BUILTIN_PLUGINS = {
    "xiaohongshu_ops": {
        "id": "xiaohongshu_ops", "name": "小红书运营", "description": "读取、分析、创作与互动草稿的一组小红书能力。",
        "skills": ["skill-xhs-analyzer", "xhs-note-creator", "skill-xhs-comment-reply"],
        "tools": ["ripple_xhs_read", "ripple_interaction_draft", "ripple_interactions_read", "ripple_operation"],
        "mcp_tools": [],
    },
}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _bounded_list(value, pattern=CAP_ID_RE, limit=80) -> list[str]:
    if not isinstance(value, list): return []
    out = []
    for item in value[:limit]:
        text = str(item or "").strip()
        if text and pattern.fullmatch(text) and text not in out: out.append(text)
    return out


class AgentCapabilityRegistry:
    def __init__(self, private_dir: Path):
        self.root = private_dir.resolve() / "agent"
        self.models_path = self.root / "models.json"
        self.sessions_path = self.root / "sessions.json"
        self.extensions_path = self.root / "extensions.json"
        self._guard = threading.RLock()

    def _read(self, path: Path, default: dict) -> dict:
        try:
            if not path.is_file() or path.stat().st_size > 1024 * 1024: return deepcopy(default)
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else deepcopy(default)
        except (OSError, ValueError, TypeError):
            return deepcopy(default)

    def model_state(self, legacy_model: str) -> dict:
        state = self._read(self.models_path, {"schema": 1, "default_model": "", "models": []})
        rows = []
        for row in state.get("models", []) if isinstance(state.get("models"), list) else []:
            if not isinstance(row, dict): continue
            mid = str(row.get("id") or "").strip()
            if not MODEL_ID_RE.fullmatch(mid): continue
            levels = _bounded_list(row.get("effort_levels"), re.compile(r"^(?:auto|low|medium|high|extra_high)$"), 5)
            if not levels: levels = ["auto"]
            if "auto" not in levels: levels.insert(0, "auto")
            rows.append({"id": mid, "name": str(row.get("name") or mid)[:200], "effort_levels": levels,
                         "supports_reasoning": len(levels) > 1, "source": str(row.get("source") or "manual")[:24]})
        if legacy_model and MODEL_ID_RE.fullmatch(legacy_model) and all(r["id"] != legacy_model for r in rows):
            rows.insert(0, {"id": legacy_model, "name": legacy_model, "effort_levels": ["auto"], "supports_reasoning": False, "source": "legacy"})
        default_model = str(state.get("default_model") or "").strip()
        if not default_model or all(r["id"] != default_model for r in rows): default_model = legacy_model if any(r["id"] == legacy_model for r in rows) else (rows[0]["id"] if rows else "")
        return {"schema": 1, "default_model": default_model, "models": rows}

    def save_models(self, models: list[dict], default_model: str, legacy_model: str = "") -> dict:
        rows = []
        for row in models[:200]:
            if not isinstance(row, dict): continue
            mid = str(row.get("id") or "").strip()
            if not MODEL_ID_RE.fullmatch(mid): raise WorkflowError(f"模型 ID 无效：{mid or '空'}", 422)
            levels = _bounded_list(row.get("effort_levels"), re.compile(r"^(?:auto|low|medium|high|extra_high)$"), 5) or ["auto"]
            if "auto" not in levels: levels.insert(0, "auto")
            rows.append({"id": mid, "name": str(row.get("name") or mid)[:200], "effort_levels": levels, "source": str(row.get("source") or "manual")[:24]})
        ids = {r["id"] for r in rows}
        if default_model not in ids: raise WorkflowError("默认模型必须来自已启用模型列表。", 422)
        _atomic_json(self.models_path, {"schema": 1, "default_model": default_model, "models": rows})
        return self.model_state(legacy_model)

    def extensions_state(self) -> dict:
        state = self._read(self.extensions_path, {"schema": 1, "mcp_servers": []})
        servers = []
        for row in state.get("mcp_servers", []) if isinstance(state.get("mcp_servers"), list) else []:
            if not isinstance(row, dict): continue
            sid = str(row.get("id") or "").strip()
            if not CAP_ID_RE.fullmatch(sid): continue
            transport = str(row.get("transport") or "http")
            if transport not in {"http", "sse", "streamable_http", "mock"}: continue
            tools = []
            for tool in row.get("tools", []) if isinstance(row.get("tools"), list) else []:
                if not isinstance(tool, dict): continue
                tid = str(tool.get("id") or "").strip()
                if CAP_ID_RE.fullmatch(tid):
                    tools.append({"id": tid, "name": str(tool.get("name") or tid)[:100], "description": str(tool.get("description") or "")[:300], "risk": str(tool.get("risk") or "read")[:32]})
            servers.append({"id": sid, "name": str(row.get("name") or sid)[:100], "transport": transport,
                            "endpoint": str(row.get("endpoint") or "")[:1200], "enabled": bool(row.get("enabled")),
                            "status": "ready" if transport == "mock" and row.get("enabled") else "not_connected", "tools": tools})
        return {"schema": 1, "mcp_servers": servers}

    def save_extensions(self, servers: list[dict]) -> dict:
        clean = []
        for row in servers[:20]:
            if not isinstance(row, dict): continue
            sid = str(row.get("id") or "").strip()
            if not CAP_ID_RE.fullmatch(sid): raise WorkflowError("MCP Server ID 无效。", 422)
            transport = str(row.get("transport") or "http")
            if transport not in {"http", "sse", "streamable_http", "mock"}: raise WorkflowError("不支持的 MCP transport。", 422)
            endpoint = str(row.get("endpoint") or "").strip()
            if transport != "mock" and endpoint:
                try:
                    parsed = urllib.parse.urlsplit(endpoint)
                except ValueError:
                    raise WorkflowError("MCP endpoint 无效。", 422) from None
                host = (parsed.hostname or "").lower().rstrip(".")
                local = host in {"localhost", "127.0.0.1", "::1"}
                if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise WorkflowError("MCP 远程 endpoint 必须使用 HTTPS；本机可使用 loopback HTTP，且 URL 不能包含凭据、query 或 fragment。", 422)
            tools = []
            for tool in row.get("tools", []) if isinstance(row.get("tools"), list) else []:
                if not isinstance(tool, dict): continue
                tid = str(tool.get("id") or "").strip()
                if not CAP_ID_RE.fullmatch(tid): raise WorkflowError("MCP Tool ID 无效。", 422)
                tools.append({"id": tid, "name": str(tool.get("name") or tid)[:100], "description": str(tool.get("description") or "")[:300], "risk": str(tool.get("risk") or "read")[:32]})
            clean.append({"id": sid, "name": str(row.get("name") or sid)[:100], "transport": transport, "endpoint": endpoint[:1200], "enabled": bool(row.get("enabled")), "tools": tools})
        _atomic_json(self.extensions_path, {"schema": 1, "mcp_servers": clean})
        return self.extensions_state()

    @staticmethod
    def _mcp_name(server_id: str, tool_id: str) -> str:
        safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server_id)[:48]
        safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", tool_id)[:64]
        return f"ripple_mcp__{safe_server}__{safe_tool}"

    def catalog(self, skills: list[dict], readiness: dict[str, bool] | None = None) -> dict:
        readiness = readiness or {}
        tools = []
        for tid, meta in INTERNAL_TOOLS.items():
            tools.append({"id": tid, "source": "ripple", "ready": bool(readiness.get(tid, True)), "user_selectable": bool(meta.get("user_selectable", True)), **meta})
        mcp_tools = []
        for server in self.extensions_state()["mcp_servers"]:
            for tool in server["tools"]:
                mcp_tools.append({"id": self._mcp_name(server["id"], tool["id"]), "server_id": server["id"], "tool_id": tool["id"],
                                  "source": "mcp", "category": "mcp", "label": tool["name"], "description": tool["description"],
                                  "risk": tool["risk"], "default_enabled": False, "ready": server["status"] == "ready"})
        skill_rows = [{"id": str(s.get("name") or ""), "name": str(s.get("name") or ""), "description": str(s.get("description") or ""),
                       "layer": str(s.get("layer") or ""), "ready": bool(s.get("apiConfigured", True)),
                       "bound_tools": list(SKILL_TOOL_BINDINGS.get(str(s.get("name") or ""), ())) } for s in skills if isinstance(s, dict) and s.get("name")]
        return {"tools": tools, "mcp_tools": mcp_tools, "skills": skill_rows, "plugins": list(BUILTIN_PLUGINS.values()),
                "generic_denied": list(GENERIC_DENY)}

    def _session_state(self) -> dict:
        value = self._read(self.sessions_path, {"schema": 1, "sessions": {}})
        return value if isinstance(value.get("sessions"), dict) else {"schema": 1, "sessions": {}}

    def get_session(self, session_id: str, model_state: dict, skills: list[dict], *,
                    runtime_ids: list[str] | tuple[str, ...] | None = None, default_runtime: str = "opencode",
                    profile_runtimes: dict[str, str] | None = None, default_profile: str = "",
                    runtime_model_states: dict[str, dict] | None = None) -> dict:
        if not SESSION_ID_RE.fullmatch(session_id): raise WorkflowError("会话标识无效。", 422)
        state = self._session_state(); raw = state["sessions"].get(session_id, {})
        if not isinstance(raw, dict): raw = {}
        runtimes = [str(value) for value in (runtime_ids or ["opencode"]) if RUNTIME_ID_RE.fullmatch(str(value))]
        if not runtimes: runtimes = ["opencode"]
        runtime_id = str(raw.get("runtime_id") or default_runtime or runtimes[0])
        if runtime_id not in runtimes: runtime_id = default_runtime if default_runtime in runtimes else runtimes[0]
        profile_map = {str(k): str(v) for k, v in (profile_runtimes or {}).items() if PROFILE_ID_RE.fullmatch(str(k)) and str(v) in runtimes}
        profile_id = str(raw.get("profile_id") or default_profile or "")
        if profile_id not in profile_map or profile_map.get(profile_id) != runtime_id:
            if default_profile in profile_map and profile_map.get(default_profile) == runtime_id:
                profile_id = default_profile
            else:
                profile_id = next((pid for pid, rid in profile_map.items() if rid == runtime_id), "")
        active_model_state = (runtime_model_states or {}).get(runtime_id) or model_state
        model_ids = {m["id"]: m for m in active_model_state.get("models", [])}
        model = str(raw.get("model") or active_model_state.get("default_model") or "")
        if model not in model_ids: model = active_model_state.get("default_model") or ""
        effort = str(raw.get("effort") or "auto")
        if effort not in EFFORTS: effort = "auto"
        skill_ids = {str(s.get("name")) for s in skills if s.get("name")}
        pinned_skills = [s for s in _bounded_list(raw.get("pinned_skills")) if s in skill_ids]
        enabled_tools = [t for t in _bounded_list(raw.get("enabled_tools")) if t in INTERNAL_TOOLS]
        if "enabled_tools" not in raw:
            enabled_tools = [tid for tid, m in INTERNAL_TOOLS.items() if m.get("default_enabled")]
        mcp_catalog = {x["id"] for x in self.catalog(skills)["mcp_tools"] if x["ready"]}
        enabled_mcp = [t for t in _bounded_list(raw.get("enabled_mcp_tools")) if t in mcp_catalog]
        plugins = [p for p in _bounded_list(raw.get("enabled_plugins")) if p in BUILTIN_PLUGINS]
        return {"session_id": session_id, "runtime_id": runtime_id, "profile_id": profile_id,
                "runtime_locked": bool(raw.get("runtime_locked")),
                "model": model, "effort": effort, "effort_mode": "instruction_fallback" if effort != "auto" else "auto",
                "pinned_skills": pinned_skills, "enabled_tools": enabled_tools, "enabled_mcp_tools": enabled_mcp, "enabled_plugins": plugins}

    def update_session(self, session_id: str, payload: dict, model_state: dict, skills: list[dict], *,
                       runtime_ids: list[str] | tuple[str, ...] | None = None, default_runtime: str = "opencode",
                       profile_runtimes: dict[str, str] | None = None, default_profile: str = "",
                       runtime_model_states: dict[str, dict] | None = None) -> dict:
        with self._guard:
            current = self.get_session(session_id, model_state, skills, runtime_ids=runtime_ids, default_runtime=default_runtime,
                                       profile_runtimes=profile_runtimes, default_profile=default_profile,
                                       runtime_model_states=runtime_model_states)
            runtimes = [str(value) for value in (runtime_ids or ["opencode"]) if RUNTIME_ID_RE.fullmatch(str(value))] or ["opencode"]
            profile_map = {str(k): str(v) for k, v in (profile_runtimes or {}).items() if PROFILE_ID_RE.fullmatch(str(k)) and str(v) in runtimes}
            locked = bool(current.get("runtime_locked"))
            if "runtime_id" in payload:
                runtime_id = str(payload.get("runtime_id") or "")
                if runtime_id not in runtimes: raise WorkflowError("所选 Agent Runtime 未注册。", 422)
                if locked and runtime_id != current["runtime_id"]: raise WorkflowError("当前会话已开始，Agent Runtime 已锁定；请新建会话后切换。", 409)
                current["runtime_id"] = runtime_id
                if profile_map.get(current.get("profile_id", "")) != runtime_id:
                    current["profile_id"] = next((pid for pid, rid in profile_map.items() if rid == runtime_id), "")
            if "profile_id" in payload:
                profile_id = str(payload.get("profile_id") or "")
                if profile_id not in profile_map: raise WorkflowError("所选 Agent Profile 不存在或当前不可用。", 422)
                if locked and profile_id != current.get("profile_id", ""): raise WorkflowError("当前会话已开始，Agent Profile 已锁定；请新建会话后切换。", 409)
                if profile_map[profile_id] != current["runtime_id"]: raise WorkflowError("Agent Profile 与当前 Runtime 不匹配。", 422)
                current["profile_id"] = profile_id
            active_model_state = (runtime_model_states or {}).get(current["runtime_id"]) or model_state
            model_ids = {m["id"]: m for m in active_model_state.get("models", [])}
            if current.get("model") not in model_ids:
                current["model"] = str(active_model_state.get("default_model") or "")
            if "model" in payload:
                model = str(payload.get("model") or "")
                if model not in model_ids: raise WorkflowError("所选模型未在当前 Agent 的模型列表中启用。", 422)
                current["model"] = model
            if "effort" in payload:
                effort = str(payload.get("effort") or "auto")
                if effort not in EFFORTS: raise WorkflowError("Effort 无效。", 422)
                current["effort"] = effort
            skill_ids = {str(s.get("name")) for s in skills if s.get("name")}
            if "pinned_skills" in payload:
                vals = _bounded_list(payload.get("pinned_skills"))
                bad = [v for v in vals if v not in skill_ids]
                if bad: raise WorkflowError(f"未注册 Skill：{bad[0]}", 422)
                current["pinned_skills"] = vals
            if "enabled_tools" in payload:
                vals = _bounded_list(payload.get("enabled_tools")); bad = [v for v in vals if v not in INTERNAL_TOOLS or not INTERNAL_TOOLS[v].get("user_selectable", True)]
                if bad: raise WorkflowError(f"未注册或不可直接固定的 Ripple Tool：{bad[0]}", 422)
                current["enabled_tools"] = vals
            if "enabled_plugins" in payload:
                vals = _bounded_list(payload.get("enabled_plugins")); bad = [v for v in vals if v not in BUILTIN_PLUGINS]
                if bad: raise WorkflowError(f"未注册 Plugin：{bad[0]}", 422)
                current["enabled_plugins"] = vals
            if "enabled_mcp_tools" in payload:
                ready = {x["id"] for x in self.catalog(skills)["mcp_tools"] if x["ready"]}
                vals = _bounded_list(payload.get("enabled_mcp_tools")); bad = [v for v in vals if v not in ready]
                if bad: raise WorkflowError(f"MCP Tool 当前不可用：{bad[0]}", 422)
                current["enabled_mcp_tools"] = vals
            current["effort_mode"] = "instruction_fallback" if current["effort"] != "auto" else "auto"
            state = self._session_state(); sessions = state["sessions"]
            sessions[session_id] = {k: deepcopy(current[k]) for k in ("runtime_id", "profile_id", "runtime_locked", "model", "effort", "pinned_skills", "enabled_tools", "enabled_mcp_tools", "enabled_plugins")}
            if len(sessions) > 200:
                for key in list(sessions)[:-200]: sessions.pop(key, None)
            _atomic_json(self.sessions_path, state)
            return current

    def lock_runtime_profile(self, session_id: str, runtime_id: str, profile_id: str = "") -> None:
        if not SESSION_ID_RE.fullmatch(session_id): raise WorkflowError("会话标识无效。", 422)
        if not RUNTIME_ID_RE.fullmatch(runtime_id): raise WorkflowError("Agent Runtime 标识无效。", 422)
        if profile_id and not PROFILE_ID_RE.fullmatch(profile_id): raise WorkflowError("Agent Profile ID 无效。", 422)
        with self._guard:
            state = self._session_state(); sessions = state["sessions"]
            raw = sessions.get(session_id, {})
            if not isinstance(raw, dict): raw = {}
            previous_runtime = str(raw.get("runtime_id") or "")
            previous_profile = str(raw.get("profile_id") or "")
            if raw.get("runtime_locked") and previous_runtime and previous_runtime != runtime_id:
                raise WorkflowError("当前会话的 Agent Runtime 已锁定。", 409)
            if raw.get("runtime_locked") and previous_profile and profile_id and previous_profile != profile_id:
                raise WorkflowError("当前会话的 Agent Profile 已锁定。", 409)
            raw["runtime_id"] = runtime_id
            raw["profile_id"] = profile_id
            raw["runtime_locked"] = True
            sessions[session_id] = raw
            if len(sessions) > 200:
                for key in list(sessions)[:-200]: sessions.pop(key, None)
            _atomic_json(self.sessions_path, state)

    def execute_mcp(self, capability_id: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 16000:
            raise WorkflowError("MCP 参数超过安全上限。", 422)
        target = next((row for row in self.catalog([])["mcp_tools"] if row["id"] == capability_id), None)
        if not target or not target.get("ready"):
            raise WorkflowError("MCP Tool 当前未连接或不存在。", 409)
        server = next((row for row in self.extensions_state()["mcp_servers"] if row["id"] == target["server_id"]), None)
        if not server or server.get("transport") != "mock":
            raise WorkflowError("当前版本只运行 Ripple 内置的确定性 Mock MCP 适配器；远程 MCP 尚未连接。", 409)
        # Deterministic no-I/O adapter used to validate the broker/injection path without arbitrary execution.
        return {"kind": "mcp_result", "capability_id": capability_id, "server": server["id"], "tool": target["tool_id"],
                "result": {"echo": deepcopy(arguments), "adapter": "ripple-mock"}}

    def resolve_turn(self, session_id: str, model_state: dict, skills: list[dict], turn_skills=None, turn_tools=None, readiness: dict[str, bool] | None = None, *,
                     runtime_ids: list[str] | tuple[str, ...] | None = None, default_runtime: str = "opencode",
                     profile_runtimes: dict[str, str] | None = None, default_profile: str = "",
                     runtime_model_states: dict[str, dict] | None = None) -> dict:
        config = self.get_session(session_id, model_state, skills, runtime_ids=runtime_ids, default_runtime=default_runtime,
                                  profile_runtimes=profile_runtimes, default_profile=default_profile,
                                  runtime_model_states=runtime_model_states)
        catalog = self.catalog(skills, readiness)
        skill_ids = {s["id"] for s in catalog["skills"] if s["ready"]}
        skills_out = [skill for skill in config["pinned_skills"] if skill in skill_ids]
        for skill in _bounded_list(turn_skills, limit=12):
            if skill not in skill_ids: raise WorkflowError(f"Skill 当前不可用：{skill}", 422)
            if skill not in skills_out: skills_out.append(skill)
        ready_tool_ids = {row["id"] for row in catalog["tools"] if row["ready"]}
        selectable_tool_ids = {row["id"] for row in catalog["tools"] if row["ready"] and row.get("user_selectable", True)}
        ready_mcp_ids = {row["id"] for row in catalog["mcp_tools"] if row["ready"]}
        tools_out = [tool for tool in config["enabled_tools"] if tool in ready_tool_ids]
        mcp_out = [tool for tool in config["enabled_mcp_tools"] if tool in ready_mcp_ids]
        for skill in skills_out:
            for tool in SKILL_TOOL_BINDINGS.get(skill, ()):
                if tool in ready_tool_ids and tool not in tools_out:
                    tools_out.append(tool)
        for plugin_id in config["enabled_plugins"]:
            plugin = BUILTIN_PLUGINS[plugin_id]
            for skill in plugin["skills"]:
                if skill in skill_ids and skill not in skills_out: skills_out.append(skill)
            for skill in plugin["skills"]:
                for tool in SKILL_TOOL_BINDINGS.get(skill, ()):
                    if tool in ready_tool_ids and tool not in tools_out:
                        tools_out.append(tool)
            for tool in plugin["tools"]:
                if tool in ready_tool_ids and tool not in tools_out: tools_out.append(tool)
            for tool in plugin.get("mcp_tools", []):
                if tool in ready_mcp_ids and tool not in mcp_out: mcp_out.append(tool)
        for tool in _bounded_list(turn_tools, limit=20):
            if tool not in selectable_tool_ids: raise WorkflowError(f"Tool 未注册、未就绪或不能由 AI 协作注入：{tool}", 422)
            if tool not in tools_out: tools_out.append(tool)
        if mcp_out and "ripple_mcp" not in tools_out:
            tools_out.append("ripple_mcp")
        return {**config, "skills": skills_out[:12], "tools": tools_out[:80], "mcp_tools": mcp_out[:80],
                "generic_denied": list(GENERIC_DENY)}
