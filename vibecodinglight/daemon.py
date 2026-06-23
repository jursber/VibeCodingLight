"""
VibeCodingLight daemon.

Core responsibilities:
  1. Read Claude/Codex state files and merge them by display mode.
  2. Build SET_MULTI frames for the hardware.
  3. Keep a single daemon instance, hot-reload config, and reconnect hardware.
"""

from __future__ import annotations

import atexit
import json
import logging
import msvcrt
import os
import signal
import sys
import time
import traceback

from .config import (
    ACTIVE_STATES, CONN_STATUS_FILE, IDLE_ACK_FILE, LOCK_FILE, LOG_FILE,
    PID_FILE, PRIORITY, STATES_ROOT, state_dir_for, load_config,
    detect_port,
)
from .proc import pid_alive
from . import protocol as proto

# Logging
os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("vibe.daemon")

# Constants
POLL_INTERVAL = 0.05          # 50ms
RECONNECT_INTERVAL = 2
ERROR_RETRY_INTERVAL = 1
ACTIVE_STATE_TTL = 300        # 5 minutes
STATE_FILE_TTL = 1800         # 30 minutes
MIN_ACTIVE_HOLD_S = 0.5
INACTIVITY_TIMEOUT_S = 1800
ALERT_STALE_S = 5.0
ACTIVE_STATE_STALE_S = 30     # Active state file mtime staleness threshold.
ACTIVE_HOLD_STATES = {"alert", "thinking", "model", "working", "stale"}
CONN_STATUS_TTL = 6.0
DERIVED_ACTIVE_STALE_S = 45.0
ACTIVE_TOOL_EXPIRE_S = 300.0


# Connection status file
def _write_conn_status(connected: bool, transport: str = "", port: str = "") -> None:
    data = {"connected": connected, "transport": transport, "ts": time.time()}
    if port:
        data["port"] = port
    tmp = CONN_STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, CONN_STATUS_FILE)
    except OSError:
        pass


# State file reading
def _read_idle_ack_ts() -> float:
    try:
        with open(IDLE_ACK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("ts") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def _read_states(agent: str) -> dict[str, dict]:
    """Read all session states for an agent."""
    d = state_dir_for(agent)
    states: dict[str, dict] = {}
    now = time.time()
    idle_ack_ts = _read_idle_ack_ts()

    try:
        files = os.listdir(d)
    except OSError:
        return states

    # Honor a recent _global_off marker unless newer session states exist.
    goff = os.path.join(d, "_global_off")
    if "_global_off" in files:
        try:
            with open(goff, "r") as f:
                raw = f.read().strip()
            ts = json.loads(raw).get("ts", 0) if raw.startswith("{") else 0
            if now - ts < 10:
                newer = False
                for name in files:
                    if name.endswith(".tmp") or name.startswith("_"):
                        continue
                    try:
                        if os.path.getmtime(os.path.join(d, name)) > ts:
                            newer = True
                            break
                    except OSError:
                        pass
                if newer:
                    try:
                        os.remove(goff)
                    except OSError:
                        pass
                else:
                    for name in files:
                        if name.endswith(".tmp") or name.startswith("_"):
                            continue
                        try:
                            os.remove(os.path.join(d, name))
                        except OSError:
                            pass
                    return {"_global_off": {"state": "off"}}
            else:
                try:
                    os.remove(goff)
                except OSError:
                    pass
        except (OSError, json.JSONDecodeError):
            pass

    for name in files:
        if name.endswith(".tmp") or name.startswith("_"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r") as f:
                raw = f.read().strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                derived = _record_to_state(data, now=now)
                state = derived.get("state", "")
                ts = derived.get("ts", data.get("ts", 0))
                is_sub = derived.get("is_subagent", data.get("is_subagent", False))
                has_derived_active = bool(data.get("active_tools") or data.get("active_subagents"))
            else:
                state = raw
                ts = os.path.getmtime(path)
                is_sub = False
                has_derived_active = False

            # Hard-expire active states by their logical JSON timestamp.
            if state in ACTIVE_STATES and ts > 0 and now - ts > ACTIVE_STATE_TTL:
                log.info("Session %s/%s state %s expired (ts), -> idle", agent, name, state)
                state = "idle"
                ts = now
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump({"state": "idle", "ts": ts}, f)
                    os.replace(tmp, path)
                except OSError:
                    pass

            if state in ACTIVE_STATES:
                try:
                    file_mtime = os.path.getmtime(path)
                    if now - file_mtime > ACTIVE_STATE_STALE_S:
                        if has_derived_active:
                            # 清除过期的 active_tools 和 active_subagents
                            _cleanup_stale_active_records(path, data, now)
                            log.info("Session %s/%s state %s stale (mtime %.1fs ago), cleaned up expired active records, -> idle",
                                     agent, name, state, now - file_mtime)
                            state = "idle"
                            ts = now
                        else:
                            log.info("Session %s/%s state %s stale without active work (mtime %.1fs ago), -> idle",
                                     agent, name, state, now - file_mtime)
                            state = "idle"
                            ts = now
                except OSError:
                    pass

            # Remove very old state files.
            if ts > 0 and now - ts > STATE_FILE_TTL and state != "off":
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue

            if state == "idle" and ts <= idle_ack_ts:
                continue

            if state in PRIORITY or state == "stale":
                states[name] = {"state": state, "is_subagent": is_sub, "ts": ts}
        except (OSError, json.JSONDecodeError):
            continue

    return states


def _cleanup_stale_active_records(path: str, data: dict, now: float) -> None:
    """清除状态文件中过期的 active_tools 和 active_subagents 记录。

    当 daemon 检测到 stale 状态时调用此函数，将状态重置为 idle。
    """
    if not isinstance(data, dict):
        return

    # 清除所有 active 记录
    data["active_tools"] = {}
    data["active_subagents"] = {}
    data["alerts"] = {}
    data["state"] = "idle"
    data["ts"] = now
    data["main_state"] = "idle"
    data["main_ts"] = now
    data["updated_by"] = "daemon_stale_cleanup"

    # 原子写入
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        pass


def _newest_ts(entries: dict[str, dict]) -> float:
    newest = 0.0
    for item in entries.values():
        try:
            newest = max(newest, float(item.get("ts", 0)))
        except (TypeError, ValueError):
            pass
    return newest


def _record_to_state(record: dict, now: float | None = None) -> dict:
    """Derive the display state from one session ledger."""
    now = time.time() if now is None else now
    active_tools = record.get("active_tools") if isinstance(record.get("active_tools"), dict) else {}
    active_subagents = record.get("active_subagents") if isinstance(record.get("active_subagents"), dict) else {}
    alerts = record.get("alerts") if isinstance(record.get("alerts"), dict) else {}
    ts = float(record.get("ts") or 0)
    main_state = str(record.get("main_state") or record.get("state") or "idle")
    main_ts = float(record.get("main_ts") or ts or now)
    is_sub = bool(record.get("is_subagent", False))

    if alerts:
        return {"state": "alert", "ts": _newest_ts(alerts) or ts or now, "is_subagent": is_sub}

    if active_tools:
        latest = _newest_ts(active_tools) or ts or now
        if now - latest > ACTIVE_TOOL_EXPIRE_S:
            return {"state": "idle", "ts": now, "is_subagent": is_sub}
        if now - latest > DERIVED_ACTIVE_STALE_S:
            return {"state": "stale", "ts": latest, "is_subagent": is_sub}
        return {"state": "model", "ts": latest, "is_subagent": is_sub}

    if active_subagents:
        latest = _newest_ts(active_subagents) or ts or now
        if now - latest > DERIVED_ACTIVE_STALE_S:
            return {"state": "stale", "ts": latest, "is_subagent": True}
        return {"state": "working", "ts": latest, "is_subagent": True}

    return {"state": main_state, "ts": main_ts, "is_subagent": is_sub}


def _pick_highest(states: dict[str, dict]) -> str:
    """Pick the highest-priority state from a set of session states."""
    if not states:
        return "off"

    now = time.time()

    best_non_alert = "off"
    best_non_alert_p = 99
    best_non_alert_ts = 0.0
    has_alert = False
    newest_alert_ts = 0.0

    for entry in states.values():
        s = entry.get("state", "off")
        ts = entry.get("ts", 0)
        p = PRIORITY.get(s, 4.5 if s == "stale" else 99)
        if s == "alert":
            has_alert = True
            if ts > newest_alert_ts:
                newest_alert_ts = ts
            continue  # alert is handled separately.
        if p < best_non_alert_p or (p == best_non_alert_p and ts > best_non_alert_ts):
            best_non_alert = s
            best_non_alert_p = p
            best_non_alert_ts = ts

    if has_alert:
        alert_age = now - newest_alert_ts
        if alert_age > ALERT_STALE_S:
            if best_non_alert != "off" and best_non_alert_ts > newest_alert_ts:
                return best_non_alert
        return "alert"

    return best_non_alert


# State-to-frame mapping
def _state_to_frame(state: str, cfg: dict) -> bytes:
    """Map a state name to a SET_MULTI frame."""
    bp = cfg.get("blink_period_ms", 800)
    brp = cfg.get("breath_period_ms", 3000)
    dg = cfg.get("duty_g", 255)
    dy = cfg.get("duty_y", 255)
    dr = cfg.get("duty_r", 255)

    if state == "off":
        return proto.build_off()

    if state == "idle":
        return proto.build_set_multi(
            proto.CH_OFF, proto.CH_OFF, proto.CH_SOLID,
            duty_r=dr, blink_period=bp, breath_period=brp,
        )

    if state == "alert":
        return proto.build_set_multi(
            proto.CH_OFF, proto.CH_OFF, proto.CH_BLINK,
            duty_r=dr, blink_period=bp, breath_period=brp,
        )

    if state == "thinking":
        return proto.build_set_multi(
            proto.CH_OFF, proto.CH_BREATH, proto.CH_OFF,
            duty_y=dy, blink_period=bp, breath_period=brp,
        )

    if state == "model":
        return proto.build_set_multi(
            proto.CH_BREATH, proto.CH_OFF, proto.CH_OFF,
            duty_g=dg, blink_period=bp, breath_period=brp,
        )

    if state == "working":
        return proto.build_set_multi(
            proto.CH_SOLID, proto.CH_OFF, proto.CH_OFF,
            duty_g=dg, blink_period=bp, breath_period=brp,
        )

    if state == "stale":
        return proto.build_set_multi(
            proto.CH_BLINK, proto.CH_OFF, proto.CH_OFF,
            duty_g=dg, blink_period=max(bp, 1200), breath_period=brp,
        )

    return proto.build_set_multi(
        proto.CH_OFF, proto.CH_OFF, proto.CH_SOLID,
        duty_r=dr, blink_period=bp, breath_period=brp,
    )


def _mixed_frame(claude_state: str, codex_state: str, cfg: dict) -> bytes:
    """Build a mixed-mode frame from two aggregate agent states."""
    bp = cfg.get("blink_period_ms", 800)
    brp = cfg.get("breath_period_ms", 3000)
    dg = cfg.get("duty_g", 255)
    dy = cfg.get("duty_y", 255)
    dr = cfg.get("duty_r", 255)

    # state -> (channel, mode, duty)
    def _channel_map(state: str):
        if state in ("alert",):
            return "red", proto.CH_BLINK, dr
        if state in ("idle",):
            return "red", proto.CH_SOLID, dr
        if state in ("thinking",):
            return "yellow", proto.CH_BREATH, dy
        if state in ("model",):
            return "green", proto.CH_BREATH, dg
        if state in ("working",):
            return "green", proto.CH_SOLID, dg
        if state in ("stale",):
            return "green", proto.CH_BLINK, dg
        return "off", proto.CH_OFF, 0

    modes = [proto.CH_OFF, proto.CH_OFF, proto.CH_OFF]
    dutys = [0, 0, 0]
    ch_idx = {"green": 0, "yellow": 1, "red": 2}

    # 判断是否有活跃状态（非 idle、非 off）
    has_active = any(s not in ("idle", "off") for s in (claude_state, codex_state))

    best_red = None
    best_red_p = 99
    best_non_red = None
    best_non_red_p = 99

    for state in (claude_state, codex_state):
        # 有活跃 session 时，idle 不进入红灯通道
        if state == "idle" and has_active:
            continue
        ch, mode, duty = _channel_map(state)
        if ch == "off":
            continue
        p = PRIORITY.get(state, 4.5 if state == "stale" else 99)
        if ch == "red":
            if p < best_red_p:
                best_red = (ch, mode, duty)
                best_red_p = p
        elif p < best_non_red_p:
            best_non_red = (ch, mode, duty)
            best_non_red_p = p

    for item in (best_red, best_non_red):
        if item is None:
            continue
        ch, mode, duty = item
        idx = ch_idx[ch]
        modes[idx] = mode
        dutys[idx] = duty

    if all(m == proto.CH_OFF for m in modes):
        modes[2] = proto.CH_SOLID
        dutys[2] = dr

    return proto.build_set_multi(
        modes[0], modes[1], modes[2],
        duty_g=dutys[0], duty_y=dutys[1], duty_r=dutys[2],
        blink_period=bp, breath_period=brp,
    )


def _state_summary(states: dict[str, dict]) -> str:
    if not states:
        return "off"
    names = sorted({str(item.get("state", "off")) for item in states.values()})
    return "+".join(names) if names else "off"


def _mixed_frame_from_entries(claude_states: dict[str, dict],
                              codex_states: dict[str, dict], cfg: dict) -> bytes:
    bp = cfg.get("blink_period_ms", 800)
    brp = cfg.get("breath_period_ms", 3000)
    dg = cfg.get("duty_g", 255)
    dy = cfg.get("duty_y", 255)
    dr = cfg.get("duty_r", 255)

    modes = [proto.CH_OFF, proto.CH_OFF, proto.CH_OFF]
    dutys = [0, 0, 0]

    ch_idx = {"green": 0, "yellow": 1, "red": 2}

    def _candidate(state: str):
        if state == "alert":
            return "red", proto.CH_BLINK, dr
        if state == "idle":
            return "red", proto.CH_SOLID, dr
        if state == "thinking":
            return "yellow", proto.CH_BREATH, dy
        if state == "model":
            return "green", proto.CH_BREATH, dg
        if state == "working":
            return "green", proto.CH_SOLID, dg
        if state == "stale":
            return "green", proto.CH_BLINK, dg
        return None

    all_entries = list(claude_states.values()) + list(codex_states.values())

    # 判断是否有活跃 session（非 idle、非 off）
    has_active = any(
        str(e.get("state", "off")) not in ("idle", "off")
        for e in all_entries
    )

    best_red = None
    best_red_p = 99
    best_red_ts = 0.0
    best_non_red = None
    best_non_red_p = 99
    best_non_red_ts = 0.0

    for entry in all_entries:
        state = str(entry.get("state", "off"))
        # 有活跃 session 时，idle 不进入红灯通道
        if state == "idle" and has_active:
            continue
        item = _candidate(state)
        if item is None:
            continue
        ch, mode, duty = item
        p = PRIORITY.get(state, 4.5 if state == "stale" else 99)
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0.0

        if ch == "red":
            if p < best_red_p or (p == best_red_p and ts > best_red_ts):
                best_red = (ch, mode, duty)
                best_red_p = p
                best_red_ts = ts
        elif p < best_non_red_p or (p == best_non_red_p and ts > best_non_red_ts):
            best_non_red = (ch, mode, duty)
            best_non_red_p = p
            best_non_red_ts = ts

    for item in (best_red, best_non_red):
        if item is None:
            continue
        ch, mode, duty = item
        idx = ch_idx[ch]
        modes[idx] = mode
        dutys[idx] = duty

    if all(m == proto.CH_OFF for m in modes):
        modes[2] = proto.CH_SOLID
        dutys[2] = dr

    return proto.build_set_multi(
        modes[0], modes[1], modes[2],
        duty_g=dutys[0], duty_y=dutys[1], duty_r=dutys[2],
        blink_period=bp, breath_period=brp,
    )


def _frame_for_states(mode: str, claude_states: dict[str, dict],
                      codex_states: dict[str, dict], cfg: dict) -> tuple[bytes, str, bool]:
    """Build a frame for the selected display mode."""
    if mode == "mixed":
        label = f"claude:{_state_summary(claude_states)},codex:{_state_summary(codex_states)}"
        return _mixed_frame_from_entries(claude_states, codex_states, cfg), label, bool(claude_states or codex_states)

    states = codex_states if mode == "codex" else claude_states
    best_state = _pick_highest(states)
    return _state_to_frame(best_state, cfg), best_state, bool(states)


def _has_non_idle_activity(mode: str, label: str,
                           claude_states: dict[str, dict],
                           codex_states: dict[str, dict]) -> bool:
    """Return whether any visible session is actively doing work."""
    if mode == "mixed":
        entries = list(claude_states.values()) + list(codex_states.values())
        return any(str(item.get("state", "off")) not in {"off", "idle"} for item in entries)
    return label not in {"off", "idle"}


# Single-instance guard
def _acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    except OSError:
        return None
    try:
        os.write(fd, b"1")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return fd
    except (IOError, OSError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _pid_file_alive() -> bool:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return pid_alive(int(f.read().strip()))
    except (OSError, ValueError):
        return False


# Main loop
def _run_once(link, cfg: dict, cfg_path: str) -> None:
    """Poll state files and send SET_MULTI frames."""
    cfg_mtime = os.path.getmtime(cfg_path) if os.path.isfile(cfg_path) else 0
    last_frame = None
    last_state = "off"
    last_switch = 0.0
    last_activity = time.time()

    mode = cfg.get("mode", "claude")

    while True:
        try:
            now = time.time()

            if hasattr(link, "health_check") and not link.health_check():
                raise ConnectionError("hardware disconnected: health_check failed")

            try:
                m = os.path.getmtime(cfg_path)
                if m != cfg_mtime:
                    cfg = load_config()
                    cfg_mtime = m
                    mode = cfg.get("mode", "claude")
                    last_frame = None
                    log.info("Config hot-reloaded, mode=%s", mode)
            except OSError:
                pass

            claude_states = _read_states("claude") if mode in ("claude", "mixed") else {}
            codex_states = _read_states("codex") if mode in ("codex", "mixed") else {}
            frame, best_state, has_active_sessions = _frame_for_states(
                mode, claude_states, codex_states, cfg
            )

            if has_active_sessions and _has_non_idle_activity(mode, best_state, claude_states, codex_states):
                last_activity = now

            # Turn lights off after long inactivity.
            if (now - last_activity) > INACTIVITY_TIMEOUT_S:
                frame = proto.build_off()
                best_state = "off"

            if (
                last_state in ACTIVE_HOLD_STATES
                and (now - last_switch) < MIN_ACTIVE_HOLD_S
                and PRIORITY.get(best_state, 4.5 if best_state == "stale" else 99) >= PRIORITY.get(last_state, 4.5 if last_state == "stale" else 99)
                and best_state != last_state
            ):
                time.sleep(POLL_INTERVAL)
                continue

            # Send changed frame.
            if frame != last_frame:
                if not link.send_raw(frame, wait=True):
                    detail = getattr(link, "last_error", "") or "no low-level error"
                    try:
                        link.close()
                    except Exception:
                        pass
                    raise ConnectionError(f"hardware disconnected: {detail}")
                if best_state != last_state:
                    log.info("Light state: %s -> %s (%s)", last_state, best_state, frame.hex())
                last_frame = frame
                last_state = best_state
                last_switch = now

            time.sleep(POLL_INTERVAL)

        except ConnectionError:
            raise
        except Exception:
            log.warning("Main loop error: %s", traceback.format_exc())
            time.sleep(POLL_INTERVAL)


def main() -> None:
    """Run the daemon process."""
    lock_fd = _acquire_lock()
    if lock_fd is None and not _pid_file_alive():
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
        time.sleep(0.15)
        lock_fd = _acquire_lock()

    if lock_fd is None:
        sys.exit(0)

    # Write PID.
    pid_str = str(os.getpid())
    pid_tmp = PID_FILE + ".tmp"
    with open(pid_tmp, "w", encoding="utf-8") as f:
        f.write(pid_str)
        f.flush()
    os.replace(pid_tmp, PID_FILE)

    log.info("Daemon started, PID=%d", os.getpid())

    def _on_exit():
        log.info("Daemon stopped, PID=%d", os.getpid())
        _write_conn_status(False, "stopped")

    atexit.register(_on_exit)

    def _signal_handler(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info("Received %s, shutting down gracefully...", sig_name)
        try:
            cfg = load_config()
            mode = cfg.get("mode", "claude")
            agents = ["claude", "codex"] if mode == "mixed" else [mode]
            for agent in agents:
                d = state_dir_for(agent)
                try:
                    for name in os.listdir(d):
                        if name.endswith(".tmp") or name.startswith("_"):
                            continue
                        path = os.path.join(d, name)
                        try:
                            with open(path, "r") as f:
                                raw = f.read().strip()
                            if raw.startswith("{"):
                                data = json.loads(raw)
                                if data.get("state") in ACTIVE_STATES:
                                    tmp = path + ".tmp"
                                    with open(tmp, "w") as f:
                                        json.dump({"state": "idle", "ts": time.time()}, f)
                                    os.replace(tmp, path)
                        except (OSError, json.JSONDecodeError):
                            pass
                except OSError:
                    pass
        except Exception:
            pass
        _write_conn_status(False, "stopped")
        sys.exit(0)

    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, _signal_handler)
    else:
        signal.signal(signal.SIGTERM, _signal_handler)

    # Load config.
    from .config import CONFIG_PATH
    cfg = load_config()
    cfg_path = CONFIG_PATH

    while True:
        link = None
        try:
            cfg = load_config()
            transport = cfg.get("transport", "serial")
            _write_conn_status(False, transport)

            from .transport import wait_for_transport, find_esp32_port
            link = wait_for_transport(transport, RECONNECT_INTERVAL, cfg)
            configured_port = str(cfg.get("serial_port") or "auto")
            port = configured_port if configured_port.lower() != "auto" else find_esp32_port()
            _write_conn_status(True, transport, port or "")
            log.info("Hardware connected (transport=%s)", transport)

            boot_frame = proto.build_set_multi(
                proto.CH_SOLID, proto.CH_SOLID, proto.CH_SOLID,
                duty_g=cfg.get("duty_g", 255),
                duty_y=cfg.get("duty_y", 255),
                duty_r=cfg.get("duty_r", 255),
            )
            link.send_raw(boot_frame, wait=True)
            time.sleep(2)

            _run_once(link, cfg, cfg_path)

        except ConnectionError:
            log.warning("Hardware disconnected, waiting to reconnect...")
            _write_conn_status(False, cfg.get("transport", "serial"))
            if link:
                try:
                    link.close()
                except Exception:
                    pass
            time.sleep(RECONNECT_INTERVAL)

        except (KeyboardInterrupt, SystemExit):
            raise

        except Exception as e:
            log.error("Fatal error (%s):\n%s", type(e).__name__, traceback.format_exc())
            _write_conn_status(False, cfg.get("transport", "serial"))
            if link:
                try:
                    link.close()
                except Exception:
                    pass
            time.sleep(ERROR_RETRY_INTERVAL)
