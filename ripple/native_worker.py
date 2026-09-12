"""Isolated subprocess bridge to upstream publishers; JSON on stdin/stdout.

Only the parent service constructs these payloads. Profiles, QR images and
transient status never use a personal browser directory. Raw publisher logs are
held in a bounded memory buffer and never returned or saved by this bridge.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

from filelock import FileLock, Timeout
from .catalog import NATIVE, biliup_binary

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/shared/scripts"


class SubmissionAlreadyAttempted(BaseException):
    """Abort legacy broad Exception retry loops without a second final submit."""


class Tail(io.TextIOBase):
    def __init__(self):
        self.text = ""
    def write(self, text):
        self.text = (self.text + str(text))[-32768:]
        return len(str(text))
    def flush(self):
        pass


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def structured_identity(raw: str) -> dict:
    for line in reversed(raw.splitlines()):
        try:
            item = json.loads(line)
            if isinstance(item, dict) and "loggedIn" in item:
                return {"logged_in": item.get("loggedIn") is True and not item.get("error"),
                        "name": str(item.get("name") or "")[:80],
                        "remote_id": str(item.get("mid") or item.get("uid") or "")[:100]}
        except (ValueError, TypeError):
            pass
    return {"logged_in": False, "name": "", "remote_id": ""}


def execute(payload: dict) -> dict:
    platform, operation = payload["platform"], payload["operation"]
    if platform not in NATIVE or operation not in {"login", "probe", "publish", "xhs_read", "xhs_interact"}:
        raise ValueError("unsupported operation")
    if operation in {"xhs_read", "xhs_interact"} and platform != "xiaohongshu":
        raise ValueError("xiaohongshu operation requires xiaohongshu account")
    if not re.fullmatch(r'[a-f0-9]{32}', payload.get('operation_id', '')):
        raise ValueError('invalid operation id')
    directory = Path(payload["private_dir"]).resolve()
    run_dir = directory / "operations" / payload["operation_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SCRIPTS))
    module = importlib.import_module(NATIVE[platform]["module"])
    import login_state
    # Redirect every legacy diagnostic path, including paths calculated from __file__.
    module.__file__ = str(run_dir / "source/skills/shared/scripts" / (module.__name__ + ".py"))
    module.PROJECT_ROOT = run_dir / "source"
    module.DEFAULT_QR_OUT = run_dir / "qr.png"
    module.DEFAULT_COOKIE = directory / "cookies.json"
    if hasattr(module, "_dump_sms_dom"):
        module._dump_sms_dom = lambda *a, **k: ""
    if hasattr(module, "_dump_publish_fail"):
        module._dump_publish_fail = lambda *a, **k: None
    # No legacy calendar write; the task ledger is authoritative.
    import calendar_ops
    calendar_ops.record_publish = lambda *a, **k: None
    # Some upstream debug steps resolve paths from cwd rather than __file__.
    # Keep those screenshots/logs in this operation's private directory too.
    os.chdir(run_dir)
    status_file = run_dir / "status.json"
    allowed_states = {"starting", "qr_ready", "scanned", "success", "error", "expired", "sms_required", "verifying"}
    def safe_status(_path, state, message="", **kwargs):
        state = state if state in allowed_states else "starting"
        atomic_json(status_file, {"state": state, "message": {
            "starting": "正在打开平台", "qr_ready": "请使用平台 App 扫码并确认", "scanned": "已扫码，请完成手机确认",
            "success": "平台操作已返回", "error": "平台操作未完成，请检查登录或网络", "expired": "登录已超时",
            "sms_required": "平台要求短信验证，请输入本人手机收到的验证码", "verifying": "正在等待平台验证",
        }[state]})
    login_state.write_status = safe_status
    opts = SimpleNamespace(profile_base=str(directory / "browser"), proxy=None, no_proxy=True,
        headed=bool(payload.get("headed", True)), keep_open=False, platform=platform,
        profile_label=str(payload.get("profile_label") or "")[:80],
        qr_out=str(run_dir / "qr.png"), status_file=str(status_file), timeout=240,
        sms_code_file=str(run_dir / "sms.code"), cookie=str(directory / "cookies.json"),
        media=None, title=None, desc=None, content=None, tags="", cover=None, allow_unsafe=False)
    opts.image_paths = []
    opts.submission_file = str(run_dir / "submission.json")
    opts.task_id = payload.get("task_id")
    opts.version_id = payload.get("version_id")
    opts.operation_id = payload.get("operation_id")
    # Bind library launch to an explicitly separate profile and installed channel.
    from playwright.sync_api import BrowserType
    original_launch = BrowserType.launch_persistent_context
    def isolated_launch(self, user_data_dir, **kwargs):
        path = Path(user_data_dir).resolve()
        if directory not in path.parents:
            raise ValueError("profile isolation")
        kwargs["channel"] = payload.get("browser_channel") or "chromium"
        if operation == 'login':
            kwargs['headless'] = not bool(payload.get('headed', True))
        kwargs["args"] = ["--no-first-run", "--no-default-browser-check"]
        kwargs["chromium_sandbox"] = True
        return original_launch(self, user_data_dir, **kwargs)
    BrowserType.launch_persistent_context = isolated_launch

    def invoke(fn, *args):
        log = Tail()
        with redirect_stdout(log), redirect_stderr(log):
            rc = fn(*args)
        return rc, log.text

    if operation in {"xhs_read", "xhs_interact"}:
        if operation == "xhs_interact" and payload.get("confirmed") is not True:
            return {"state": "not_submitted", "not_submitted": True, "message": "缺少真实互动授权。", "data": {}}
        from . import xhs_browser
        if operation == "xhs_interact":
            # Persist the ambiguity boundary before browser interaction. A crash after
            # this marker must be reconciled instead of blindly repeating the action.
            atomic_json(run_dir / "xhs-interaction-intent.json", {
                "operation_id": payload["operation_id"], "action": str(payload.get("xhs_action") or "")[:32]
            })
        try:
            data = xhs_browser.run(
                str(payload.get("xhs_action") or ""), directory,
                payload.get("xhs_params") if isinstance(payload.get("xhs_params"), dict) else {},
            )
        except xhs_browser.XhsBrowserError as exc:
            code = str(exc)
            if code == "risk_control":
                return {"state": "verification_required", "not_submitted": operation == "xhs_interact",
                        "message": "小红书要求人工验证或限制了当前访问，请在平台处理后再试。", "data": {}}
            if code == "login_required":
                return {"state": "verification_required", "not_submitted": operation == "xhs_interact",
                        "message": "小红书登录态已失效，请先重新连接账号。", "data": {}}
            return {"state": "not_submitted" if operation == "xhs_interact" else "failed_terminal",
                    "not_submitted": operation == "xhs_interact", "message": "小红书操作参数或页面状态不符合要求。", "data": {}}
        if operation == "xhs_read":
            return {"state": "success", "message": "小红书只读操作完成。", "data": data}
        states = []
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            states = [str(item.get("status") or "") for item in data["results"] if isinstance(item, dict)]
        elif isinstance(data, dict) and data.get("status"):
            states = [str(data.get("status"))]
        if states and all(state == "verified" for state in states):
            state = "verified"
        elif states and all(state == "not_submitted" for state in states):
            state = "not_submitted"
        elif "unknown_result" in states:
            state = "unknown_result"
        elif states:
            state = "partial"
        else:
            state = "unknown_result"
        return {"state": state, "not_submitted": state == "not_submitted",
                "message": "小红书互动已核对。" if state == "verified" else "小红书互动结果需要按明细核对。", "data": data}

    if operation == "login":
        fn = module.cmd_login_qr if platform in {"kuaishou", "weixin-channels", "zhihu"} else module.cmd_login
        rc, _ = invoke(fn, opts)
        if rc != 0:
            return {"state": "disconnected", "message": "登录未完成或二维码已过期。"}
        _, raw = invoke(module.cmd_whoami, opts)
        ident = structured_identity(raw)
        return {"state": "connected" if ident["logged_in"] else "disconnected", "identity": ident,
                "message": "登录状态已核验。" if ident["logged_in"] else "扫码已结束，但账号核验未通过。"}
    if operation == "probe":
        _, raw = invoke(module.cmd_whoami, opts)
        ident = structured_identity(raw)
        if platform == "x" and "x_browser_probe_failed" in raw:
            return {"state": "verification_required", "not_submitted": True, "identity": ident,
                    "message": "无法打开 X 独立 Profile。请先关闭人工登录浏览器窗口，再检查登录状态。"}
        return {"state": "connected" if ident["logged_in"] else "expired", "identity": ident,
                "message": "账号状态已核验。" if ident["logged_in"] else "登录已失效或暂时无法核验。"}

    if payload.get("confirmed") is not True:
        return {"state": "failed_terminal", "not_submitted": True, "message": "缺少真实发布授权。"}
    _, raw = invoke(module.cmd_whoami, opts)
    identity = structured_identity(raw)
    expected = payload.get("identity") or {}
    if not identity["logged_in"]:
        return {"state": "verification_required", "not_submitted": True, "message": "账号未登录，未执行上传。"}
    for key in ("name", "remote_id"):
        if expected.get(key) and identity.get(key) != expected[key]:
            return {"state": "verification_required", "not_submitted": True, "message": "账号身份已变化，未执行上传。"}
    c = payload["content"]
    paths = payload.get("media_paths", [])
    opts.title, opts.content, opts.desc, opts.tags = c["title"], c["body"], c["body"], c.get("tags", "")
    opts.images = ",".join(paths)
    opts.video = paths[0] if paths and Path(paths[0]).suffix.lower() in {".mp4", ".mov", ".webm"} else None
    opts.media = paths[0] if paths else None
    opts.image_paths = list(paths)
    setattr(opts, "exec", True)
    # Persist the submission boundary before any upstream upload/click. A crash
    # after this point is ambiguous even when the subprocess exits nonzero.
    if platform != "x":
        atomic_json(run_dir / "submission.json", {"task_id": payload["task_id"], "version_id": payload["version_id"], "operation_id": payload["operation_id"]})
    if platform == "bilibili":
        binary = biliup_binary()
        if not binary:
            return {"state": "failed_terminal", "not_submitted": True, "message": "biliup 尚未安装。"}
        argv = [str(binary), "-u", opts.cookie, "upload", paths[0], "--title", c["title"], "--tid", "36", "--copyright", "1", "--tag", c.get("tags") or "日常", "--desc", c["body"]]
        result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=840)
        return {"state": "accepted" if result.returncode == 0 else "unknown_result", "message": "投稿程序已返回；请在平台核对作品。", "evidence": "biliup_exit"}
    # Upstream scripts occasionally retry the final click internally. Intercept
    # submit entry points to allow exactly one submission per approved attempt.
    clicked = False
    def once(fn):
        def guarded(*args, **kwargs):
            nonlocal clicked
            if clicked:
                raise SubmissionAlreadyAttempted("duplicate final submit blocked")
            clicked = True
            return fn(*args, **kwargs)
        return guarded
    if platform == "douyin":
        module._click_publish = once(module._click_publish)
        # Do not infer publication from matching a title in old management items.
        module._verify_published = lambda *a, **k: False
    if platform == "weixin-channels":
        from playwright.sync_api import Keyboard
        original_press = Keyboard.press
        def press(self, key, **kwargs):
            nonlocal clicked
            if key == "Enter":
                if clicked:
                    raise SubmissionAlreadyAttempted("duplicate final submit blocked")
                clicked = True
            return original_press(self, key, **kwargs)
        Keyboard.press = press
    fn = module.cmd_publish_video if opts.video and platform in {"xiaohongshu", "douyin"} else module.cmd_publish
    try:
        rc, _ = invoke(fn, opts)
    except BaseException:
        if platform == "x" and not (run_dir / "submission.json").exists():
            return {"state": "verification_required", "not_submitted": True,
                    "message": "X 浏览器发布在最终提交前中止；未点击发布，可检查页面状态后重新审核。"}
        return {"state": "unknown_result", "not_submitted": False,
                "message": "上传或提交结果未确认，请先在平台核对，禁止直接重发。"}
    return {"state": "accepted" if rc == 0 else "unknown_result", "not_submitted": False,
            "message": "发布器已返回页面结果，待核对作品链接。", "evidence": "upstream_ui_signal"}


def main():
    payload = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024).decode("utf-8"))
    directory = Path(payload["private_dir"]).resolve()
    run_dir = directory / "operations" / payload["operation_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    ambiguous = payload.get("operation") in {"publish", "xhs_interact"}
    outcome = {"state": "unknown_result" if ambiguous else "error", "message": "平台操作未完成，请检查浏览器环境、网络或账号状态。"}
    try:
        with FileLock(str(directory / "browser-operation.lock"), timeout=0):
            with redirect_stdout(Tail()), redirect_stderr(Tail()):
                outcome = execute(payload)
    except Timeout:
        outcome = {"state": "verification_required", "not_submitted": True, "message": "该账号的浏览器正在使用，请稍后重试。"}
    except BaseException:
        boundary = (run_dir / "submission.json").exists() or (run_dir / "xhs-interaction-intent.json").exists()
        if not boundary:
            outcome["not_submitted"] = True
    outcome.update(operation_id=payload["operation_id"], task_id=payload.get("task_id"), version_id=payload.get("version_id"))
    atomic_json(run_dir / "result.json", outcome)
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    main()
