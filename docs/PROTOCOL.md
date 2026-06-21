# VibeLight 协议 V2

## SET_MULTI 命令（cmd=1，16 字节）

每通道独立控制模式，支持同步闪烁/呼吸。

| 偏移 | 长度 | 字段 | 说明 |
|------|------|------|------|
| 0-1 | 2 | magic | `0xA5 0x5A` |
| 2 | 1 | ver | 协议版本 = 2 |
| 3 | 1 | cmd | 1 = SET_MULTI |
| 4 | 1 | flags | bit0=blink_sync, bit1=breath_sync |
| 5-6 | 2 | blink_period | 闪烁周期 ms，uint16 LE |
| 7-8 | 2 | breath_period | 呼吸周期 ms，uint16 LE |
| 9 | 1 | mode_g | 绿灯模式: 0=OFF, 1=SOLID, 2=BLINK, 3=BREATH |
| 10 | 1 | mode_y | 黄灯模式 |
| 11 | 1 | mode_r | 红灯模式 |
| 12 | 1 | duty_g | 绿灯亮度 0-255 |
| 13 | 1 | duty_y | 黄灯亮度 |
| 14 | 1 | duty_r | 红灯亮度 |
| 15 | 1 | crc8 | CRC-8/ATM，计算范围 byte[2..14] |

### 同步语义

- `blink_sync=1`：所有 BLINK 模式的通道共享同一周期和相位
- `breath_sync=1`：所有 BREATH 模式的通道共享同一周期和相位
- daemon 固定发送 `flags=0x03`（两个都同步）

### CRC8

多项式 0x07，初值 0x00，与固件一致。

### 示例帧

红灯常亮 + 绿灯闪烁（800ms 周期，全亮）：

```
A5 5A 02 01 03 20 03 20 03 02 00 01 FF 00 FF [CRC]
```

其中：
- `03` = blink_sync + breath_sync
- `20 03` = 800ms (0x0320)
- `02 00 01` = 绿灯=BLINK, 黄灯=OFF, 红灯=SOLID
- `FF 00 FF` = 绿灯=255, 黄灯=0, 红灯=255
