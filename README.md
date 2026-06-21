# VibeCodingLight

将 Claude Code 和 OpenAI Codex 的工作状态映射到物理交通灯，让 AI 编程的"思考、执行、等待"一目了然。

## 灯光语义

| 状态 | 灯效 | 含义 |
|------|------|------|
| **alert** | 🔴 红灯闪烁 | 等待用户批准/确认/报错 |
| **idle** | 🔴 红灯常亮 | 等待用户输入 |
| **thinking** | 🟡 黄灯呼吸 | 模型正在思考 |
| **model** | 🟢 绿灯闪烁 | 等待 LLM 返回/调用工具 |
| **working** | 🟢 绿灯常亮 | 正在写文件/正常工作 |

优先级：alert > idle > thinking > model > working

混合模式下，两个 AI 的状态同时显示在不同灯上。例如 Claude 思考（黄灯呼吸）+ Codex 工作（绿灯常亮）。

## 新电脑部署（快速开始）

### 前置条件

- Windows 10 / 11
- Python 3.10+（已安装并加入 PATH）
- ESP32-C3 开发板 + 三色灯模块（已接线）

### 步骤

**1. 复制项目到目标电脑**

将整个 `VibeCodingLight` 文件夹复制到任意位置（如 `E:\Cursor\VibeCodingLight`）。

**2. 安装 Python 包**

```powershell
cd E:\Cursor\VibeCodingLight
pip install -e .
```

**3. 烧录固件（仅首次）**

用 Arduino IDE 打开 `firmware/vibelight/velight.ino`，选择 ESP32-C3 开发板，烧录。

**4. 一键安装**

```powershell
vibectl install
```

这条命令自动完成：
- 检测 ESP32 串口号
- 将 hooks 写入 `~/.claude/settings.json`（Claude Code 的 hooks 配置）
- 创建开机自启脚本（Windows 启动文件夹）
- 启动守护进程

**5. 重启 Claude Code**

**必须完全退出 Claude Code 再重新打开**，hooks 才会生效。新建会话后即可使用。

### 安装后验证

```powershell
vibectl status
```

应显示：
- 模式: claude
- 守护进程: 运行中
- 硬件: 已连接

## 日常使用

安装完成后，正常使用 Claude Code 或 Codex 即可。守护进程在后台自动运行，灯跟随 AI 状态变化。

### 切换模式

```powershell
vibectl switch claude    # 只指示 Claude
vibectl switch codex     # 只指示 Codex
vibectl switch mixed     # 同时指示两个
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

## 硬件

- ESP32-C3 开发板
- 三色交通灯模块（或红/黄/绿三路 LED）
- USB 连接电脑

接线：
- GPIO 0 → 绿灯
- GPIO 1 → 黄灯
- GPIO 2 → 红灯
- 有源低电平（LOW = 亮，HIGH = 灭）

固件位于 `firmware/vibelight/velight.ino`，使用 Arduino IDE 烧录。

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

### 特殊行为

- **启动动画**：守护进程启动时三灯全亮 2 秒，然后熄灭
- **无会话熄灯**：没有活跃会话时灯自动熄灭
- **30 分钟超时**：连续 30 分钟无活动状态自动熄灯
- **子代理过滤**：智能识别并过滤子代理残留 hooks

## 配置文件

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

配置修改后守护进程自动热重载，无需重启。

## 故障排查

### 灯不亮

1. `vibectl status` 检查守护进程和硬件连接
2. 确认 ESP32 固件已烧录
3. 确认 USB 线连接正常

### 灯态不变化

1. `vibectl status` 确认模式正确
2. **完全退出 Claude Code 再重新打开**（hooks 在启动时加载）
3. 新建会话
4. 查看日志：`%LOCALAPPDATA%\Temp\vibe_daemon.log`

### 串口被占用

关闭占用 COM 口的程序（Arduino IDE 串口监视器等），然后 `vibectl stop && vibectl start`。

## 项目结构

```
VibeCodingLight/
├── README.md
├── pyproject.toml
├── config/default.json
├── firmware/vibelight/velight.ino   # ESP32-C3 固件
├── vibecodinglight/
│   ├── cli.py           # vibectl 命令行
│   ├── daemon.py        # 守护进程
│   ├── hooks.py         # hook 脚本
│   ├── hooks_catalog.py # hook 事件定义
│   ├── protocol.py      # SET_MULTI 协议
│   ├── transport.py     # 串口/BLE 传输
│   ├── config.py        # 配置管理
│   └── proc.py          # 进程工具
└── tests/test_core.py
```
