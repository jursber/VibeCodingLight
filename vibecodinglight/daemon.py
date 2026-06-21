"""
VibeCodingLight 守护进程。

核心功能：
  1. 读取 claude/codex 的状态文件，按模式合并
  2. 生成 SET_MULTI 帧发送到硬件
  3. 单实例保障、配置热重载、自动重连
"""

from __future__ import annotations

import atexit
import json
import logging
import msvcrt
import os
import sys
import time
import traceback

from .config import (
    ACTIVE_STATES, CONN_STATUS_FILE, LOCK_FILE, LOG_FILE,
    PID_FILE, PRIORITY, STATES_ROOT, state_dir_for, load_config,
    detect_port,
)
from .proc import pid_alive
from . import protocol as proto

# ── 日志 ──────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("vibe.daemon")

# ── 常量 ──────────────────────────────────────────────────
POLL_INTERVAL = 0.05          # 50ms
RECONNECT_INTERVAL = 2
ERROR_RETRY_INTERVAL = 1
ACTIVE_STATE_TTL = 300        # 5 分钟
STATE_FILE_TTL = 1800         # 30 分钟
MIN_ACTIVE_HOLD_S = 0.5       # 最短显示时间
INACTIVITY_TIMEOUT_S = 1800   # 30 分钟无活动自动熄灯
ALERT_STALE_S = 5.0           # alert 超过此秒数后，如果有更新的非 alert 状态则降级
ACTIVE_HOLD_STATES = {"alert", "thinking", "model", "working"}
CONN_STATUS_TTL = 6.0


# ── 连接状态文件 ──────────────────────────────────────────
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


# ── 状态文件读取 ──────────────────────────────────────────
def _read_states(agent: str) -> dict[str, dict]:
    """读取某个 agent 的所有 session 状态。"""
    d = state_dir_for(agent)
    states: dict[str, dict] = {}
    now = time.time()

    try:
        files = os.listdir(d)
    except OSError:
        return states

    # 检查 _global_off
    goff = os.path.join(d, "_global_off")
    if "_global_off" in files:
        try:
            with open(goff, "r") as f:
                raw = f.read().strip()
            ts = json.loads(raw).get("ts", 0) if raw.startswith("{") else 0
            if now - ts < 10:
                # 检查是否有比 _global_off 更新的文件
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

    # 读取 session 状态
    for name in files:
        if name.endswith(".tmp") or name.startswith("_"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r") as f:
                raw = f.read().strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                state = data.get("state", "")
                ts = data.get("ts", 0)
                is_sub = data.get("is_subagent", False)
            else:
                state = raw
                ts = os.path.getmtime(path)
                is_sub = False

            # 活动态超时降级
            if state in ACTIVE_STATES and ts > 0 and now - ts > ACTIVE_STATE_TTL:
                log.info("Session %s/%s state %s expired, → idle", agent, name, state)
                state = "idle"
                ts = now
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump({"state": "idle", "ts": ts}, f)
                    os.replace(tmp, path)
                except OSError:
                    pass

            # 文件过期删除
            if ts > 0 and now - ts > STATE_FILE_TTL and state != "off":
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue

            if state in PRIORITY:
                states[name] = {"state": state, "is_subagent": is_sub, "ts": ts}
        except (OSError, json.JSONDecodeError):
            continue

    return states


def _pick_highest(states: dict[str, dict]) -> str:
    """从状态集合中选出最高优先级的状态名。

    特殊规则：alert 超过 ALERT_STALE_S 秒后，如果有更新的非 alert 状态则降级。
    解决问题：PermissionRequest 写入 alert 后，用户批准了操作但红灯一直闪。
    """
    if not states:
        return "off"

    now = time.time()

    # 找到最高优先级的非 alert 状态及其时间
    best_non_alert = "off"
    best_non_alert_p = 99
    best_non_alert_ts = 0.0
    has_alert = False
    newest_alert_ts = 0.0

    for entry in states.values():
        s = entry.get("state", "off")
        ts = entry.get("ts", 0)
        p = PRIORITY.get(s, 99)
        if s == "alert":
            has_alert = True
            if ts > newest_alert_ts:
                newest_alert_ts = ts
        if p < best_non_alert_p or (p == best_non_alert_p and ts > best_non_alert_ts):
            best_non_alert = s
            best_non_alert_p = p
            best_non_alert_ts = ts

    # 如果有 alert，检查是否过期
    if has_alert:
        alert_age = now - newest_alert_ts
        if alert_age > ALERT_STALE_S:
            # alert 过期，如果有更新的非 alert 状态则使用它
            if best_non_alert != "off" and best_non_alert_ts > newest_alert_ts:
                return best_non_alert
        return "alert"

    return best_non_alert


# ── 状态 → 帧映射 ────────────────────────────────────────
def _state_to_frame(state: str, cfg: dict) -> bytes:
    """将状态映射为 SET_MULTI 帧。"""
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

    # 默认：红灯常亮（idle）
    return proto.build_set_multi(
        proto.CH_OFF, proto.CH_OFF, proto.CH_SOLID,
        duty_r=dr, blink_period=bp, breath_period=brp,
    )


def _mixed_frame(claude_state: str, codex_state: str, cfg: dict) -> bytes:
    """混合模式：同时显示两个 agent 的状态。

    优先级映射到通道：
      alert(idle) → 红灯
      thinking    → 黄灯
      model/working → 绿灯

    如果两个 agent 映射到同一通道，选高优先级的。
    如果映射到不同通道，同时亮。
    """
    bp = cfg.get("blink_period_ms", 800)
    brp = cfg.get("breath_period_ms", 3000)
    dg = cfg.get("duty_g", 255)
    dy = cfg.get("duty_y", 255)
    dr = cfg.get("duty_r", 255)

    # 状态 → (通道, 模式, duty)
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
        return "off", proto.CH_OFF, 0

    c_ch, c_mode, c_duty = _channel_map(claude_state)
    x_ch, x_mode, x_duty = _channel_map(codex_state)

    # 初始化：全部关闭
    modes = [proto.CH_OFF, proto.CH_OFF, proto.CH_OFF]
    dutys = [0, 0, 0]

    ch_idx = {"green": 0, "yellow": 1, "red": 2}

    # 放置 Claude 状态
    if c_ch != "off":
        idx = ch_idx[c_ch]
        modes[idx] = c_mode
        dutys[idx] = c_duty

    # 放置 Codex 状态（如果通道冲突，选高优先级）
    if x_ch != "off":
        idx = ch_idx[x_ch]
        if modes[idx] == proto.CH_OFF:
            modes[idx] = x_mode
            dutys[idx] = x_duty
        else:
            # 通道冲突：选高优先级
            c_p = PRIORITY.get(claude_state, 99)
            x_p = PRIORITY.get(codex_state, 99)
            if x_p < c_p:
                modes[idx] = x_mode
                dutys[idx] = x_duty

    # 如果全部关闭，显示红灯常亮（idle）
    if all(m == proto.CH_OFF for m in modes):
        modes[2] = proto.CH_SOLID
        dutys[2] = dr

    return proto.build_set_multi(
        modes[0], modes[1], modes[2],
        duty_g=dutys[0], duty_y=dutys[1], duty_r=dutys[2],
        blink_period=bp, breath_period=brp,
    )


# ── 单实例保障 ────────────────────────────────────────────
def _acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    except OSError:
        return None
    try:
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


# ── 主循环 ────────────────────────────────────────────────
def _run_once(link, cfg: dict, cfg_path: str) -> None:
    """主循环：轮询状态文件，发送 SET_MULTI 帧。"""
    cfg_mtime = os.path.getmtime(cfg_path) if os.path.isfile(cfg_path) else 0
    last_frame = None
    last_state = "off"
    last_switch = 0.0
    last_activity = time.time()  # 最后一次有活动状态的时间

    mode = cfg.get("mode", "claude")

    while True:
        try:
            now = time.time()

            # 配置热重载
            try:
                m = os.path.getmtime(cfg_path)
                if m != cfg_mtime:
                    cfg = load_config()
                    cfg_mtime = m
                    mode = cfg.get("mode", "claude")
                    last_frame = None
                    log.info("配置已热重载, mode=%s", mode)
            except OSError:
                pass

            # 读取状态
            has_active_sessions = False
            if mode == "mixed":
                claude_states = _read_states("claude")
                codex_states = _read_states("codex")
                claude_best = _pick_highest(claude_states)
                codex_best = _pick_highest(codex_states)
                best_state = min(claude_best, codex_best,
                                 key=lambda s: PRIORITY.get(s, 99))
                has_active_sessions = bool(claude_states or codex_states)
                frame = _mixed_frame(claude_best, codex_best, cfg)
            else:
                states = _read_states(mode)
                best_state = _pick_highest(states)
                has_active_sessions = bool(states)
                frame = _state_to_frame(best_state, cfg)

            # 有非 off/idle 的活动状态时更新最后活动时间
            if has_active_sessions and best_state not in ("off", "idle"):
                last_activity = now

            # 30 分钟无活动 → 熄灯
            if (now - last_activity) > INACTIVITY_TIMEOUT_S:
                frame = proto.build_off()
                best_state = "off"

            # 最短显示时间保护
            if (
                last_state in ACTIVE_HOLD_STATES
                and (now - last_switch) < MIN_ACTIVE_HOLD_S
                and PRIORITY.get(best_state, 99) >= PRIORITY.get(last_state, 99)
                and best_state != last_state
            ):
                time.sleep(POLL_INTERVAL)
                continue

            # 发送帧
            if frame != last_frame:
                if not link.send_raw(frame, wait=True):
                    detail = getattr(link, "last_error", "") or "无底层错误信息"
                    try:
                        link.close()
                    except Exception:
                        pass
                    raise ConnectionError(f"硬件连接断开: {detail}")
                if best_state != last_state:
                    log.info("灯态: %s → %s (%s)", last_state, best_state, frame.hex())
                last_frame = frame
                last_state = best_state
                last_switch = now

            time.sleep(POLL_INTERVAL)

        except ConnectionError:
            raise
        except Exception:
            log.warning("主循环异常: %s", traceback.format_exc())
            time.sleep(POLL_INTERVAL)


def main() -> None:
    """守护进程主入口。"""
    # 单实例检查
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

    # 写 PID
    pid_str = str(os.getpid())
    pid_tmp = PID_FILE + ".tmp"
    with open(pid_tmp, "w", encoding="utf-8") as f:
        f.write(pid_str)
        f.flush()
    os.replace(pid_tmp, PID_FILE)

    log.info("守护进程启动, PID=%d", os.getpid())

    def _on_exit():
        log.info("守护进程退出, PID=%d", os.getpid())
        _write_conn_status(False, "stopped")

    atexit.register(_on_exit)

    # 加载配置
    from .config import CONFIG_PATH
    cfg = load_config()
    cfg_path = CONFIG_PATH

    while True:
        link = None
        try:
            # 每次重连前重新加载配置（热重载在 _run_once 内处理，这里是断线重连后）
            cfg = load_config()
            transport = cfg.get("transport", "serial")
            _write_conn_status(False, transport)

            from .transport import wait_for_transport, find_esp32_port
            link = wait_for_transport(transport, RECONNECT_INTERVAL)
            port = find_esp32_port() if transport == "serial" else ""
            _write_conn_status(True, transport, port or "")
            log.info("硬件已连接 (transport=%s)", transport)

            # 启动动画：全亮 2 秒
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
            log.warning("硬件断开，等待重连...")
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
            log.error("致命异常 (%s):\n%s", type(e).__name__, traceback.format_exc())
            _write_conn_status(False, cfg.get("transport", "serial"))
            if link:
                try:
                    link.close()
                except Exception:
                    pass
            time.sleep(ERROR_RETRY_INTERVAL)
