"""
硬件传输层：USB 串口（默认）或 BLE GATT。

BLE 在独立线程里维持连接并处理写入，与 daemon 主线程的同步接口对接。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial
import serial.tools.list_ports

from .config import (
    BAUD_RATE, BLE_CHAR_UUID, BLE_DEVICE_NAME, BLE_SERVICE_UUID,
    ESP32_VID, _TEMP_DIR,
)

log = logging.getLogger("vibe.transport")

_BLE_CACHE_FILE = os.path.join(_TEMP_DIR, "vibe_ble_device.json")


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
        except Exception:
            pass


@dataclass
class _BleCommand:
    payload: bytes
    ack: Optional[queue.Queue[bool]] = None


class BleLink:
    """BLE GATT 传输，内部 asyncio 循环跑在单独线程中。"""

    def __init__(self) -> None:
        self._name = BLE_DEVICE_NAME
        self._char_uuid = BLE_CHAR_UUID
        self._cmd_queue: queue.Queue[_BleCommand] = queue.Queue(maxsize=32)
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> str:
        return self._last_error or ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="vibe-ble", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_loop())
        finally:
            loop.close()

    async def _find_device(self):
        from bleak import BleakScanner
        device = await BleakScanner.find_device_by_name(self._name, timeout=10.0)
        if device:
            self._write_cache(getattr(device, "address", ""), getattr(device, "name", "") or self._name)
            return device

        target = BLE_SERVICE_UUID.lower()
        devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
        for _addr, (dev, adv) in devices.items():
            name = (dev.name or adv.local_name or "").strip()
            uuids = {str(u).lower() for u in getattr(adv, "service_uuids", [])}
            if name == self._name or target in uuids:
                self._write_cache(getattr(dev, "address", ""), name or self._name)
                return dev

        cached = self._read_cache()
        addr = str(cached.get("address", "")).strip()
        if addr:
            log.info("BLE 扫描未发现广播，尝试缓存地址: %s", addr)
            return addr
        return None

    def _read_cache(self) -> dict:
        try:
            with open(_BLE_CACHE_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _write_cache(self, address: str, name: str = "") -> None:
        if not address:
            return
        data = {"address": address, "name": name or self._name, "ts": time.time()}
        tmp = _BLE_CACHE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
            os.replace(tmp, _BLE_CACHE_FILE)
        except OSError:
            pass

    async def _run_loop(self) -> None:
        from bleak import BleakClient

        backoff = 2
        BACKOFF_MAX = 30

        while not self._stop.is_set():
            try:
                device = await self._find_device()
                if device is None:
                    self._last_error = f"未扫描到 BLE 设备 {self._name}"
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue

                async with BleakClient(device, timeout=30.0) as client:
                    if not client.is_connected:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX)
                        continue

                    self._connected.set()
                    self._last_error = None
                    backoff = 2
                    await asyncio.sleep(0.5)

                    loop = asyncio.get_event_loop()
                    while client.is_connected and not self._stop.is_set():
                        cmd = await loop.run_in_executor(None, self._blocking_get)
                        if cmd is None:
                            continue
                        try:
                            await client.write_gatt_char(self._char_uuid, cmd.payload, response=False)
                            self._ack(cmd, True)
                        except Exception as ex1:
                            try:
                                await client.write_gatt_char(self._char_uuid, cmd.payload, response=True)
                                self._ack(cmd, True)
                            except Exception as ex2:
                                self._last_error = f"BLE 写入失败: {ex2}"
                                self._ack(cmd, False)
                                break
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._last_error = str(ex) or repr(ex)
                log.warning("BLE 会话异常: %s", ex)
            finally:
                self._connected.clear()
                self._drain(False)

            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    def _blocking_get(self) -> Optional[_BleCommand]:
        try:
            return self._cmd_queue.get(timeout=0.15)
        except queue.Empty:
            return None

    def _ack(self, cmd: _BleCommand, ok: bool) -> None:
        if cmd.ack:
            try:
                cmd.ack.put_nowait(ok)
            except queue.Full:
                pass

    def _drain(self, ok: bool = False) -> None:
        while True:
            try:
                self._ack(self._cmd_queue.get_nowait(), ok)
            except queue.Empty:
                break

    def send_raw(self, data: bytes, wait: bool = True, timeout: float = 5.0) -> bool:
        if not self._connected.is_set():
            return False
        ack: Optional[queue.Queue[bool]] = queue.Queue(maxsize=1) if wait else None
        try:
            self._cmd_queue.put_nowait(_BleCommand(bytes(data), ack))
        except queue.Full:
            return False
        if ack is None:
            return True
        try:
            return bool(ack.get(timeout=timeout))
        except queue.Empty:
            return False

    def close(self) -> None:
        self._stop.set()
        self._drain(False)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)


def wait_for_serial(reconnect_interval: float) -> SerialLink:
    """阻塞直到 USB 串口可用。"""
    while True:
        port = find_esp32_port()
        if port:
            try:
                return SerialLink(open_serial(port))
            except Exception:
                pass
        time.sleep(reconnect_interval)


def wait_for_transport(mode: str, reconnect_interval: float):
    """阻塞直到有可用的传输对象。"""
    if mode == "ble":
        link = BleLink()
        link.start()
        while not link.is_connected:
            if link._last_error:
                log.info("等待 BLE: %s", link._last_error)
            time.sleep(1.0)
        return link
    return wait_for_serial(reconnect_interval)
