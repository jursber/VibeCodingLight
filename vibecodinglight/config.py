"""
配置管理。

配置文件路径：%LOCALAPPDATA%\\VibeCodingLight\\config.json
状态目录：%LOCALAPPDATA%\\Temp\\vibe_states\\{agent}\\{session_id}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# ── 硬件常量 ──────────────────────────────────────────────
ESP32_VID = 0x303A
DEFAULT_PORT = "COM3"
BAUD_RATE = 115200

BLE_DEVICE_NAME = os.environ.get("VIBE_BLE_NAME", "VibeLight")
BLE_SERVICE_UUID = "e52c12b6-7ac3-4636-9c17-3d608bcea796"
BLE_CHAR_UUID = "e52c12b7-7ac3-4636-9c17-3d608bcea796"

# ── 状态优先级（数字越小越优先）───────────────────────────
PRIORITY = {
    "alert":    1,
    "thinking": 2,
    "model":    3,
    "working":  4,
    "idle":     5,
    "off":      6,
}

ACTIVE_STATES = {"alert", "thinking", "model", "working"}

# ── 路径 ──────────────────────────────────────────────────
_APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VibeCodingLight")
_TEMP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp")

CONFIG_PATH = os.path.join(_APP_DIR, "config.json")
STATES_ROOT = os.path.join(_TEMP_DIR, "vibe_states")
PID_FILE = os.path.join(_TEMP_DIR, "vibe_daemon.pid")
LOCK_FILE = os.path.join(_TEMP_DIR, "vibe_daemon.lock")
LOG_FILE = os.path.join(_TEMP_DIR, "vibe_daemon.log")
CONN_STATUS_FILE = os.path.join(_TEMP_DIR, "vibe_conn_status.json")

# ── 默认配置 ──────────────────────────────────────────────
_DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "claude",
    "serial_port": "auto",
    "transport": "serial",
    "duty_g": 255,
    "duty_y": 255,
    "duty_r": 255,
    "blink_period_ms": 800,
    "breath_period_ms": 3000,
}


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """校验并修正配置值，损坏的字段回退到默认值。"""
    defaults = _DEFAULT_CONFIG
    result = dict(cfg)

    # 模式校验
    if result.get("mode") not in ("claude", "codex", "mixed"):
        result["mode"] = defaults["mode"]

    # 传输方式校验
    if result.get("transport") not in ("serial", "ble"):
        result["transport"] = defaults["transport"]

    # 亮度校验 (0-255)
    for key in ("duty_g", "duty_y", "duty_r"):
        try:
            v = int(result.get(key, defaults[key]))
            result[key] = max(0, min(255, v))
        except (TypeError, ValueError):
            result[key] = defaults[key]

    # 周期校验 (50-60000)
    for key in ("blink_period_ms", "breath_period_ms"):
        try:
            v = int(result.get(key, defaults[key]))
            result[key] = max(50, min(60000, v))
        except (TypeError, ValueError):
            result[key] = defaults[key]

    # 串口校验
    if not isinstance(result.get("serial_port"), str) or not result["serial_port"]:
        result["serial_port"] = defaults["serial_port"]

    return result


def load_config() -> dict[str, Any]:
    """加载配置文件，不存在则创建默认配置。配置损坏时自动修正。"""
    _ensure_dir(_APP_DIR)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config is not a dict")
        return _validate_config(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        cfg = dict(_DEFAULT_CONFIG)
        save_config(cfg)
        return cfg


def save_config(cfg: dict) -> None:
    """原子写入配置文件。"""
    _ensure_dir(_APP_DIR)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, CONFIG_PATH)


def state_dir_for(agent: str) -> str:
    """获取某个 agent 的状态目录。agent 参数必须是纯名称，不含路径分隔符。"""
    if os.sep in agent or "/" in agent or not agent:
        raise ValueError(f"Invalid agent name: {agent!r}")
    d = os.path.join(STATES_ROOT, agent)
    _ensure_dir(d)
    return d


def detect_port() -> str:
    """自动扫描 ESP32 串口。"""
    import serial.tools.list_ports
    for port in serial.tools.list_ports.comports():
        if port.vid == ESP32_VID:
            return port.device
    return DEFAULT_PORT
