# 执行前检查清单(主 Agent 启动 Phase 2 前自检)

- [ ] 已读 `~/.claude/skills/research/CONFIG.yaml`
- [ ] 已识别主题:`{topic}`
- [ ] 已判定模式:`{minimal/enhanced/extreme}`
- [ ] 已选定角色 ID 集合:`{[1, 2, 3, ...]}`(对应模式 + 动态扩展)
- [ ] 项目目录已创建:`{{BASE_PATH}}/{PROJECT}/`
  - [ ] 01-plan
  - [ ] 02-agents
  - [ ] 03-integrated
  - [ ] 04-final
  - [ ] 05-archive
- [ ] `01-plan/research_plan.md` 已写入
- [ ] 准备好同条消息内并发 N 个 Task(`subagent_type: research-researcher`)

## 准备并发调用的清单

每个 Task 调用必须准备好以下变量:

```text
ID            = 1, 2, 3, ...
ROLE_NAME     = 市场测算师 / 竞争情报员 / ...
RESPONSIBILITY = 简述
KEYWORDS      = 关键词列表(主题派生 + 角色固定关键词)
TOPIC         = 用户主题
MODE          = minimal / enhanced / extreme
PROJECT_PATH  = {{BASE_PATH}}/{project}
OUTPUT_PATH   = {PROJECT_PATH}/02-agents/Agent{ID}_{ROLE_NAME}.md
MIN_SEARCH    = 5 (minimal) / 10 (enhanced) / 20 (extreme)
```

## 风险提示

- ❌ **不要分多条消息发 Task**(会变串行)
- ❌ 不要让 researcher 自己读 CONFIG.yaml(增加 token 浪费)
- ❌ 不要使用相对路径
