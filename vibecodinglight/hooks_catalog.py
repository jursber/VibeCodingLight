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
    HookEntry("UserPromptSubmit",   True,  "thinking"),  # 用户输入 → 黄灯呼吸
    HookEntry("PreToolUse",         True,  "auto"),      # 调用工具 → 绿灯闪烁
    HookEntry("PermissionRequest",  True,  "alert"),     # 权限请求 → 红灯闪烁
    HookEntry("PostToolUse",        True,  "working"),   # 工具完成 → 绿灯常亮
    HookEntry("PostToolUseFailure", True,  "alert"),     # 工具失败 → 红灯闪烁
    HookEntry("PostToolBatch",      True,  "working"),   # 批量工具完成 → 绿灯常亮
    HookEntry("SubagentStart",      True,  "thinking"),  # 子代理启动 → 黄灯呼吸
    HookEntry("Stop",               True,  "idle"),      # 回合结束 → 红灯常亮
    HookEntry("StopFailure",        True,  "alert"),     # 回合失败 → 红灯闪烁
    HookEntry("SessionEnd",         True,  "off"),       # 会话结束 → 全灭
)

# ── Codex Hook 事件 ───────────────────────────────────────
CODEX_HOOKS: tuple[HookEntry, ...] = (
    HookEntry("SessionStart",       True,  None),       # 启动 daemon
    HookEntry("UserPromptSubmit",   True,  "thinking"),  # 黄灯呼吸
    HookEntry("PreToolUse",         True,  "auto"),      # 绿灯闪烁
    HookEntry("PermissionRequest",  True,  "alert"),     # 红灯闪烁
    HookEntry("PostToolUse",        True,  "working"),   # 绿灯常亮
    HookEntry("SubagentStart",      True,  "thinking"),  # 黄灯呼吸
    HookEntry("SubagentStop",       True,  "working"),   # 绿灯常亮
    HookEntry("Stop",               True,  "idle"),      # 红灯常亮
    HookEntry("PreCompact",         True,  "thinking"),  # 黄灯呼吸
    HookEntry("PostCompact",        True,  "idle"),      # 红灯常亮
    HookEntry("SessionEnd",         True,  "off"),       # 全灭
)


def hooks_for(agent: str) -> tuple[HookEntry, ...]:
    """获取某个 agent 的 hook 列表。"""
    if agent == "codex":
        return CODEX_HOOKS
    return CLAUDE_HOOKS
