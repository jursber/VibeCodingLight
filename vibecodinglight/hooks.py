"""
Hook 脚本 — 被 IDE hook 调用，写入状态文件或启动 daemon。

核心目标：把 hook 事件写成轻量状态账本。
- 主代理、工具调用、子代理和 alert 分开记录，避免高频 hook 互相覆盖
- daemon 再从账本推导用户真正需要看的状态
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
RECENT_EVENTS_LIMIT = 20

# 需要权限弹窗的工具名
ALERT_TOOLS = {"AskUserQuestion", "PermissionRequest"}
TOOL_START_EVENTS = {"PreToolUse"}
TOOL_END_EVENTS = {"PostToolUse", "PostToolUseFailure", "PostToolBatch"}
SUBAGENT_START_EVENTS = {"SubagentStart"}
SUBAGENT_END_EVENTS = {"SubagentStop"}
ALERT_CLEAR_EVENTS = {"PreToolUse", "PostToolUse", "PostToolBatch", "Stop", "SessionEnd"}
TURN_BOUNDARY_EVENTS = {"UserPromptSubmit", "Stop", "SessionEnd"}
SELF_COMMAND_MARKERS = (
    " -m vibecodinglight ",
    "\\vibecodinglight\\",
    "/vibecodinglight/",
    "vibectl ",
)


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
    if not done.wait(timeout=STDIN_TIMEOUT):
        # stdin 读取超时，返回空（hook 进程不应长时间阻塞）
        pass
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


def _write_state_record(agent: str, session_id: str, data: dict[str, Any]) -> None:
    """原子写入完整状态账本。"""
    d = state_dir_for(agent)
    path = os.path.join(d, session_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
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


def _tool_event_id(stdin_data: dict[str, Any]) -> tuple[str, bool]:
    """提取工具调用 ID；平台没有给 ID 时回退到工具名。"""
    for key in ("tool_use_id", "tool_call_id", "tool_id", "id"):
        value = str(stdin_data.get(key) or "").strip()
        if value:
            return value, True
    tool_name = str(stdin_data.get("tool_name") or "tool").strip() or "tool"
    return f"unknown:{tool_name}", False


def _subagent_id(stdin_data: dict[str, Any]) -> str:
    for key in ("agent_id", "subagent_id", "agent_name"):
        value = str(stdin_data.get(key) or "").strip()
        if value:
            return value
    return "unknown-subagent"


def _new_record(state: str, now: float) -> dict[str, Any]:
    return {
        "state": state,
        "ts": now,
        "main_state": state,
        "main_ts": now,
        "active_tools": {},
        "active_subagents": {},
        "alerts": {},
        "recent_events": [],
    }


def _normalize_record(raw: dict[str, Any] | None, state: str, now: float) -> dict[str, Any]:
    if not raw:
        return _new_record(state, now)
    record = dict(raw)
    current = str(record.get("state") or state)
    record.setdefault("state", current)
    record.setdefault("ts", float(record.get("ts") or now))
    record.setdefault("main_state", current)
    record.setdefault("main_ts", float(record.get("ts") or now))
    for key in ("active_tools", "active_subagents", "alerts"):
        if not isinstance(record.get(key), dict):
            record[key] = {}
    if not isinstance(record.get("recent_events"), list):
        record["recent_events"] = []
    return record


def _effective_state(record: dict[str, Any]) -> str:
    if record.get("alerts"):
        return "alert"
    if record.get("active_tools"):
        return "model"
    if record.get("active_subagents"):
        best = "working"
        best_p = PRIORITY.get(best, 99)
        for item in record["active_subagents"].values():
            state = str(item.get("state") or "working")
            p = PRIORITY.get(state, 99)
            if p < best_p:
                best = state
                best_p = p
        return best
    return str(record.get("main_state") or record.get("state") or "idle")


def _append_recent_event(record: dict[str, Any], event: str, state: str,
                         stdin_data: dict[str, Any], now: float) -> None:
    item = {
        "event": event,
        "state": state,
        "ts": now,
    }
    for key in ("tool_name", "tool_use_id", "tool_call_id", "agent_id"):
        value = stdin_data.get(key)
        if value:
            item[key] = value
    events = list(record.get("recent_events") or [])
    events.append(item)
    record["recent_events"] = events[-RECENT_EVENTS_LIMIT:]


def _apply_event_state(agent: str, session_id: str, event: str, state: str,
                       stdin_data: dict[str, Any] | None = None) -> None:
    """把一个 hook 事件合并进 session 状态账本。"""
    stdin_data = stdin_data or {}
    now = time.time()
    record = _normalize_record(_read_current_state(agent, session_id), state, now)

    if event in TOOL_END_EVENTS and record.get("updated_by") in ("Stop", "SessionEnd"):
        _append_recent_event(record, event, str(record.get("state") or "idle"), stdin_data, now)
        _write_state_record(agent, session_id, record)
        return

    if event in TURN_BOUNDARY_EVENTS:
        record["active_tools"] = {}
        record["active_subagents"] = {}

    if event in ALERT_CLEAR_EVENTS:
        record["alerts"] = {}

    if event in TOOL_START_EVENTS:
        tool_id, has_stable_id = _tool_event_id(stdin_data)
        existing = record["active_tools"].get(tool_id, {})
        count = int(existing.get("count", 0)) + 1 if not has_stable_id else 1
        record["active_tools"][tool_id] = {
            "state": "model",
            "ts": now,
            "tool_name": stdin_data.get("tool_name", ""),
            "count": count,
            "stable_id": has_stable_id,
        }
    elif event in TOOL_END_EVENTS:
        tool_id, has_stable_id = _tool_event_id(stdin_data)
        existing = record["active_tools"].get(tool_id)
        if existing and not has_stable_id:
            count = max(0, int(existing.get("count", 1)) - 1)
            if count:
                existing["count"] = count
                existing["ts"] = now
                record["active_tools"][tool_id] = existing
            else:
                record["active_tools"].pop(tool_id, None)
        else:
            record["active_tools"].pop(tool_id, None)
        if event == "PostToolUseFailure":
            record["alerts"]["tool_failure"] = {"state": "alert", "ts": now}
        elif not record["active_tools"]:
            record["main_state"] = state
            record["main_ts"] = now

    if event in SUBAGENT_START_EVENTS:
        sub_id = _subagent_id(stdin_data)
        record["active_subagents"][sub_id] = {
            "state": state,
            "ts": now,
        }
    elif event in SUBAGENT_END_EVENTS:
        sub_id = _subagent_id(stdin_data)
        record["active_subagents"].pop(sub_id, None)

    if state == "alert" and event not in TOOL_END_EVENTS:
        record["alerts"][event or "alert"] = {"state": "alert", "ts": now}

    if (
        event in ("UserPromptSubmit", "Stop", "PreCompact", "PostCompact")
        or event in SUBAGENT_END_EVENTS
        or not stdin_data.get("agent_id")
    ):
        if event not in TOOL_START_EVENTS and event not in TOOL_END_EVENTS:
            record["main_state"] = state
            record["main_ts"] = now

    record["state"] = _effective_state(record)
    record["ts"] = now
    record["updated_by"] = event
    record["is_subagent"] = bool(stdin_data.get("agent_id"))
    _append_recent_event(record, event, record["state"], stdin_data, now)
    _write_state_record(agent, session_id, record)


def _resolve_state(event: str, state_hint: str | None,
                   stdin_data: dict[str, Any]) -> str | None:
    """根据事件和 stdin 数据确定最终状态。

    - state_hint="auto": 根据 tool_name 判断 alert/working
    - state_hint="thinking" 且 event="UserPromptSubmit": 检查是否有 prompt 文本
    - 其他: 直接使用 state_hint
    """
    if state_hint is None:
        return None

    command_text = " ".join(
        str(stdin_data.get(key) or "")
        for key in ("command", "cmd", "input", "tool_input")
    ).lower().replace("\\", "/")
    if any(marker.replace("\\", "/") in command_text for marker in SELF_COMMAND_MARKERS):
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
    # cli.py 调用时 sys.argv[1] 是 "set-state"，需要跳过
    args = sys.argv[1:]
    if args and args[0] == "set-state":
        args = args[1:]

    if len(args) < 2:
        sys.exit(1)

    agent = args[0].lower().strip()
    if agent not in ("claude", "codex"):
        sys.exit(1)
    state_hint = args[1]

    event = ""
    if "--event" in args:
        idx = args.index("--event")
        if idx + 1 < len(args):
            event = args[idx + 1]

    stdin_data = _read_stdin_json()
    session_id = (stdin_data.get("session_id") or
                  stdin_data.get("conversation_id") or "").strip()

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
        _apply_event_state(agent, session_id, event, state, stdin_data)


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
    # cli.py 调用时 sys.argv[1] 是 "set-alert"，需要跳过
    args = sys.argv[1:]
    if args and args[0] == "set-alert":
        args = args[1:]
    agent = (args[0] if args else "claude").lower().strip()
    if agent not in ("claude", "codex"):
        agent = "claude"

    stdin_data = _read_stdin_json()
    session_id = (stdin_data.get("session_id") or
                  stdin_data.get("conversation_id") or "").strip()

    if session_id and os.sep not in session_id and "/" not in session_id:
        _apply_event_state(agent, session_id, "PermissionRequest", "alert", stdin_data)

    # 输出 defer 决策
    print(json.dumps({"hookSpecificOutput": {"permissionDecision": "defer"}}))


def _get_daemon_argv() -> list[str]:
    """获取 daemon 启动命令。"""
    # 打包模式
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon"]

    # 开发模式：使用当前解释器，避免 pythonw/vibectl 包装器绕到另一个 Python。
    return [sys.executable, "-m", "vibecodinglight", "daemon"]
