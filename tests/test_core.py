"""核心功能测试。"""

import json
import os
import time
import pytest
from vibecodinglight import protocol as proto
from vibecodinglight.daemon import (
    _pick_highest,
    _mixed_frame,
    _state_to_frame,
    _frame_for_states,
    _record_to_state,
)
from vibecodinglight.hooks import _apply_event_state, _read_current_state, _resolve_state
from vibecodinglight.config import PRIORITY, load_config, _validate_config
from vibecodinglight.hooks_catalog import HookEntry


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
        now = time.time()
        states = {
            "s1": {"state": "thinking", "ts": now},
            "s2": {"state": "alert", "ts": now},
            "s3": {"state": "working", "ts": now},
        }
        assert _pick_highest(states) == "alert"

    def test_stale_alert_downgrades(self):
        """alert 超过 5 秒且有更新的非 alert 状态时，自动降级。"""
        now = time.time()
        states = {
            "s1": {"state": "alert", "ts": now - 10},    # 10 秒前的 alert
            "s2": {"state": "working", "ts": now - 2},   # 2 秒前的 working
        }
        assert _pick_highest(states) == "working"

    def test_fresh_alert_wins(self):
        """alert 未过期时仍然优先。"""
        now = time.time()
        states = {
            "s1": {"state": "alert", "ts": now - 2},     # 2 秒前的 alert
            "s2": {"state": "working", "ts": now - 1},   # 1 秒前的 working
        }
        assert _pick_highest(states) == "alert"

    def test_stale_alert_no_newer_state(self):
        """alert 过期但没有更新的非 alert 状态时，仍然显示 alert。"""
        now = time.time()
        states = {
            "s1": {"state": "alert", "ts": now - 10},
            "s2": {"state": "working", "ts": now - 15},  # working 更旧
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
        # thinking → yellow breath, working → green solid
        assert parsed[3] == proto.CH_SOLID  # green (working)
        assert parsed[4] == proto.CH_BREATH  # yellow (thinking)
        assert parsed[5] == proto.CH_OFF  # red

    def test_mixed_frame_same_channel(self):
        cfg = load_config()
        f = _mixed_frame("working", "model", cfg)
        parsed = proto.parse_frame(f)
        assert parsed is not None
        # Both map to green; model has higher priority
        assert parsed[3] == proto.CH_BREATH  # green (model wins)

    def test_mixed_frame_red_conflict(self):
        cfg = load_config()
        f = _mixed_frame("idle", "alert", cfg)
        parsed = proto.parse_frame(f)
        assert parsed is not None
        # Both map to red; alert has higher priority
        assert parsed[5] == proto.CH_BLINK  # red (alert wins)

    def test_frame_for_states_mixed_preserves_two_agents(self):
        cfg = load_config()
        now = time.time()
        frame, label, active = _frame_for_states(
            "mixed",
            {"claude-session": {"state": "thinking", "ts": now}},
            {"codex-session": {"state": "working", "ts": now}},
            cfg,
        )

        parsed = proto.parse_frame(frame)

        assert parsed is not None
        assert parsed[3] == proto.CH_SOLID    # Codex working -> green solid
        assert parsed[4] == proto.CH_BREATH   # Claude thinking -> yellow breath
        assert parsed[5] == proto.CH_OFF
        assert label == "claude:thinking,codex:working"
        assert active is True

    def test_record_to_state_parallel_tools_stays_model_until_all_finish(self):
        now = time.time()
        record = {
            "state": "working",
            "ts": now,
            "active_tools": {
                "tool-a": {"state": "model", "ts": now - 1, "tool_name": "Bash"},
                "tool-b": {"state": "model", "ts": now, "tool_name": "Edit"},
            },
        }

        assert _record_to_state(record, now=now)["state"] == "model"

        del record["active_tools"]["tool-a"]
        assert _record_to_state(record, now=now)["state"] == "model"

        del record["active_tools"]["tool-b"]
        assert _record_to_state(record, now=now)["state"] == "working"

    def test_record_to_state_stale_active_beats_idle_without_lying(self):
        now = time.time()
        record = {
            "state": "model",
            "ts": now - 60,
            "active_tools": {
                "tool-a": {"state": "model", "ts": now - 60, "tool_name": "Bash"},
            },
        }

        assert _record_to_state(record, now=now)["state"] == "stale"

    def test_record_to_state_expires_stale_fallback_tool_counts(self):
        now = time.time()
        record = {
            "state": "model",
            "ts": now - 600,
            "main_state": "working",
            "main_ts": now - 600,
            "active_tools": {
                "unknown:Bash": {
                    "state": "model",
                    "ts": now - 600,
                    "tool_name": "Bash",
                    "count": 6,
                    "stable_id": False,
                },
            },
        }

        assert _record_to_state(record, now=now)["state"] == "idle"


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

    def test_apply_event_state_tracks_parallel_tools(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state(
            "claude",
            "session-1",
            "PreToolUse",
            "model",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )
        _apply_event_state(
            "claude",
            "session-1",
            "PreToolUse",
            "model",
            {"tool_use_id": "tool-b", "tool_name": "Edit"},
        )
        _apply_event_state(
            "claude",
            "session-1",
            "PostToolUse",
            "working",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )

        data = _read_current_state("claude", "session-1")

        assert data["state"] == "model"
        assert set(data["active_tools"]) == {"tool-b"}

    def test_apply_event_state_counts_tools_without_ids(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state("codex", "session-1", "PreToolUse", "model", {"tool_name": "Bash"})
        _apply_event_state("codex", "session-1", "PreToolUse", "model", {"tool_name": "Bash"})
        _apply_event_state("codex", "session-1", "PostToolUse", "working", {"tool_name": "Bash"})

        data = _read_current_state("codex", "session-1")

        assert data["state"] == "model"
        assert data["active_tools"]["unknown:Bash"]["count"] == 1

        _apply_event_state("codex", "session-1", "PostToolUse", "working", {"tool_name": "Bash"})
        data = _read_current_state("codex", "session-1")

        assert data["state"] == "working"
        assert data["active_tools"] == {}

    def test_apply_event_state_tracks_subagent_lifecycle(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state(
            "codex",
            "session-1",
            "SubagentStart",
            "thinking",
            {"agent_id": "sub-1"},
        )
        data = _read_current_state("codex", "session-1")

        assert data["state"] == "thinking"
        assert set(data["active_subagents"]) == {"sub-1"}

        _apply_event_state(
            "codex",
            "session-1",
            "SubagentStop",
            "working",
            {"agent_id": "sub-1"},
        )
        data = _read_current_state("codex", "session-1")

        assert data["state"] == "working"
        assert data["active_subagents"] == {}

    def test_apply_event_state_clears_permission_alert_after_tool_starts(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state("claude", "session-1", "PermissionRequest", "alert", {})
        assert _read_current_state("claude", "session-1")["state"] == "alert"

        _apply_event_state(
            "claude",
            "session-1",
            "PreToolUse",
            "model",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )
        data = _read_current_state("claude", "session-1")

        assert data["state"] == "model"
        assert data["alerts"] == {}

    def test_apply_event_state_stop_clears_active_work(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state(
            "codex",
            "session-1",
            "PreToolUse",
            "model",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )
        _apply_event_state(
            "codex",
            "session-1",
            "SubagentStart",
            "thinking",
            {"agent_id": "sub-1"},
        )

        _apply_event_state("codex", "session-1", "Stop", "idle", {})
        data = _read_current_state("codex", "session-1")

        assert data["state"] == "idle"
        assert data["active_tools"] == {}
        assert data["active_subagents"] == {}

    def test_apply_event_state_new_prompt_clears_previous_turn_residue(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state(
            "codex",
            "session-1",
            "PreToolUse",
            "model",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )

        _apply_event_state("codex", "session-1", "UserPromptSubmit", "thinking", {"prompt": "next"})
        data = _read_current_state("codex", "session-1")

        assert data["state"] == "thinking"
        assert data["active_tools"] == {}

    def test_apply_event_state_ignores_late_post_tool_after_stop(self, tmp_path, monkeypatch):
        import vibecodinglight.config as config

        monkeypatch.setattr(config, "STATES_ROOT", str(tmp_path))

        _apply_event_state(
            "codex",
            "session-1",
            "PreToolUse",
            "model",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )
        _apply_event_state("codex", "session-1", "Stop", "idle", {})
        _apply_event_state(
            "codex",
            "session-1",
            "PostToolUse",
            "working",
            {"tool_use_id": "tool-a", "tool_name": "Bash"},
        )
        data = _read_current_state("codex", "session-1")

        assert data["state"] == "idle"
        assert data["active_tools"] == {}

    def test_resolve_state_ignores_vibecodinglight_self_commands(self):
        assert _resolve_state(
            "PreToolUse",
            "auto",
            {
                "tool_name": "Bash",
                "command": (
                    "C:/Users/Administrator/AppData/Local/hermes/hermes-agent/venv/"
                    "Scripts/python.exe -m vibecodinglight status"
                ),
            },
        ) is None


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
        assert result["mode"] == "mixed"

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


class TestCliSafety:
    def test_write_claude_hooks_creates_backup_before_rewrite(self, tmp_path, monkeypatch):
        from vibecodinglight import cli

        settings_path = tmp_path / "settings.json"
        original = {"hooks": {"Stop": [{"matcher": "", "hooks": []}]}, "keep": True}
        settings_path.write_text(json.dumps(original), encoding="utf-8")

        monkeypatch.setattr(cli, "_claude_settings_path", lambda: str(settings_path))

        cli._write_claude_hooks((HookEntry("Stop", True, "idle"),))

        backups = list(tmp_path.glob("settings.json.bak-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == original

    def test_write_claude_hooks_falls_back_when_replace_is_denied(self, tmp_path, monkeypatch):
        from vibecodinglight import cli

        settings_path = tmp_path / "settings.json"
        original = {"hooks": {}, "keep": True}
        settings_path.write_text(json.dumps(original), encoding="utf-8")

        monkeypatch.setattr(cli, "_claude_settings_path", lambda: str(settings_path))

        real_replace = os.replace

        def deny_replace(src, dst):
            if str(dst) == str(settings_path):
                raise PermissionError("locked by reader")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", deny_replace)

        cli._write_claude_hooks((HookEntry("Stop", True, "idle"),))

        updated = json.loads(settings_path.read_text(encoding="utf-8"))
        assert updated["keep"] is True
        assert updated["hooks"]["Stop"]

        backups = list(tmp_path.glob("settings.json.bak-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == original

    def test_doctor_command_registered(self):
        from vibecodinglight import cli

        assert "doctor" in cli._COMMANDS

    def test_sync_runtime_command_registered(self):
        from vibecodinglight import cli

        assert "sync-runtime" in cli._COMMANDS

    def test_legacy_traffic_light_hook_commands_are_treated_as_vibe_hooks(self):
        from vibecodinglight import cli

        assert cli._is_vibe_hook_command(
            "E:/Cursor/claude_traffic_light/.venv/Scripts/python.exe "
            "E:/Cursor/claude_traffic_light/set_state_unified.py auto"
        )
        assert cli._is_vibe_hook_command(
            "E:/Cursor/claude_traffic_light/.venv/Scripts/python.exe "
            "E:/Cursor/claude_traffic_light/start_daemon_unified.py"
        )

    def test_hook_daemon_argv_does_not_use_vibectl_wrapper(self, monkeypatch):
        from vibecodinglight import hooks

        monkeypatch.setattr("shutil.which", lambda name: "C:/fake/vibectl.exe" if name == "vibectl" else None)
        argv = hooks._get_daemon_argv()

        assert "vibectl" not in " ".join(argv).lower()
        assert argv[-3:] == ["-m", "vibecodinglight", "daemon"]


class TestTransport:
    def test_wait_for_serial_uses_explicit_configured_port(self, monkeypatch):
        from vibecodinglight import transport

        opened = []

        class FakeSerial:
            is_open = True

        def fake_open_serial(port):
            opened.append(port)
            return FakeSerial()

        monkeypatch.setattr(transport, "open_serial", fake_open_serial)

        link = transport.wait_for_serial(0, {"serial_port": "COM9"})

        assert link.is_connected is True
        assert opened == ["COM9"]

    def test_wait_for_serial_auto_scans_detected_port(self, monkeypatch):
        from vibecodinglight import transport

        opened = []

        class FakeSerial:
            is_open = True

        monkeypatch.setattr(transport, "find_esp32_port", lambda: "COM7")
        monkeypatch.setattr(transport, "open_serial", lambda port: opened.append(port) or FakeSerial())

        link = transport.wait_for_serial(0, {"serial_port": "auto"})

        assert link.is_connected is True
        assert opened == ["COM7"]

    def test_serial_link_health_check_detects_closed_serial(self):
        from vibecodinglight.transport import SerialLink

        class FakeSerial:
            is_open = False

            @property
            def in_waiting(self):
                return 0

        link = SerialLink(FakeSerial())

        assert link.health_check() is False
