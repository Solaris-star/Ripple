"""Process-local restricted launcher for a user's native Hermes ACP server.

This module is executed by the Python interpreter that owns the detected Hermes
installation. It does not modify that installation. Before the upstream ACP entry
is imported, it removes global MCP discovery and changes the ACP session toolset
resolver so only a client-provided server named ``ripple`` can enter the model
surface. All normal Hermes tools remain disabled in this process.
"""
from __future__ import annotations

try:
    import hermes_bootstrap
except ModuleNotFoundError:
    hermes_bootstrap = None
else:
    hermes_bootstrap.harden_import_path()

from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import sys

_SHIM_DIR = Path(__file__).resolve().parent
sys.path[:] = [value for value in sys.path if Path(value or ".").resolve() != _SHIM_DIR]


def _ensure_hermes_root() -> None:
    import hermes_cli
    root = Path(hermes_cli.__file__).resolve().parents[1]
    value = str(root)
    if value not in sys.path:
        sys.path.insert(0, value)


def _restricted_toolsets(_toolsets=None, mcp_server_names=None) -> list[str]:
    names = [str(name) for name in (mcp_server_names or [])]
    return ["mcp-ripple"] if "ripple" in names else []


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _install_policy() -> None:
    _ensure_hermes_root()
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    os.environ["NO_COLOR"] = "1"

    import acp_adapter.session as session_module
    session_module._expand_acp_enabled_toolsets = _restricted_toolsets

    # Ripple supplies its own reviewed profile/rules per turn. Prevent native
    # SOUL/memory discovery from duplicating that context or importing unrelated
    # host state into this restricted process.
    agent_init_module = importlib.import_module("agent.agent_init")
    if not getattr(agent_init_module.init_agent, "_ripple_restricted", False):
        original_init_agent = agent_init_module.init_agent

        def restricted_init_agent(agent, *args, **kwargs):
            kwargs["skip_context_files"] = True
            kwargs["skip_memory"] = True
            result = original_init_agent(agent, *args, **kwargs)
            # Some OpenAI-compatible gateways acknowledge stream=True but only
            # forward the first delta before a false stop frame. Ripple defaults
            # to the complete-response path so successful turns are never saved
            # as one-token answers. Operators may opt back into native streaming
            # after validating their endpoint end to end.
            if _env_enabled("RIPPLE_HERMES_DISABLE_STREAMING", default=True):
                agent._disable_streaming = True
            return result

        setattr(restricted_init_agent, "_ripple_restricted", True)
        setattr(agent_init_module, "init_agent", restricted_init_agent)

    # SessionManager imports load_config lazily. Keep provider/model/auth settings
    # but remove configured MCP servers and plugin declarations from this process.
    import hermes_cli.config as config_module
    original_load_config = config_module.load_config

    def restricted_load_config(*args, **kwargs):
        raw = original_load_config(*args, **kwargs)
        value = deepcopy(raw) if isinstance(raw, dict) else {}
        value["mcp_servers"] = {}
        tools = value.get("tools") if isinstance(value.get("tools"), dict) else {}
        value["tools"] = {**tools, "tool_search": {"enabled": "off"}}
        return value

    config_module.load_config = restricted_load_config

    # The upstream entry performs global MCP discovery before accepting sessions.
    # Per-session Ripple MCP registration remains available in HermesACPAgent.
    import tools.mcp_tool as mcp_module
    mcp_module.discover_mcp_tools = lambda *args, **kwargs: []


def _check() -> int:
    _install_policy()
    import acp  # noqa: F401
    import acp_adapter.server as server_module
    from model_tools import get_tool_definitions

    # server.py imports the resolver by value, so verify that it captured ours.
    if server_module._expand_acp_enabled_toolsets(None, ["ripple", "other"]) != ["mcp-ripple"]:
        print(json.dumps({"ok": False, "reason": "resolver_not_applied"}))
        return 2
    tools = get_tool_definitions(enabled_toolsets=[], disabled_toolsets=None, quiet_mode=True)
    names = [str(row.get("function", {}).get("name") or "") for row in tools if isinstance(row, dict)]
    if names:
        print(json.dumps({"ok": False, "reason": "builtin_tools_visible", "tools": names[:20]}))
        return 3
    print(json.dumps({"ok": True, "policy": "ripple-mcp-only", "builtin_tools": []}))
    return 0


def main() -> None:
    if "--ripple-check" in sys.argv[1:]:
        raise SystemExit(_check())
    _install_policy()
    import logging
    from acp_adapter import server as server_module
    original_new_session = server_module.HermesACPAgent.new_session

    async def guarded_new_session(self, *args, **kwargs):
        try:
            return await original_new_session(self, *args, **kwargs)
        except Exception:
            logging.getLogger(__name__).exception("Restricted Hermes ACP session creation failed")
            raise

    server_module.HermesACPAgent.new_session = guarded_new_session
    from acp_adapter.entry import main as hermes_acp_main
    hermes_acp_main([])


if __name__ == "__main__":
    main()
