"""核心功能测试。"""

import pytest
from vibecodinglight import protocol as proto
from vibecodinglight.daemon import _pick_highest, _mixed_frame, _state_to_frame
from vibecodinglight.hooks import _resolve_state
from vibecodinglight.config import PRIORITY, load_config, _validate_config


class TestProtocol:
    def test_crc8(self):
        assert proto.crc8(b"test") == 0xB9
        assert proto.crc8(b"") == 0x00

    def test_build_set_multi(self):
        f = proto.build_set_multi(proto.CH_SOLID, proto.CH_OFF, proto.CH_OFF)
        assert len(f) == 16
        assert f[0:2] == proto.MAGIC
        assert f[2] == proto.PROTO_VER
        assert f[3] == proto.CMD_SET_MULTI

    def test_parse_roundtrip(self):
        f = proto.build_set_multi(
            proto.CH_SOLID, proto.CH_BLINK, proto.CH_OFF,
            duty_g=200, duty_y=150, duty_r=100,
            blink_period=1000, breath_period=2000,
        )
        parsed = proto.parse_frame(f)
        assert parsed is not None
        assert parsed[1] == 1000  # blink_period
        assert parsed[2] == 2000  # breath_period
        assert parsed[3] == proto.CH_SOLID  # mode_g
        assert parsed[4] == proto.CH_BLINK  # mode_y
        assert parsed[5] == proto.CH_OFF  # mode_r
        assert parsed[6] == 200  # duty_g
        assert parsed[7] == 150  # duty_y
        assert parsed[8] == 100  # duty_r

    def test_build_off(self):
        f = proto.build_off()
        parsed = proto.parse_frame(f)
        assert parsed is not None
        assert parsed[3] == proto.CH_OFF
        assert parsed[4] == proto.CH_OFF
        assert parsed[5] == proto.CH_OFF

    def test_parse_invalid(self):
        assert proto.parse_frame(b"\x00" * 16) is None
        assert proto.parse_frame(b"\xa5\x5a" + b"\x00" * 14) is None
        assert proto.parse_frame(b"short") is None

    def test_period_clamp(self):
        f = proto.build_set_multi(
            proto.CH_OFF, proto.CH_OFF, proto.CH_OFF,
            blink_period=10,  # below min
        )
        parsed = proto.parse_frame(f)
        assert parsed is not None
        assert parsed[1] == proto.PERIOD_MIN_MS


class TestDaemon:
    def test_pick_highest_empty(self):
        assert _pick_highest({}) == "off"

    def test_pick_highest_single(self):
        assert _pick_highest({"s1": {"state": "thinking"}}) == "thinking"

    def test_pick_highest_priority(self):
        states = {
            "s1": {"state": "thinking"},
            "s2": {"state": "alert"},
            "s3": {"state": "working"},
        }
        assert _pick_highest(states) == "alert"

    def test_state_to_frame(self):
        cfg = load_config()
        for state in ["off", "idle", "alert", "thinking", "model", "working"]:
            f = _state_to_frame(state, cfg)
            assert len(f) == 16
            assert f[0:2] == proto.MAGIC

    def test_mixed_frame_different_channels(self):
        cfg = load_config()
        f = _mixed_frame("thinking", "working", cfg)
        parsed = proto.parse_frame(f)
        assert parsed is not None
        # thinking → yellow blink, working → green solid
        assert parsed[3] == proto.CH_SOLID  # green (working)
        assert parsed[4] == proto.CH_BLINK  # yellow (thinking)
        assert parsed[5] == proto.CH_OFF  # red

    def test_mixed_frame_same_channel(self):
        cfg = load_config()
        f = _mixed_frame("working", "model", cfg)
        parsed = proto.parse_frame(f)
        assert parsed is not None
        # Both map to green; model has higher priority
        assert parsed[3] == proto.CH_BLINK  # green (model wins)

    def test_mixed_frame_red_conflict(self):
        cfg = load_config()
        f = _mixed_frame("idle", "alert", cfg)
        parsed = proto.parse_frame(f)
        assert parsed is not None
        # Both map to red; alert has higher priority
        assert parsed[5] == proto.CH_BLINK  # red (alert wins)


class TestHooks:
    def test_resolve_auto_bash(self):
        assert _resolve_state("PreToolUse", "auto", {"tool_name": "Bash"}) == "model"

    def test_resolve_auto_alert(self):
        assert _resolve_state("PreToolUse", "auto", {"tool_name": "AskUserQuestion"}) == "alert"

    def test_resolve_thinking_with_prompt(self):
        assert _resolve_state("UserPromptSubmit", "thinking", {"prompt": "hello"}) == "thinking"

    def test_resolve_thinking_empty_prompt(self):
        assert _resolve_state("UserPromptSubmit", "thinking", {"prompt": ""}) is None

    def test_resolve_thinking_no_prompt(self):
        assert _resolve_state("UserPromptSubmit", "thinking", {}) is None

    def test_resolve_direct(self):
        assert _resolve_state("Stop", "idle", {}) == "idle"
        assert _resolve_state("SessionEnd", "off", {}) == "off"

    def test_resolve_none(self):
        assert _resolve_state("SessionStart", None, {}) is None


class TestPriority:
    def test_alert_highest(self):
        assert PRIORITY["alert"] < PRIORITY["thinking"]
        assert PRIORITY["alert"] < PRIORITY["idle"]

    def test_thinking_higher_than_model(self):
        assert PRIORITY["thinking"] < PRIORITY["model"]

    def test_idle_higher_than_off(self):
        assert PRIORITY["idle"] < PRIORITY["off"]


class TestConfigValidation:
    def test_valid_config_passes(self):
        cfg = {"mode": "claude", "duty_g": 200, "duty_y": 100, "duty_r": 50,
               "blink_period_ms": 500, "breath_period_ms": 2000, "transport": "serial",
               "serial_port": "COM3"}
        result = _validate_config(cfg)
        assert result["mode"] == "claude"
        assert result["duty_g"] == 200

    def test_invalid_mode_falls_back(self):
        cfg = {"mode": "invalid"}
        result = _validate_config(cfg)
        assert result["mode"] == "claude"

    def test_invalid_transport_falls_back(self):
        cfg = {"transport": "wifi"}
        result = _validate_config(cfg)
        assert result["transport"] == "serial"

    def test_duty_clamped_high(self):
        cfg = {"duty_g": 999, "duty_y": -1, "duty_r": 300}
        result = _validate_config(cfg)
        assert result["duty_g"] == 255
        assert result["duty_y"] == 0
        assert result["duty_r"] == 255

    def test_period_clamped(self):
        cfg = {"blink_period_ms": 10, "breath_period_ms": 99999}
        result = _validate_config(cfg)
        assert result["blink_period_ms"] == 50
        assert result["breath_period_ms"] == 60000

    def test_invalid_duty_type_falls_back(self):
        cfg = {"duty_g": "abc"}
        result = _validate_config(cfg)
        assert result["duty_g"] == 255

    def test_empty_serial_port_falls_back(self):
        cfg = {"serial_port": ""}
        result = _validate_config(cfg)
        assert result["serial_port"] == "auto"
