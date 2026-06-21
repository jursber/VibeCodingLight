"""
Hook 脚本 — 被 IDE hook 调用，写入状态文件或启动 daemon。

核心创新：子代理 hooks 智能过滤。
- Claude Code 子代理的 hook payload 中包含 agent_id 字段（主代理没有）
- 如果主代理已 Stop（session 状态为 idle），忽略子代理的残留事件
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
from typing import Any

from .config import PRIORITY, STATES_ROOT, state_dir_for

STDIN_TIMEOUT = 0.2  # 秒

# 需要权限弹窗的工具名
ALERT_TOOLS = {"AskUserQuestion", "PermissionRequest"}


def _read_stdin_json() -> dict[str, Any]:
    """从 stdin 读取 JSON，带超时防止 hook 进程挂住。"""
    result: dict[str, Any] = {}
    done = threading.Event()

    def _reader():
        nonlocal result
        try:
            if sys.stdin is None:
                return
            data = sys.stdin.read()
            if data and data.strip():
                result = json.loads(data.strip())
        except (json.JSONDecodeError, OSError):
            pass
        finally:
            done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    done.wait(timeout=STDIN_TIMEOUT)
    return result


def _write_state(agent: str, session_id: str, state: str,
                 is_subagent: bool = False) -> None:
    """原子写入状态文件。"""
    d = state_dir_for(agent)
    path = os.path.join(d, session_id)
    data = {"state": state, "ts": time.time()}
    if is_subagent:
        data["is_subagent"] = True
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _delete_state(agent: str, session_id: str) -> None:
    """删除状态文件（off 状态）。"""
    d = state_dir_for(agent)
    path = os.path.join(d, session_id)
    try:
        os.remove(path)
    except OSError:
        pass


def _read_current_state(agent: str, session_id: str) -> dict | None:
    """读取当前 session 的状态文件。"""
    d = state_dir_for(agent)
    path = os.path.join(d, session_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _resolve_state(event: str, state_hint: str | None,
                   stdin_data: dict[str, Any]) -> str | None:
    """根据事件和 stdin 数据确定最终状态。

    - state_hint="auto": 根据 tool_name 判断 alert/working
    - state_hint="thinking" 且 event="UserPromptSubmit": 检查是否有 prompt 文本
    - 其他: 直接使用 state_hint
    """
    if state_hint is None:
        return None

    if state_hint == "auto":
        tool_name = stdin_data.get("tool_name", "")
        if tool_name in ALERT_TOOLS:
            return "alert"
        return "model"

    if state_hint == "thinking" and event == "UserPromptSubmit":
        prompt = (stdin_data.get("prompt") or
                  stdin_data.get("user_prompt") or
                  stdin_data.get("message") or
                  stdin_data.get("text") or "").strip()
        if not prompt:
            return None  # 空 prompt 不触发 thinking
        return "thinking"

    return state_hint


def main_set_state() -> None:
    """set-state 子命令入口。

    用法: vibectl set-state <agent> <state_hint> [--event <event>]

    agent: claude / codex
    state_hint: thinking / working / alert / idle / off / auto
    """
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit(1)

    agent = args[0]
    state_hint = args[1]

    event = ""
    if "--event" in args:
        idx = args.index("--event")
        if idx + 1 < len(args):
            event = args[idx + 1]

    stdin_data = _read_stdin_json()
    session_id = (stdin_data.get("session_id") or
                  stdin_data.get("conversation_id") or "").strip()

    # 检查是否是子代理触发
    agent_id = stdin_data.get("agent_id")
    is_subagent = bool(agent_id)

    # 子代理过滤：如果主代理已停止，忽略子代理残留事件
    if is_subagent and session_id:
        current = _read_current_state(agent, session_id)
        if current and current.get("state") == "idle":
            # 主代理已 Stop，忽略子代理的残留事件
            sys.exit(0)

    # 确定最终状态
    state = _resolve_state(event, state_hint, stdin_data)
    if state is None:
        sys.exit(0)

    # session_id 处理
    if not session_id:
        if state == "off":
            # 全局 off：写 _global_off 标记
            d = state_dir_for(agent)
            path = os.path.join(d, "_global_off")
            data = json.dumps({"state": "off", "ts": time.time()})
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
        sys.exit(0)

    # 安全校验：session_id 不能含路径分隔符
    if os.sep in session_id or "/" in session_id:
        sys.exit(1)

    if state == "off":
        _delete_state(agent, session_id)
    else:
        _write_state(agent, session_id, state, is_subagent=is_subagent)


def main_start_daemon() -> None:
    """start-daemon 子命令入口。被 SessionStart hook 调用，确保 daemon 运行。"""
    from .proc import pid_alive
    from .config import PID_FILE, LOCK_FILE

    # 检查 PID 文件
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid_alive(pid):
            sys.exit(0)  # daemon 已在运行
    except (OSError, ValueError):
        pass

    # 启动 daemon
    import subprocess
    vibe_cmd = _get_daemon_argv()
    try:
        subprocess.Popen(
            vibe_cmd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except Exception:
        pass


def main_set_alert() -> None:
    """set-alert 子命令入口。用于 PermissionRequest hook。

    写入 alert 状态，然后输出 defer JSON 让 IDE 继续显示权限弹窗。
    """
    args = sys.argv[1:]
    agent = args[0] if args else "claude"

    stdin_data = _read_stdin_json()
    session_id = (stdin_data.get("session_id") or
                  stdin_data.get("conversation_id") or "").strip()

    agent_id = stdin_data.get("agent_id")
    is_subagent = bool(agent_id)

    # 子代理过滤
    if is_subagent and session_id:
        current = _read_current_state(agent, session_id)
        if current and current.get("state") == "idle":
            sys.exit(0)

    if session_id and os.sep not in session_id and "/" not in session_id:
        _write_state(agent, session_id, "alert", is_subagent=is_subagent)

    # 输出 defer 决策
    print(json.dumps({"hookSpecificOutput": {"permissionDecision": "defer"}}))


def _get_daemon_argv() -> list[str]:
    """获取 daemon 启动命令。"""
    # 打包模式
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon"]

    # 开发模式：优先 pythonw，回退 python
    import shutil
    python = sys.executable
    pythonw = shutil.which("pythonw") or python
    # 查找 vibectl 命令
    vibectl = shutil.which("vibectl")
    if vibectl:
        return [pythonw, vibectl, "daemon"]

    # 回退：python -m vibecodinglight daemon
    return [pythonw, "-m", "vibecodinglight", "daemon"]
