"""
Claude Code / Codex Hook 事件目录。

每个 HookCatalogEntry 包含：
  - event: 事件名
  - wired: 是否默认接线（安装 hooks 时写入配置）
  - state: 写入的状态名（thinking/working/alert/idle/off/None）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HookEntry:
    event: str
    wired: bool
    state: str | None  # None = daemon 启动等特殊用途


# ── Claude Code Hook 事件 ─────────────────────────────────
# 只保留实际需要接线的事件，不接线的不列入
CLAUDE_HOOKS: tuple[HookEntry, ...] = (
    HookEntry("SessionStart",       True,  None),       # 启动 daemon
    HookEntry("UserPromptSubmit",   True,  "thinking"),  # 用户输入 → 思考
    HookEntry("PreToolUse",         True,  "auto"),      # 工具调用前 → auto 判断
    HookEntry("PermissionRequest",  True,  "alert"),     # 权限请求 → alert
    HookEntry("PostToolUse",        True,  "thinking"),  # 工具完成 → 思考
    HookEntry("PostToolUseFailure", True,  "alert"),     # 工具失败 → alert
    HookEntry("PostToolBatch",      True,  "thinking"),  # 批量工具完成 → 思考
    HookEntry("SubagentStart",      True,  "thinking"),  # 子代理启动 → 思考
    HookEntry("Stop",               True,  "idle"),      # 回合结束 → idle
    HookEntry("StopFailure",        True,  "alert"),     # 回合失败 → alert
    HookEntry("SessionEnd",         True,  "off"),       # 会话结束 → 灯灭
)

# ── Codex Hook 事件 ───────────────────────────────────────
CODEX_HOOKS: tuple[HookEntry, ...] = (
    HookEntry("SessionStart",       True,  None),       # 启动 daemon
    HookEntry("UserPromptSubmit",   True,  "thinking"),
    HookEntry("PreToolUse",         True,  "auto"),
    HookEntry("PermissionRequest",  True,  "alert"),
    HookEntry("PostToolUse",        True,  "thinking"),
    HookEntry("SubagentStart",      True,  "thinking"),
    HookEntry("SubagentStop",       True,  "working"),   # Codex 子代理结束 → 工作
    HookEntry("Stop",               True,  "idle"),
    HookEntry("PreCompact",         True,  "thinking"),
    HookEntry("PostCompact",        True,  "idle"),
    HookEntry("SessionEnd",         True,  "off"),
)


def hooks_for(agent: str) -> tuple[HookEntry, ...]:
    """获取某个 agent 的 hook 列表。"""
    if agent == "codex":
        return CODEX_HOOKS
    return CLAUDE_HOOKS
