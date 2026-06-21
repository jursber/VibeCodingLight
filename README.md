# VibeCodingLight

将 Claude Code 和 OpenAI Codex 的工作状态映射到物理交通灯，让 AI 编程的"思考、执行、等待"一目了然。

## 功能

- **三色灯控**：红/黄/绿三色 LED 实时反映 AI 状态
- **多模式**：Claude 专用、Codex 专用、混合模式（同时显示两个 AI 的状态）
- **纯后台**：无 GUI、无托盘图标，用户完全无感知
- **自动启动**：支持开机自启，无需手动操作
- **双传输**：USB 串口 + BLE 蓝牙，可自由切换
- **子代理过滤**：智能识别并过滤子代理残留 hooks，避免灯态异常

## 灯光语义

| 状态 | 灯效 | 含义 |
|------|------|------|
| **alert** | 🔴 红灯闪烁 | 等待用户批准/确认/报错 |
| **idle** | 🔴 红灯常亮 | 等待用户输入 |
| **thinking** | 🟡 黄灯闪烁 | 模型正在思考 |
| **model** | 🟢 绿灯闪烁 | 等待 LLM 返回/调用工具 |
| **working** | 🟢 绿灯常亮 | 正在写文件/正常工作 |

优先级：alert > idle > thinking > model > working

混合模式下，两个 AI 的状态同时显示在不同灯上。例如 Claude 思考（黄灯闪烁）+ Codex 工作（绿灯常亮）。

## 硬件

- ESP32-C3 开发板
- 三色交通灯模块（或红/黄/绿三路 LED）
- USB 连接电脑

接线：
- GPIO 0 → 绿灯
- GPIO 1 → 黄灯
- GPIO 2 → 红灯
- 有源低电平（LOW = 亮，HIGH = 灭）

固件位于 `firmware/vibelight/`，使用 Arduino IDE 烧录。

## 安装

### 1. 安装 Python 包

```powershell
cd VibeCodingLight
pip install -e .
```

安装后自动注册 `vibectl` 命令。

### 2. 配置

```powershell
vibectl setup
```

按提示设置串口号、传输方式、亮度、闪烁周期等。也可以直接编辑配置文件：

```
%LOCALAPPDATA%\VibeCodingLight\config.json
```

### 3. 安装 hooks 和开机自启

```powershell
vibectl install
```

这会：
- 将 hooks 写入 `~/.claude/settings.json`（Claude 模式）或 `~/.codex/hooks.json`（Codex 模式）
- 在 Windows 启动文件夹创建开机自启脚本

### 4. 启动守护进程

```powershell
vibectl start
```

## 使用

### 日常使用

安装完成后，正常使用 Claude Code 或 Codex 即可。守护进程会在后台自动运行，灯会跟随 AI 状态变化。

### 切换模式

```powershell
# 只指示 Claude 的状态
vibectl switch claude

# 只指示 Codex 的状态
vibectl switch codex

# 同时指示两个 AI 的状态
vibectl switch mixed
```

### 查看状态

```powershell
vibectl status
```

### 停止/重启

```powershell
vibectl stop
vibectl start
```

### 卸载

```powershell
vibectl uninstall
```

## 工作原理

```
Claude Code / Codex Hook 事件
        ↓
  hooks.py（写入状态文件）
        ↓
  %LOCALAPPDATA%\Temp\vibe_states\{agent}\{session_id}
        ↓
  daemon.py（50ms 轮询，合并状态）
        ↓
  SET_MULTI 帧（16 字节二进制协议）
        ↓
  USB 串口 / BLE → ESP32-C3
        ↓
  红/黄/绿灯显示
```

### 子代理过滤

Claude Code 的子代理 hooks 在主代理 Stop 后仍可能触发，导致灯态异常。本项目通过以下机制解决：

1. 子代理的 hook payload 中包含 `agent_id` 字段（主代理没有）
2. hook 脚本检测到 `agent_id` 时，检查该 session 是否已处于 idle 状态
3. 如果主代理已 Stop（idle），忽略子代理的残留事件

## 配置文件说明

### 主配置

`%LOCALAPPDATA%\VibeCodingLight\config.json`

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `mode` | 模式：claude/codex/mixed | claude |
| `serial_port` | 串口号，auto 为自动检测 | auto |
| `transport` | 传输方式：serial/ble | serial |
| `duty_g` | 绿灯亮度 (0-255) | 255 |
| `duty_y` | 黄灯亮度 (0-255) | 255 |
| `duty_r` | 红灯亮度 (0-255) | 255 |
| `blink_period_ms` | 闪烁周期 (ms) | 800 |
| `breath_period_ms` | 呼吸周期 (ms) | 3000 |

配置文件修改后，守护进程会自动热重载，无需重启。

### 状态文件

`%LOCALAPPDATA%\Temp\vibe_states\{agent}\{session_id}`

每个文件是一个 JSON，格式：

```json
{
  "state": "working",
  "ts": 1718000000.123,
  "is_subagent": false
}
```

状态文件由 hook 脚本写入，守护进程读取后自动清理过期文件（5 分钟活动态超时，30 分钟文件删除）。

## 多电脑部署

1. 将整个 `VibeCodingLight` 文件夹复制到目标电脑
2. 运行 `pip install -e .`
3. 运行 `vibectl setup` 配置串口
4. 运行 `vibectl install` 安装 hooks 和开机自启
5. 烧录固件（仅首次）

每台电脑的配置独立保存在 `%LOCALAPPDATA%\VibeCodingLight\config.json`。

## 故障排查

### 灯不亮

1. 运行 `vibectl status` 检查守护进程和硬件连接状态
2. 确认 ESP32 固件已烧录
3. 确认串口号正确（`vibectl setup` 重新配置）
4. 确认 hooks 已安装（`vibectl install`）

### 灯态不变化

1. 确认当前模式正确（`vibectl status`）
2. 新建一个 IDE 会话（hooks 在新会话中生效）
3. 检查日志：`%LOCALAPPDATA%\Temp\vibe_daemon.log`

### 串口被占用

关闭占用 COM 口的程序（如 Arduino IDE 串口监视器、其他串口工具），然后重启守护进程。

### 子代理 hooks 残留

本项目已内置子代理过滤机制。如果仍有问题，检查日志中的状态文件写入记录。

## 项目结构

```
VibeCodingLight/
├── README.md
├── pyproject.toml
├── .gitignore
├── config/
│   └── default.json          # 默认配置模板
├── firmware/
│   └── vibelight/
│       └── vibelight.ino     # ESP32-C3 固件
├── vibecodinglight/
│   ├── __init__.py
│   ├── __main__.py           # python -m 入口
│   ├── cli.py                # vibectl 命令行
│   ├── daemon.py             # 核心守护进程
│   ├── hooks.py              # hook 脚本（被 IDE 调用）
│   ├── hooks_catalog.py      # Claude/Codex hook 事件定义
│   ├── protocol.py           # SET_MULTI 协议
│   ├── transport.py          # 串口/BLE 传输层
│   ├── config.py             # 配置管理
│   └── proc.py               # 进程工具
└── docs/
    └── PROTOCOL.md           # 协议说明
```

## License

本仓库当前未声明开源许可证。
