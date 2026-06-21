"""
VibeLight 协议 V2 — SET_MULTI 命令，每通道独立模式。

帧格式（16 字节）：
  [0-1]  magic: 0xA5 0x5A
  [2]    ver: 2
  [3]    cmd: 1 (SET_MULTI)
  [4]    flags: bit0=blink_sync, bit1=breath_sync
  [5-6]  blink_period (uint16 LE, ms)
  [7-8]  breath_period (uint16 LE, ms)
  [9]    mode_g: 0=OFF, 1=SOLID, 2=BLINK, 3=BREATH
  [10]   mode_y
  [11]   mode_r
  [12]   duty_g (0-255)
  [13]   duty_y
  [14]   duty_r
  [15]   crc8 (poly=0x07, init=0, 计算范围 byte[2..14])
"""

from __future__ import annotations

MAGIC = bytes((0xA5, 0x5A))
PROTO_VER = 2
CMD_SET_MULTI = 1
FRAME_LEN = 16

# 通道模式
CH_OFF    = 0
CH_SOLID  = 1
CH_BLINK  = 2
CH_BREATH = 3

PERIOD_MIN_MS = 50
PERIOD_MAX_MS = 60000


def crc8(data: bytes) -> int:
    """CRC-8/ATM：多项式 0x07，初值 0。"""
    crc = 0
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def build_set_multi(
    mode_g: int, mode_y: int, mode_r: int,
    duty_g: int = 255, duty_y: int = 255, duty_r: int = 255,
    blink_period: int = 800, breath_period: int = 3000,
    blink_sync: bool = True, breath_sync: bool = True,
) -> bytes:
    """构造 16 字节 SET_MULTI 帧。"""
    blink_period = max(PERIOD_MIN_MS, min(PERIOD_MAX_MS, int(blink_period)))
    breath_period = max(PERIOD_MIN_MS, min(PERIOD_MAX_MS, int(breath_period)))
    duty_g = max(0, min(255, int(duty_g)))
    duty_y = max(0, min(255, int(duty_y)))
    duty_r = max(0, min(255, int(duty_r)))
    mode_g = max(0, min(3, int(mode_g)))
    mode_y = max(0, min(3, int(mode_y)))
    mode_r = max(0, min(3, int(mode_r)))

    flags = (1 if blink_sync else 0) | (2 if breath_sync else 0)
    body = bytes((
        PROTO_VER, CMD_SET_MULTI, flags,
        blink_period & 0xFF, (blink_period >> 8) & 0xFF,
        breath_period & 0xFF, (breath_period >> 8) & 0xFF,
        mode_g, mode_y, mode_r,
        duty_g, duty_y, duty_r,
    ))
    return MAGIC + body + bytes((crc8(body),))


def build_off() -> bytes:
    """全灭帧。"""
    return build_set_multi(CH_OFF, CH_OFF, CH_OFF)


def parse_frame(frame: bytes) -> tuple | None:
    """校验并解析 SET_MULTI 帧；失败返回 None。"""
    if len(frame) != FRAME_LEN or frame[0:2] != MAGIC:
        return None
    if frame[2] != PROTO_VER or frame[3] != CMD_SET_MULTI:
        return None
    body = frame[2:15]
    if crc8(body) != frame[15]:
        return None
    flags = frame[4]
    bp = frame[5] | (frame[6] << 8)
    brp = frame[7] | (frame[8] << 8)
    return (
        flags,
        bp, brp,
        frame[9], frame[10], frame[11],   # modes G, Y, R
        frame[12], frame[13], frame[14],  # dutys G, Y, R
    )
