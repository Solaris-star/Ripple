"""Ripple command-line entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from ripple.commands.doctor import cmd_doctor

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("RIPPLE_ROOT", str(PROJECT_ROOT))
    proxy = os.environ.get("RIPPLE_PROXY", "")
    if proxy:
        env.setdefault("http_proxy", proxy)
        env.setdefault("https_proxy", proxy)
    env.setdefault("no_proxy", "localhost,127.0.0.1")
    return env


def cmd_web(args) -> int:
    port = getattr(args, "port", 7860)
    env = _runtime_env()
    env["RIPPLE_PORT"] = str(port)
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "web" / "app.py")],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ripple", description="Ripple — 内容工作台 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="检查本地运行环境")
    p_doctor.set_defaults(func=cmd_doctor)

    p_web = sub.add_parser("web", help="启动 Web UI")
    p_web.add_argument("--port", type=int, default=7860, help="端口（默认 7860）")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())