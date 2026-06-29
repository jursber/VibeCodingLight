"""
硬件传输层：USB 串口。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import serial
import serial.tools.list_ports

from .config import (
    BAUD_RATE, ESP32_VID,
)

log = logging.getLogger("vibe.transport")


def find_esp32_port() -> Optional[str]:
    for port in serial.tools.list_ports.comports():
        if port.vid == ESP32_VID:
            return port.device
    return None


def open_serial(port: str) -> serial.Serial:
    ser = serial.Serial(port, BAUD_RATE, timeout=1, dsrdtr=False)
    ser.dtr = False
    ser.rts = False
    return ser


class SerialLink:
    """USB 串口传输。"""

    def __init__(self, ser: serial.Serial) -> None:
        self._ser = ser

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self._ser, "is_open", False))

    def health_check(self) -> bool:
        """轻量检查串口是否仍可用。"""
        try:
            if not getattr(self._ser, "is_open", False):
                return False
            # 读取 in_waiting 会触发 pyserial 查询底层句柄；USB 拔出时常在这里报错。
            getattr(self._ser, "in_waiting", 0)
            return True
        except (serial.SerialException, OSError):
            return False

    def send_raw(self, data: bytes, wait: bool = True, timeout: float = 5.0) -> bool:
        try:
            self._ser.write(data)
            self._ser.flush()
            return True
        except (serial.SerialException, OSError):
            return False

    def close(self) -> None:
        try:
            self._ser.close()
        except (OSError, serial.SerialException):
            pass


def wait_for_serial(reconnect_interval: float, cfg: dict | None = None) -> SerialLink:
    """阻塞直到 USB 串口可用。"""
    cfg = cfg or {}
    while True:
        configured = str(cfg.get("serial_port") or "auto").strip()
        port = configured if configured and configured.lower() != "auto" else find_esp32_port()
        if port:
            try:
                return SerialLink(open_serial(port))
            except (OSError, serial.SerialException):
                pass
        time.sleep(reconnect_interval)


def wait_for_transport(mode: str, reconnect_interval: float, cfg: dict | None = None):
    """阻塞直到串口可用（mode 参数保留以兼容旧调用，仅支持 serial）。"""
    return wait_for_serial(reconnect_interval, cfg)
