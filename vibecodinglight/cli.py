"""
VibeCodingLight CLI — vibectl 命令行入口。

用法：
  vibectl start          启动守护进程
  vibectl stop           停止守护进程
  vibectl status         查看状态
  vibectl switch <mode>  切换模式 (claude/codex/mixed)
  vibectl setup          交互式配置
  vibectl doctor         诊断 hooks、状态账本和硬件连接
  vibectl sync-runtime   同步 hooks/startup 并重启 daemon
  vibectl install        安装 hooks + 开机自启
  vibectl uninstall      卸载 hooks + 开机自启
  vibectl daemon         作为守护进程运行（内部命令）
  vibectl set-state      设置状态（内部命令，被 hook 调用）
  vibectl set-alert      设置 alert（内部命令，被 hook 调用）
  vibectl start-daemon   启动 daemon（内部命令，被 hook 调用）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import time


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def cmd_start() -> None:
    """启动守护进程。"""
    from .proc import pid_alive
    from .config import PID_FILE

    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid_alive(pid):
            _print(f"守护进程已在运行 (PID={pid})")
            return
    except (OSError, ValueError):
        pass

    # 启动 daemon
    cmd = _daemon_argv()
    try:
        subprocess.Popen(
            cmd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        _print("守护进程已启动")
    except Exception as e:
        _print(f"启动失败: {e}")


def cmd_stop() -> None:
    """停止守护进程。"""
    from .proc import pid_alive
    from .config import PID_FILE, LOCK_FILE

    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid_alive(pid):
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            _print(f"已停止守护进程 (PID={pid})")
        else:
            _print("守护进程未运行")
    except (OSError, ValueError):
        _print("守护进程未运行")

    # 清理 PID 和锁文件
    for path in [PID_FILE, LOCK_FILE]:
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_status() -> None:
    """查看状态。"""
    from .proc import pid_alive
    from .config import PID_FILE, CONN_STATUS_FILE, load_config

    cfg = load_config()
    _print(f"模式: {cfg.get('mode', 'claude')}")
    _print(f"传输: {cfg.get('transport', 'serial')}")
    _print(f"端口: {cfg.get('serial_port', 'auto')}")

    # daemon 状态
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid_alive(pid):
            _print(f"守护进程: 运行中 (PID={pid})")
        else:
            _print("守护进程: 未运行")
    except (OSError, ValueError):
        _print("守护进程: 未运行")

    # 连接状态
    try:
        with open(CONN_STATUS_FILE, "r", encoding="utf-8") as f:
            conn = json.load(f)
        if conn.get("connected"):
            _print(f"硬件: 已连接 ({conn.get('transport', '')})")
        else:
            _print("硬件: 未连接")
    except (OSError, json.JSONDecodeError):
        _print("硬件: 未知")

    _print()
    _print("状态摘要:")
    _print(_format_state_summary(compact=True))


def _iter_state_records() -> list[tuple[str, str, dict]]:
    from .config import state_dir_for, is_state_file

    records: list[tuple[str, str, dict]] = []
    for agent in ("claude", "codex"):
        try:
            d = state_dir_for(agent)
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not is_state_file(name):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                data = json.loads(raw) if raw.startswith("{") else {"state": raw}
                if isinstance(data, dict):
                    records.append((agent, name, data))
            except (OSError, json.JSONDecodeError):
                continue
    return records


def _format_age(ts) -> str:
    try:
        age = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "未知"
    if age < 1:
        return "<1s"
    if age < 60:
        return f"{age:.0f}s"
    return f"{age / 60:.1f}m"


def _format_state_summary(compact: bool = False) -> str:
    from .daemon import _record_to_state

    records = _iter_state_records()
    if not records:
        return "  无活动 session"

    lines: list[str] = []
    for agent, session_id, data in records:
        raw_state = data.get("state", "unknown")
        state = _record_to_state(data).get("state", raw_state)
        active_tools = data.get("active_tools") if isinstance(data.get("active_tools"), dict) else {}
        active_subagents = data.get("active_subagents") if isinstance(data.get("active_subagents"), dict) else {}
        alerts = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
        raw_suffix = f" raw={raw_state}," if raw_state != state else ""
        line = (
            f"  {agent}/{session_id}: {state}, "
            f"{raw_suffix}"
            f"age={_format_age(data.get('ts'))}, "
            f"tools={len(active_tools)}, subagents={len(active_subagents)}, alerts={len(alerts)}"
        )
        lines.append(line)
        if not compact:
            events = data.get("recent_events") if isinstance(data.get("recent_events"), list) else []
            for item in events[-5:]:
                event = item.get("event", "?")
                event_state = item.get("state", "?")
                detail = item.get("tool_name") or item.get("agent_id") or ""
                suffix = f" ({detail})" if detail else ""
                lines.append(f"    - {event} -> {event_state}{suffix}, age={_format_age(item.get('ts'))}")
    return "\n".join(lines)


def cmd_doctor() -> None:
    """诊断 hooks、状态账本和硬件连接。"""
    from .config import CONFIG_PATH, CONN_STATUS_FILE, load_config

    cfg = load_config()
    _print("=== VibeCodingLight Doctor ===")
    _print(f"配置: {CONFIG_PATH}")
    _print(f"模式: {cfg.get('mode', 'mixed')}")
    _print(f"传输: {cfg.get('transport', 'serial')}")
    _print(f"端口: {cfg.get('serial_port', 'auto')}")
    _print()

    for label, path in (("Claude hooks", _claude_settings_path()), ("Codex hooks", _codex_hooks_path())):
        exists = os.path.isfile(path)
        _print(f"{label}: {'存在' if exists else '未找到'} ({path})")
        if exists:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                hooks = raw.get("hooks", {})
                vibe_events = []
                for event, groups in hooks.items():
                    for group in groups if isinstance(groups, list) else []:
                        for hk in group.get("hooks", []):
                            cmd = str(hk.get("command", ""))
                            if "vibecodinglight" in cmd or "vibectl" in cmd:
                                vibe_events.append(event)
                _print(f"  Vibe hooks 事件数: {len(set(vibe_events))}")
                if vibe_events:
                    _print(f"  事件: {', '.join(sorted(set(vibe_events)))}")
            except (OSError, json.JSONDecodeError, AttributeError):
                _print("  无法解析 hooks 配置")
    _print()

    try:
        with open(CONN_STATUS_FILE, "r", encoding="utf-8") as f:
            conn = json.load(f)
        _print(
            "硬件: "
            + ("已连接" if conn.get("connected") else "未连接")
            + f", transport={conn.get('transport', '')}, port={conn.get('port', '')}, age={_format_age(conn.get('ts'))}"
        )
    except (OSError, json.JSONDecodeError):
        _print("硬件: 未知（尚无连接状态文件）")

    _print()
    _print("状态账本:")
    _print(_format_state_summary(compact=False))


def cmd_switch() -> None:
    """切换模式。"""
    if len(sys.argv) < 3:
        _print("用法: vibectl switch <claude|codex|mixed>")
        return

    mode = sys.argv[2].lower()
    if mode not in ("claude", "codex", "mixed"):
        _print("无效模式，可选: claude, codex, mixed")
        return

    from .config import load_config, save_config
    cfg = load_config()
    old_mode = cfg.get("mode", "claude")
    cfg["mode"] = mode
    save_config(cfg)

    # 先卸载所有模式的 hooks（避免残留），再安装新模式
    _uninstall_hooks("mixed")
    _install_hooks(mode)

    _print(f"已切换: {old_mode} → {mode}")


def cmd_setup() -> None:
    """交互式配置。"""
    from .config import load_config, save_config, detect_port

    cfg = load_config()

    _print("=== VibeCodingLight 配置 ===")
    _print()

    # 串口
    auto_port = detect_port()
    _print(f"检测到 ESP32 串口: {auto_port}")
    port = input(f"串口号 [{cfg.get('serial_port', 'auto')}]: ").strip()
    if port:
        cfg["serial_port"] = port
    elif cfg.get("serial_port", "auto") == "auto":
        cfg["serial_port"] = "auto"

    # 传输方式
    transport = input(f"传输方式 (serial/ble) [{cfg.get('transport', 'serial')}]: ").strip()
    if transport in ("serial", "ble"):
        cfg["transport"] = transport

    # 亮度
    for ch, label in [("duty_g", "绿灯"), ("duty_y", "黄灯"), ("duty_r", "红灯")]:
        v = input(f"{label}亮度 (0-255) [{cfg.get(ch, 255)}]: ").strip()
        if v.isdigit():
            cfg[ch] = max(0, min(255, int(v)))

    # 闪烁周期
    bp = input(f"闪烁周期 ms [{cfg.get('blink_period_ms', 800)}]: ").strip()
    if bp.isdigit():
        cfg["blink_period_ms"] = max(50, min(60000, int(bp)))

    # 呼吸周期
    brp = input(f"呼吸周期 ms [{cfg.get('breath_period_ms', 3000)}]: ").strip()
    if brp.isdigit():
        cfg["breath_period_ms"] = max(50, min(60000, int(brp)))

    # 模式
    mode = input(f"模式 (claude/codex/mixed) [{cfg.get('mode', 'claude')}]: ").strip()
    if mode in ("claude", "codex", "mixed"):
        cfg["mode"] = mode

    save_config(cfg)
    _print("\n配置已保存。")


def cmd_install() -> None:
    """安装 hooks + 开机自启。"""
    from .config import load_config, save_config
    cfg = load_config()
    mode = cfg.get("mode", "mixed")

    # 检测 Claude 和 Codex 是否安装
    claude_installed = os.path.isdir(os.path.expanduser("~/.claude"))
    codex_installed = os.path.isdir(os.path.expanduser("~/.codex"))

    _print("环境检测:")
    _print(f"  Claude Code: {'已安装' if claude_installed else '未检测到'}")
    _print(f"  OpenAI Codex: {'已安装' if codex_installed else '未检测到'}")
    _print()

    # 如果是混合模式但某个未安装，降级并告知
    if mode == "mixed":
        if not claude_installed and not codex_installed:
            _print("错误: Claude Code 和 Codex 均未检测到，无法安装 hooks。")
            return
        if not codex_installed:
            _print("未检测到 Codex，自动切换为 Claude 独占模式。")
            mode = "claude"
            cfg["mode"] = mode
            save_config(cfg)
        elif not claude_installed:
            _print("未检测到 Claude Code，自动切换为 Codex 独占模式。")
            mode = "codex"
            cfg["mode"] = mode
            save_config(cfg)

    _install_hooks(mode)
    _install_autostart()
    _print()
    _print(f"安装完成。当前模式: {mode}")
    _print("请重启 Claude Code / Codex 以加载 hooks。")


def cmd_sync_runtime() -> None:
    """同步正在运行的 hooks/startup/daemon 到当前仓库代码。"""
    from .config import load_config

    cfg = load_config()
    mode = cfg.get("mode", "mixed")
    _print("同步运行环境:")
    _print(f"  模式: {mode}")
    _install_hooks(mode)
    _install_autostart()
    cmd_stop()
    cmd_start()
    _print("同步完成。hooks 和开机自启已重写，daemon 已重启。")


def cmd_uninstall() -> None:
    """卸载 hooks + 开机自启。"""
    from .config import load_config
    cfg = load_config()
    mode = cfg.get("mode", "claude")

    _uninstall_hooks(mode)
    _uninstall_autostart()
    _print("卸载完成。")


def cmd_daemon() -> None:
    """作为守护进程运行。"""
    from .daemon import main
    main()


def cmd_set_state() -> None:
    """设置状态（被 hook 调用）。"""
    from .hooks import main_set_state
    main_set_state()


def cmd_start_daemon() -> None:
    """启动 daemon（被 hook 调用）。"""
    from .hooks import main_start_daemon
    main_start_daemon()


def cmd_set_alert() -> None:
    """设置 alert（被 hook 调用）。"""
    from .hooks import main_set_alert
    main_set_alert()


# ── Hook 安装/卸载 ────────────────────────────────────────

def _get_hook_command(script: str, *args: str) -> str:
    """获取写入 hooks.json 的命令字符串。路径使用正斜杠（兼容 Git Bash）。"""
    if getattr(sys, "frozen", False):
        parts = [sys.executable.replace("\\", "/"), script] + list(args)
    else:
        python = sys.executable.replace("\\", "/")
        parts = [python, "-m", "vibecodinglight", script] + list(args)
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def _install_hooks(mode: str) -> None:
    """安装 hooks 到 IDE 配置文件。"""
    from .hooks_catalog import hooks_for, CLAUDE_HOOKS, CODEX_HOOKS

    if mode in ("claude", "mixed"):
        _write_claude_hooks(CLAUDE_HOOKS)
    if mode in ("codex", "mixed"):
        _write_codex_hooks(CODEX_HOOKS)


def _uninstall_hooks(mode: str) -> None:
    """从 IDE 配置文件卸载 hooks。"""
    if mode in ("claude", "mixed"):
        _remove_claude_hooks()
    if mode in ("codex", "mixed"):
        _remove_codex_hooks()


def _claude_settings_path() -> str:
    return os.path.expanduser("~/.claude/settings.json")


def _codex_hooks_path() -> str:
    return os.path.expanduser("~/.codex/hooks.json")


def _codex_event_key(event: str) -> str:
    # Codex 现在使用驼峰事件名作为 hooks.json 的标准键名。
    return str(event)


def _legacy_codex_event_key(event: str) -> str:
    chars = []
    for i, ch in enumerate(str(event)):
        if ch.isupper() and i > 0:
            chars.append("_")
        chars.append(ch.lower())
    return "".join(chars)


def _is_vibe_hook_command(command: str) -> bool:
    text = str(command).replace("\\", "/").lower()
    markers = (
        "vibecodinglight",
        "vibectl",
        "claude_traffic_light",
        "start_daemon_unified.py",
        "set_state_unified.py",
        "set_alert_and_defer.py",
    )
    return any(marker in text for marker in markers)


def _backup_file(path: str) -> str | None:
    """写配置前创建时间戳备份；文件不存在时不生成备份。"""
    if not os.path.isfile(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}"
    suffix = 1
    while os.path.exists(backup):
        suffix += 1
        backup = f"{path}.bak-{stamp}-{suffix}"
    shutil.copy2(path, backup)
    return backup


def _replace_or_overwrite(tmp: str, path: str) -> None:
    """Prefer atomic replace; fall back to in-place overwrite on Windows locks."""
    try:
        os.replace(tmp, path)
        return
    except PermissionError:
        pass

    with open(tmp, "r", encoding="utf-8") as src:
        content = src.read()
    with open(path, "w", encoding="utf-8") as dst:
        dst.write(content)
        dst.flush()
        try:
            os.fsync(dst.fileno())
        except OSError:
            pass
    try:
        os.remove(tmp)
    except OSError:
        pass


def _write_claude_hooks(hooks) -> None:
    """写入 Claude Code hooks 到 settings.json。"""
    path = _claude_settings_path()
    settings = {}
    existed = os.path.isfile(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    if "hooks" not in settings:
        settings["hooks"] = {}

    for h in hooks:
        if not h.wired:
            continue

        if h.state is None:
            # SessionStart → start-daemon
            cmd = _get_hook_command("start-daemon")
        elif h.event == "PermissionRequest":
            cmd = _get_hook_command("set-alert", "claude")
        else:
            cmd = _get_hook_command("set-state", "claude", h.state or "auto",
                                    "--event", h.event)

        hook_entry = {"type": "command", "command": cmd, "timeout": 10}
        group = {"matcher": "", "hooks": [hook_entry]}

        if h.event not in settings["hooks"]:
            settings["hooks"][h.event] = []
        # 替换已有的 vibe hooks
        settings["hooks"][h.event] = [
            g for g in settings["hooks"][h.event]
            if not (isinstance(g, dict) and
                    any(_is_vibe_hook_command(str(hk.get("command", "")))
                        for hk in g.get("hooks", [])))
        ]
        settings["hooks"][h.event].append(group)

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if existed:
        _backup_file(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.flush()
    _replace_or_overwrite(tmp, path)


def _remove_claude_hooks() -> None:
    path = _claude_settings_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        return

    hooks = settings.get("hooks", {})
    changed = False
    for event in list(hooks.keys()):
        original = hooks[event]
        filtered = [
            g for g in original
            if not (isinstance(g, dict) and
                    any(_is_vibe_hook_command(str(hk.get("command", "")))
                        for hk in g.get("hooks", [])))
        ]
        if len(filtered) != len(original):
            hooks[event] = filtered
            changed = True

    if changed:
        tmp = path + ".tmp"
        _backup_file(path)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.flush()
        _replace_or_overwrite(tmp, path)


def _write_codex_hooks(hooks) -> None:
    """写入 Codex hooks 到 hooks.json。"""
    path = _codex_hooks_path()
    config = {}
    existed = os.path.isfile(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    if "hooks" not in config:
        config["hooks"] = {}

    for h in hooks:
        if not h.wired:
            continue

        if h.state is None:
            cmd = _get_hook_command("start-daemon")
        elif h.event == "PermissionRequest":
            cmd = _get_hook_command("set-alert", "codex")
        else:
            cmd = _get_hook_command("set-state", "codex", h.state or "auto",
                                    "--event", h.event)

        event_key = _codex_event_key(h.event)
        legacy_event_key = _legacy_codex_event_key(h.event)
        # Codex 读取驼峰事件键；同时清理旧的下划线键，避免残留双写。
        hook_entry = {"type": "command", "command": cmd, "timeout": 10}
        group = {"matcher": "", "hooks": [hook_entry]}

        for key in (h.event, event_key, legacy_event_key):
            if key not in config["hooks"]:
                continue
            config["hooks"][key] = [
                g for g in config["hooks"][key]
                if not (isinstance(g, dict) and
                        any(_is_vibe_hook_command(str(hk.get("command", "")))
                            for hk in g.get("hooks", [])))
            ]
            if not config["hooks"][key]:
                del config["hooks"][key]

        config["hooks"].setdefault(event_key, []).append(group)

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if existed:
        _backup_file(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.flush()
    _replace_or_overwrite(tmp, path)


def _remove_codex_hooks() -> None:
    from .hooks_catalog import CODEX_HOOKS

    path = _codex_hooks_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return

    hooks = config.get("hooks", {})
    changed = False
    for event in list(hooks.keys()):
        original = hooks[event]
        filtered = [
            g for g in original
            if not (isinstance(g, dict) and
                    any(_is_vibe_hook_command(str(hk.get("command", "")))
                        for hk in g.get("hooks", [])))
        ]
        if len(filtered) != len(original):
            hooks[event] = filtered
            changed = True

    # Remove legacy snake_case keys that may have been left behind by older installs.
    for event in list(hooks.keys()):
        if event in {_legacy_codex_event_key(h.event) for h in CODEX_HOOKS}:
            del hooks[event]
            changed = True

    if changed:
        tmp = path + ".tmp"
        _backup_file(path)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.flush()
        _replace_or_overwrite(tmp, path)


# ── 开机自启 ──────────────────────────────────────────────

def _startup_folder() -> str:
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )


def _install_autostart() -> None:
    """在 Windows 启动文件夹创建 watchdog 脚本（每 30 秒检查，挂了自动重启）。"""
    if sys.platform != "win32":
        _print("开机自启仅支持 Windows")
        return

    startup = _startup_folder()
    shortcut_path = os.path.join(startup, "VibeCodingLight.vbs")

    # daemon 启动命令。复用 CLI 的 argv，避免 startup 和手动启动使用不同解释器。
    daemon_cmd = " ".join(f'"{p}"' if " " in p else p for p in _daemon_argv())
    daemon_cmd_escaped = daemon_cmd.replace('"', '""')

    # Watchdog VBS：每 30 秒检查一次，daemon 挂了自动重启
    vbs = f'''Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
tempDir = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\\Temp"
pidFile = tempDir & "\\vibe_daemon.pid"
lockFile = tempDir & "\\vibe_daemon.lock"

Function IsDaemonRunning()
    IsDaemonRunning = False
    If Not fso.FileExists(pidFile) Then Exit Function
    Set f = fso.OpenTextFile(pidFile, 1)
    If f.AtEndOfStream Then f.Close: Exit Function
    pid = Trim(f.ReadAll())
    f.Close
    If pid = "" Then Exit Function
    ' 用 WMI 查询进程，完全无窗口
    On Error Resume Next
    Set wmi = GetObject("winmgmts:\\\\.\\root\\cimv2")
    If Err.Number <> 0 Then Err.Clear: Exit Function
    Set procs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ProcessId = " & pid)
    If Err.Number <> 0 Then Err.Clear: Exit Function
    IsDaemonRunning = (procs.Count > 0)
    On Error GoTo 0
End Function

' 首次启动
If Not IsDaemonRunning() Then
    If fso.FileExists(lockFile) Then fso.DeleteFile lockFile, True
    WshShell.Run "{daemon_cmd_escaped}", 0, False
    WScript.Sleep 3000
End If

' Watchdog 循环：每 30 秒检查一次
Do
    WScript.Sleep 30000
    If Not IsDaemonRunning() Then
        If fso.FileExists(lockFile) Then fso.DeleteFile lockFile, True
        WshShell.Run "{daemon_cmd_escaped}", 0, False
        WScript.Sleep 3000
    End If
Loop
'''
    with open(shortcut_path, "w", encoding="utf-8") as f:
        f.write(vbs)
    _print(f"已创建开机自启 (watchdog): {shortcut_path}")


def _uninstall_autostart() -> None:
    startup = _startup_folder()
    shortcut_path = os.path.join(startup, "VibeCodingLight.vbs")
    try:
        os.remove(shortcut_path)
        _print("已移除开机自启")
    except OSError:
        _print("开机自启不存在")


def _daemon_argv() -> list[str]:
    """获取 daemon 启动命令。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon"]
    return [sys.executable, "-m", "vibecodinglight", "daemon"]


# ── 主入口 ────────────────────────────────────────────────

_COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "switch": cmd_switch,
    "setup": cmd_setup,
    "doctor": cmd_doctor,
    "sync-runtime": cmd_sync_runtime,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "daemon": cmd_daemon,
    "set-state": cmd_set_state,
    "start-daemon": cmd_start_daemon,
    "set-alert": cmd_set_alert,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        _print("VibeCodingLight — AI 编程状态灯控")
        _print()
        _print("用法:")
        _print("  vibectl start          启动守护进程")
        _print("  vibectl stop           停止守护进程")
        _print("  vibectl status         查看状态")
        _print("  vibectl switch <mode>  切换模式 (claude/codex/mixed)")
        _print("  vibectl setup          交互式配置")
        _print("  vibectl doctor         诊断 hooks、状态账本和硬件连接")
        _print("  vibectl sync-runtime   同步 hooks/startup 并重启 daemon")
        _print("  vibectl install        安装 hooks + 开机自启")
        _print("  vibectl uninstall      卸载 hooks + 开机自启")
        return

    cmd = sys.argv[1]
    if cmd not in _COMMANDS:
        _print(f"未知命令: {cmd}")
        _print("运行 vibectl --help 查看帮助")
        return

    _COMMANDS[cmd]()
