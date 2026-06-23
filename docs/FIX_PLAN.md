# VibeCodingLight v1.2.x 修复计划

> 综合代码审计两轮发现的全部问题，按优先级排列。
> 固件代码不修改。

---

## P0 — 立即修复（影响灯光正确性）

### 1. stale 检测阈值过短

**问题**：`ACTIVE_STATE_STALE_S = 30` 秒太激进。LLM 思考、复杂工具执行经常超过 30 秒，导致状态被错误降级为 stale（绿灯呼吸），灯光频繁跳变。

**修复**：
- `config.py`：将 `ACTIVE_STATE_STALE_S` 从 30 提高到 120 秒
- 同时将 `DERIVED_ACTIVE_STALE_S` 从 45 提高到 120 秒（`_record_to_state` 中的派生状态 stale 判断）

**文件**：`daemon.py` L48, L51

### 2. stale 清理写入绕过 idle_ack 过滤

**问题**：daemon 在 `_read_states` 中检测到 stale without active work 时，写入 `{"state": "idle", "ts": now}`。这个 `now` 一定大于 `idle_ack_ts`，导致这个"假 idle"不会被过滤，红灯莫名亮起。

**修复**：stale 清理写入 idle 时，保留原始 ts 而不是用 `now`：
```python
# 原来
state = "idle"
ts = now

# 改为
state = "idle"
# ts 保持原始值，不覆盖
```

**文件**：`daemon.py` L186-187

### 3. 状态文件 read-modify-write 竞态

**问题**：`_apply_event_state` 先读、再改、再写，无锁保护。两个 hook 并发时，后写入的会覆盖先写入的修改，导致 active_tools 残留或丢失。

**修复**：在 `_apply_event_state` 的 read-modify-write 外层加 `msvcrt.locking` 文件锁：
```python
def _apply_event_state(...):
    lock_path = path + ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
        # 原有的 read-modify-write 逻辑
        ...
    finally:
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(lock_fd)
```

**文件**：`hooks.py` `_apply_event_state`

### 4. `_clear_other_active_records` 竞态

**问题**：遍历其他 session 并清除 active_tools 时无锁保护，可能误杀并发写入的新 active_tools。

**修复**：对每个 session 文件加锁后再读写（复用 P0-3 的锁机制）。

**文件**：`hooks.py` `_clear_other_active_records`

---

## P1 — 短期改进（提升健壮性）

### 5. idle_ack 机制在多会话下误杀

**问题**：Session B 发送 prompt 时 `_ack_idle` 更新时间戳，导致 Session A 的合法 idle 被过滤。在 mixed 模式下，如果所有 session 都被过滤，灯全灭。

**修复方案**：将 idle_ack 从全局时间戳改为 per-session 机制：
- 删除全局 `_idle_ack` 文件
- 在每个 session 的状态文件中添加 `idle_ack_ts` 字段
- `_read_states` 中按 per-session 的 ack 时间过滤

**文件**：`hooks.py` `_ack_idle`, `daemon.py` `_read_states`, `config.py` `IDLE_ACK_FILE`

### 6. 信号处理器精简

**问题**：`_signal_handler` 在信号上下文中执行大量 I/O（遍历目录、读写文件），可能死锁或数据损坏。

**修复**：信号处理器只设置标志位，主循环负责清理：
```python
_shutdown_requested = False

def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True

# 在 _run_once 主循环中检查
if _shutdown_requested:
    _cleanup_active_states(cfg)
    sys.exit(0)
```

**文件**：`daemon.py` `_signal_handler`, `_run_once`

### 7. `taskkill /F` 不触发信号处理器

**问题**：`cmd_stop` 使用 `taskkill /F`，在 Windows 上不触发 SIGBREAK，跳过状态清理。

**修复**：`cmd_stop` 改用 `taskkill /PID <pid>`（不加 `/F`），或用 `os.kill(pid, signal.CTRL_BREAK_EVENT)` 发送 SIGBREAK。

**文件**：`cli.py` `cmd_stop`

### 8. session_id 增强验证

**问题**：只检查了路径分隔符，未检查空字节、Windows 非法字符、超长字符串。

**修复**：
```python
def _validate_session_id(sid: str) -> bool:
    if not sid or len(sid) > 128:
        return False
    if os.sep in sid or "/" in sid or "\0" in sid:
        return False
    # Windows 非法字符
    if any(c in sid for c in ':*?"<>|'):
        return False
    return True
```

**文件**：`hooks.py` `main_set_state`, `main_set_alert`

### 9. stdin 读取增加大小限制

**问题**：`sys.stdin.read()` 无上限，可能导致内存消耗。

**修复**：`sys.stdin.read(102400)` 限制 100KB。

**文件**：`hooks.py` `_read_stdin_json`

### 10. 收窄 `except Exception`

**问题**：daemon.py 7 处、transport.py 5 处、hooks.py 1 处使用 `except Exception`，可能掩盖非瞬态错误。

**修复**：
- transport.py BLE 写入：捕获 `BleakError` + `asyncio.TimeoutError`
- hooks.py daemon 启动：捕获 `OSError` + `subprocess.SubprocessError`，并记录日志
- daemon.py 信号处理器：捕获 `OSError`（如果保留异常处理的话）

**文件**：`transport.py`, `hooks.py`, `daemon.py`

### 11. 信号处理器异常记录

**问题**：`except Exception: pass` 吞掉所有异常，状态清理失败无日志。

**修复**：改为 `except Exception: log.exception("Signal handler cleanup failed")`

**文件**：`daemon.py` L700

### 12. `main_start_daemon` 启动失败记录

**问题**：`except Exception: pass` 吞掉 Popen 失败，无日志。

**修复**：改为 `except Exception: log.warning("Failed to start daemon", exc_info=True)`

**文件**：`hooks.py` L490

### 13. PID/LOCK 文件 atexit 清理

**问题**：`atexit` 处理器只写 conn_status，不清理 PID/LOCK 文件。

**修复**：在 `_on_exit` 中添加清理逻辑：
```python
def _on_exit():
    log.info("Daemon stopped, PID=%d", os.getpid())
    _write_conn_status(False, "stopped")
    for f in (PID_FILE, LOCK_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
```

**文件**：`daemon.py` `_on_exit`

---

## P2 — 中期改进（提升可维护性）

### 14. 提取原子写入工具函数

**问题**：`tmp + json.dump + flush + fsync + replace` 模式在项目中重复 10+ 次。

**修复**：在 `config.py` 中提取：
```python
def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
```

**文件**：新增 `config.py` 中的工具函数，替换所有调用点

### 15. 提取文件名过滤常量

**问题**：`name.endswith(".tmp") or name.startswith("_")` 重复 7 次。

**修复**：
```python
# config.py
def is_state_file(name: str) -> bool:
    return not name.endswith(".tmp") and not name.startswith("_")
```

**文件**：`config.py` 新增函数，替换 daemon.py/hooks.py/cli.py 中的调用

### 16. 拆分 cli.py

**问题**：cli.py 承担了 CLI 命令、hook 安装/卸载、开机自启管理三类职责，814 行。

**修复**：提取 `installer.py`：
- `_write_claude_hooks` / `_remove_claude_hooks`
- `_write_codex_hooks` / `_remove_codex_hooks`
- `_install_autostart` / `_uninstall_autostart` / `_startup_folder`
- `_backup_file` / `_replace_or_overwrite`
- `_is_vibe_hook_command` / `_legacy_codex_event_key`

**文件**：新增 `installer.py`，`cli.py` 调用它

### 17. 删除死代码

**问题**：
- `_write_state()`（hooks.py L84-100）：定义后从未被调用
- `_mixed_frame()`（daemon.py L342-413）：生产代码未使用，仅测试中引用

**修复**：
- 删除 `_write_state()`
- 统一 `_mixed_frame()` 和 `_mixed_frame_from_entries()` 为一个函数
- 更新测试引用

**文件**：`hooks.py`, `daemon.py`, `tests/test_core.py`

### 18. 补全类型标注

**缺失标注的函数**：
- `_format_age(ts)` — cli.py
- `_run_once(link, cfg, cfg_path)` — daemon.py，`link` 参数应为 `SerialLink | BleLink`
- `_acquire_lock()` — daemon.py，返回 `int | None`
- `wait_for_transport()` — transport.py，返回 `SerialLink | BleLink`
- `_write_claude_hooks(hooks)` / `_write_codex_hooks(hooks)` — cli.py，参数应为 `tuple[HookEntry, ...]`
- transport.py 混用 `Optional[str]` 和 `str | None`，统一为 `str | None`

**文件**：多个文件

### 19. `_global_off` 处理提取为独立函数

**问题**：`_read_states` 中的 `_global_off` 逻辑（daemon.py L102-139）嵌套 5 层，缺乏注释。

**修复**：提取为 `_handle_global_off(d, files, now)` 函数，添加详细注释说明每个分支的业务含义。

**文件**：`daemon.py`

---

## P3 — 长期改进（达到工业级）

### 20. 添加 CI/CD

**修复**：创建 `.github/workflows/ci.yml`：
- Python 3.10/3.11/3.12 矩阵测试
- pytest + coverage
- mypy 类型检查
- ruff lint + format 检查

**文件**：新增 `.github/workflows/ci.yml`

### 21. 添加 lint 和类型检查配置

**修复**：在 `pyproject.toml` 中添加：
```toml
[tool.mypy]
python_version = "3.10"
warn_return_any = true
warn_unused_configs = true

[tool.ruff]
target-version = "py310"
line-length = 100

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**文件**：`pyproject.toml`

### 22. 提升测试覆盖

**优先添加测试的函数**：
1. `_read_states` — 各种 I/O 异常、_global_off 分支、stale 清理
2. `_mixed_frame_from_entries` — 独立测试（当前仅通过 `_frame_for_states` 间接覆盖）
3. `_effective_state` — subagent 优先级推导
4. `_normalize_record` — 记录标准化
5. `pid_alive` — 跨平台进程检测
6. `_ack_idle` — 空闲确认机制
7. `atomic_write_json`（新增的工具函数）

**文件**：`tests/test_core.py` 或新增 `tests/test_*.py`

### 23. BLE 认证机制

**问题**：BLE 无认证，任何设备可连接并注入帧。

**修复**：在 BLE 连接时要求配对或令牌验证。或者在固件端添加简单的挑战-响应认证。

**文件**：`firmware/vibelight/velight.ino`（固件不改，仅记录为未来需求）

### 24. `import msvcrt` 平台保护

**问题**：模块级导入，在 Linux 上直接崩溃。

**修复**：改为延迟导入或加 `sys.platform` 保护：
```python
if sys.platform == "win32":
    import msvcrt
else:
    msvcrt = None  # type: ignore
```

**文件**：`daemon.py`

---

## 执行顺序

```
v1.2.1  P0-1 stale 阈值
        P0-2 stale 清理 ts
        P0-3 状态文件锁
        P0-4 清理记录锁

v1.2.2  P1-5  idle_ack per-session
        P1-6  信号处理器精简
        P1-7  taskkill 替代
        P1-8  session_id 验证
        P1-9  stdin 大小限制
        P1-10 收窄 except
        P1-11 信号异常记录
        P1-12 启动失败记录
        P1-13 atexit 清理

v1.3.0  P2-14 原子写入工具函数
        P2-15 文件名过滤常量
        P2-16 拆分 cli.py
        P2-17 删除死代码
        P2-18 补全类型标注
        P2-19 _global_off 提取

v1.4.0  P3-20 CI/CD
        P3-21 lint + mypy 配置
        P3-22 测试覆盖提升
        P3-24 msvcrt 平台保护
```
