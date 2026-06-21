"""
VibeCodingLight CLI — vibectl 命令行入口。

用法：
  vibectl start          启动守护进程
  vibectl stop           停止守护进程
  vibectl status         查看状态
  vibectl switch <mode>  切换模式 (claude/codex/mixed)
  vibectl setup          交互式配置
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


def _write_claude_hooks(hooks) -> None:
    """写入 Claude Code hooks 到 settings.json。"""
    path = _claude_settings_path()
    settings = {}
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
                    any("vibecodinglight" in str(hk.get("command", "")) or
                        "vibecodinglight" in str(hk.get("command", "")) or "vibectl" in str(hk.get("command", ""))
                        for hk in g.get("hooks", [])))
        ]
        settings["hooks"][h.event].append(group)

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.flush()
    os.replace(tmp, path)


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
                    any("vibecodinglight" in str(hk.get("command", "")) or
                        "vibecodinglight" in str(hk.get("command", "")) or "vibectl" in str(hk.get("command", ""))
                        for hk in g.get("hooks", [])))
        ]
        if len(filtered) != len(original):
            hooks[event] = filtered
            changed = True

    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.flush()
        os.replace(tmp, path)


def _write_codex_hooks(hooks) -> None:
    """写入 Codex hooks 到 hooks.json。"""
    path = _codex_hooks_path()
    config = {}
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

        hook_entry = {"type": "command", "command": cmd, "timeout": 10}
        group = {"matcher": "", "hooks": [hook_entry]}

        if h.event not in config["hooks"]:
            config["hooks"][h.event] = []
        config["hooks"][h.event] = [
            g for g in config["hooks"][h.event]
            if not (isinstance(g, dict) and
                    any("vibecodinglight" in str(hk.get("command", "")) or
                        "vibecodinglight" in str(hk.get("command", "")) or "vibectl" in str(hk.get("command", ""))
                        for hk in g.get("hooks", [])))
        ]
        config["hooks"][h.event].append(group)

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.flush()
    os.replace(tmp, path)


def _remove_codex_hooks() -> None:
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
                    any("vibecodinglight" in str(hk.get("command", "")) or
                        "vibecodinglight" in str(hk.get("command", "")) or "vibectl" in str(hk.get("command", ""))
                        for hk in g.get("hooks", [])))
        ]
        if len(filtered) != len(original):
            hooks[event] = filtered
            changed = True

    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.flush()
        os.replace(tmp, path)


# ── 开机自启 ──────────────────────────────────────────────

def _startup_folder() -> str:
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )


def _install_autostart() -> None:
    """在 Windows 启动文件夹创建快捷方式。"""
    if sys.platform != "win32":
        _print("开机自启仅支持 Windows")
        return

    startup = _startup_folder()
    shortcut_path = os.path.join(startup, "VibeCodingLight.vbs")

    # 用 VBS 启动 pythonw，无控制台窗口
    exe = sys.executable
    if getattr(sys, "frozen", False):
        cmd = f'"{exe}" daemon'
    else:
        pythonw = shutil.which("pythonw") or exe
        cmd = f'"{pythonw}" -m vibecodinglight daemon'

    # VBS 字符串中转义双引号
    cmd_escaped = cmd.replace('"', '""')
    vbs = f'Set WshShell = CreateObject("WScript.Shell")\nWshShell.Run "{cmd_escaped}", 0, False\n'
    with open(shortcut_path, "w", encoding="utf-8") as f:
        f.write(vbs)
    _print(f"已创建开机自启: {shortcut_path}")


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
    pythonw = shutil.which("pythonw") or sys.executable
    return [pythonw, "-m", "vibecodinglight", "daemon"]


# ── 主入口 ────────────────────────────────────────────────

_COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "switch": cmd_switch,
    "setup": cmd_setup,
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
        _print("  vibectl install        安装 hooks + 开机自启")
        _print("  vibectl uninstall      卸载 hooks + 开机自启")
        return

    cmd = sys.argv[1]
    if cmd not in _COMMANDS:
        _print(f"未知命令: {cmd}")
        _print("运行 vibectl --help 查看帮助")
        return

    _COMMANDS[cmd]()
