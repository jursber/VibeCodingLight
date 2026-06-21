# VibeCodingLight

把 Claude Code 和 OpenAI Codex 的工作状态映射到一组三色物理指示灯上，让您不用盯着终端，也能看到 Agent 当前是在等待、思考、调用工具、执行任务，还是需要您处理。

这个项目适合长时间使用 Claude Code / Codex 做 Vibe Coding 的场景：桌面上放一个红黄绿灯，Agent 状态变化时灯会自动变化，支持 Claude、Codex 以及二者同时工作的混合模式。

## 核心能力

- 同时支持 Claude Code hooks 和 Codex hooks。
- 支持 `claude`、`codex`、`mixed` 三种显示模式。
- 通过状态账本合并高频 hook、并发工具调用和子代理状态，减少状态被误覆盖。
- 默认自动扫描 ESP32 串口，USB 拔出后重新插到其他端口也会重连。
- daemon 后台运行，支持开机自启、断线重连、配置热加载。
- 安装、卸载、同步 hooks 前会备份已有 Claude / Codex 配置。
- 提供 `vibectl doctor` 诊断 hooks、状态账本、硬件连接和最近事件。

## 灯光含义

| Agent 状态 | 灯效 | 用户可理解的含义 |
| --- | --- | --- |
| `alert` | 红灯闪烁 | 需要用户处理，例如权限批准、错误、失败 |
| `idle` | 红灯常亮 | Agent 空闲，正在等用户输入 |
| `thinking` | 黄灯呼吸 | 用户已提交需求，模型正在思考或规划 |
| `model` | 绿灯呼吸 | 正在等待模型返回，或工具调用处于活跃状态 |
| `working` | 绿灯常亮 | Agent 正在正常执行工作 |
| `stale` | 绿灯慢闪 | 最近可能仍在执行，但 hook 已一段时间没有刷新 |
| `off` | 全灭 | 会话结束，或长时间无活动后熄灯 |

优先级为：

```text
alert > thinking > model > working > stale > idle > off
```

### 混合模式如何看

`mixed` 模式会分别读取 Claude 和 Codex 的状态，再映射到同一个三色灯上：

- 红灯：空闲或等待用户处理。
- 黄灯：正在思考。
- 绿灯：正在调用工具、等待模型或执行工作。
- 如果 Claude 和 Codex 分别处于不同状态，灯可以同时显示多个颜色。
- 如果两个 Agent 抢同一个颜色通道，显示优先级更高的状态。

说明：VibeCodingLight 反映的是 Claude Code / Codex hooks 暴露出来的可观测状态，不声称读取模型内部真实状态。项目会在复杂场景下做保守推断：如果活跃状态长时间没有新事件，不会直接误判为空闲，而是显示 `stale`。

## 工作原理

```text
Claude Code / Codex hook event
        |
        v
vibecodinglight.hooks
        |
        v
状态账本文件
%LOCALAPPDATA%\Temp\vibe_states\{agent}\{session_id}
        |
        v
vibecodinglight.daemon
50ms 轮询、合并状态、生成灯光帧
        |
        v
USB Serial / BLE
        |
        v
ESP32-C3
        |
        v
红 / 黄 / 绿灯
```

状态账本会记录：

- `main_state`：当前会话主状态。
- `active_tools`：尚未结束的工具调用。
- `active_subagents`：尚未结束的子代理。
- `alerts`：尚未解除的告警。
- `recent_events`：最近 hook 事件，供诊断使用。

## 硬件要求

- Windows 10 / 11。
- Python 3.10 或更高版本。
- ESP32-C3 开发板。
- 三色交通灯模块，或红、黄、绿三路 LED。
- USB 数据线。

默认接线：

| ESP32-C3 GPIO | 灯 |
| --- | --- |
| GPIO 0 | 绿灯 |
| GPIO 1 | 黄灯 |
| GPIO 2 | 红灯 |

默认固件按低电平点亮设计：

```text
LOW  = 亮
HIGH = 灭
```

固件位置：

```text
firmware/vibelight/velight.ino
```

首次使用前，请用 Arduino IDE 打开该文件，选择 ESP32-C3 开发板并烧录。

## 快速开始

### 1. 克隆或复制项目

```powershell
cd E:\Cursor
git clone https://github.com/jursber/VibeCodingLight.git
cd E:\Cursor\VibeCodingLight
```

如果您已经有本地目录，也可以直接进入项目目录。

### 2. 安装 Python 包

推荐使用可编辑安装，方便后续本地修改立即生效：

```powershell
pip install -e .
```

### 3. 安装 hooks 和开机自启

```powershell
vibectl install
```

这一步会做几件事：

- 检测 Claude Code / Codex 配置目录。
- 写入 Claude hooks：`%USERPROFILE%\.claude\settings.json`。
- 写入 Codex hooks：`%USERPROFILE%\.codex\hooks.json`。
- 创建 Windows 开机自启 watchdog。
- 启动后台 daemon。
- 写入配置前自动创建 `.bak-YYYYMMDD-HHMMSS` 备份。

### 4. 重启 Claude Code / Codex

hooks 通常在应用启动时加载。安装后请完全退出 Claude Code / Codex，再重新打开。

### 5. 检查状态

```powershell
vibectl doctor
```

重点看：

- 当前模式是否正确。
- hooks 是否写入。
- daemon 是否运行。
- 硬件是否连接。
- 最近是否有 hook 事件。

## 常用命令

```powershell
vibectl status
```

查看当前模式、传输方式、端口、daemon 和硬件状态。

```powershell
vibectl doctor
```

做深度诊断，适合排查灯不变、状态不准、hooks 没生效、硬件断连等问题。

```powershell
vibectl switch claude
vibectl switch codex
vibectl switch mixed
```

切换显示模式。

```powershell
vibectl start
vibectl stop
```

启动或停止后台 daemon。

```powershell
vibectl sync-runtime
```

把当前仓库代码对应的 hooks / startup 脚本同步到运行环境，并重启 daemon。修改本地代码后建议运行一次。

```powershell
vibectl uninstall
```

卸载 hooks 和开机自启。卸载前同样会备份原配置。

## 配置文件

配置文件位置：

```text
%LOCALAPPDATA%\VibeCodingLight\config.json
```

常见配置：

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `mode` | 显示模式：`claude`、`codex`、`mixed` | `mixed` |
| `transport` | 传输方式：`serial` 或 `ble` | `serial` |
| `serial_port` | 串口号；`auto` 表示自动扫描 ESP32 | `auto` |
| `duty_g` | 绿灯亮度，0-255 | `255` |
| `duty_y` | 黄灯亮度，0-255 | `255` |
| `duty_r` | 红灯亮度，0-255 | `255` |
| `blink_period_ms` | 闪烁周期，毫秒 | `800` |
| `breath_period_ms` | 呼吸周期，毫秒 | `3000` |

daemon 会热加载配置，多数配置改完不需要手动重启。

## USB 换口与自动重连

推荐保持：

```json
{
  "serial_port": "auto",
  "transport": "serial"
}
```

在 `auto` 模式下，daemon 会按 ESP32 的 USB VID 自动扫描串口：

1. USB 拔出后，daemon 会检测连接异常。
2. 连接状态文件会更新为未连接。
3. daemon 进入重连循环。
4. 插到另一个 USB 端口后，会重新扫描并连接新的 COM 口。

连接状态文件：

```text
%LOCALAPPDATA%\Temp\vibe_conn_status.json
```

如果想确认当前识别到的端口：

```powershell
vibectl doctor
```

## 故障排查

### 灯一直不亮

1. 确认 ESP32 固件已经烧录。
2. 确认 USB 线是数据线，不只是充电线。
3. 运行：

```powershell
vibectl doctor
```

4. 查看 daemon 日志：

```text
%LOCALAPPDATA%\Temp\vibe_daemon.log
```

### 灯状态不跟随 Agent 变化

1. 安装或切换模式后，完全退出 Claude Code / Codex 再重新打开。
2. 新建一个会话测试。
3. 运行：

```powershell
vibectl doctor
```

4. 看状态账本中是否出现最近 hook 事件。

### 红灯常亮

红灯常亮表示 `idle`，通常是 Agent 正在等待用户输入。如果实际不该空闲，请重点检查：

- hooks 是否写入正确。
- 当前模式是否选对。
- Claude / Codex 是否在安装 hooks 后重新启动。
- 状态账本是否有新事件。

### 绿灯一直闪

绿灯慢闪通常表示 `stale`：系统认为最近可能仍有活跃任务，但 hook 已经一段时间没有刷新。常见原因：

- Agent 任务仍在后台执行。
- 某个工具调用没有收到结束事件。
- 会话异常结束，账本还没被新事件覆盖。

建议运行：

```powershell
vibectl doctor
```

查看 `active_tools`、`active_subagents` 和最近事件。

### USB 插到新端口后没恢复

1. 确认配置里 `serial_port` 是 `auto`。
2. 运行：

```powershell
vibectl doctor
```

3. 如果仍未连接，重启 daemon：

```powershell
vibectl stop
vibectl start
```

4. 检查是否有其他程序占用了串口，例如 Arduino IDE 串口监视器。

## 项目结构

```text
VibeCodingLight/
├── README.md
├── pyproject.toml
├── config/
│   └── default.json
├── firmware/
│   └── vibelight/
│       └── velight.ino
├── vibecodinglight/
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── daemon.py
│   ├── hooks.py
│   ├── hooks_catalog.py
│   ├── proc.py
│   ├── protocol.py
│   └── transport.py
└── tests/
    └── test_core.py
```

## 开发与测试

安装开发依赖后运行：

```powershell
python -m pytest -q
```

本地代码修改后，如果这台机器上正在运行 VibeCodingLight，建议同步运行环境：

```powershell
python -m vibecodinglight sync-runtime
```

然后再检查：

```powershell
python -m vibecodinglight doctor
```

## 设计边界

VibeCodingLight 的目标是“尽量真实、低延迟、稳定地反映用户需要知道的 Agent 状态”，但它有明确边界：

- 它依赖 Claude Code / Codex hooks，不读取模型内部状态。
- hook 事件本身可能存在缺失、延迟或顺序变化。
- 项目会通过状态账本、优先级、TTL 和 `stale` 状态做保守推断。
- 在极端异常场景下，`doctor` 输出和 daemon 日志是判断真实状态的主要依据。

## License

未声明。发布或复用前请根据您的实际意图补充许可证。
