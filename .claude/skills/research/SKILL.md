---
name: research
description: 多 Agent 协作生成图文并茂的深度行业研究报告。当用户提出"行业研究"、"研究报告"、"深度研究"、"市场分析"等需求,或显式调用 /research 时使用。支持 minimal(3 agents/8000字)、enhanced(6 agents/15000字)、extreme(8-12 agents/20000-30000字)三种模式,自动并行搜索、统稿、审核,产出含 Mermaid 图表与表格的图文并茂报告。
user-invocable: true
version: 3.2.0-cc
---

# Research v3.2.0-cc — Claude Code 适配版

通用深度研究报告生成系统。**主 Agent (Claude) 充当 Orchestrator**,通过 cc 的 `Task` 工具并行调度自定义子代理 (`research-researcher` / `research-optimizer` / `research-reviewer`),完成「搜索 → 统稿 → 审核」全流程。

---

## 触发与启动

**用户输入示例**:
- `/research 储能行业 2026 投资机会`
- `深度研究 AI 芯片行业,需要学术级报告`
- `简要分析锂电池市场`

**模式自动判定**:

| 关键词 | 模式 | Agent 数 | 字数下限 |
|---|---|---|---|
| "极致" / "深度" / "全面" / "学术" | **extreme** | 8-12 | 20,000 |
| "简要" / "快速" / "概览" | **minimal** | 3 | 8,000 |
| 默认 | **enhanced** | 6 | 15,000 |

如用户未明确,**主动询问一次**:"研究深度选 minimal/enhanced/extreme,默认 enhanced 可以吗?"

---

## 执行流程(Orchestrator 视角)

详细步骤见 `PROMPTS/orchestrator.md`,流程图见 `WORKFLOW.md`。

### Step 1:确认主题 + 模式
### Step 2:读 CONFIG.yaml
### Step 3:创建项目目录(Bash,非 PowerShell)
### Step 4:并发启动 researcher(同消息 N 个 Task)
### Step 5:汇总 + 检查(记录失败 Agent)
### Step 6:启动 optimizer(附缺失角色清单)
### Step 6.5:Mermaid 语法静态检查(Grep,非 Playwright)
### Step 7:补偿(失败时,仍送审)
### Step 8:启动 reviewer(自身失败时跳过审核)
### Step 9:归档清理

---

## 并行 researcher(关键)

在主 Agent 的**同一条响应**内,发出 N 个 `Task` 调用(`subagent_type: research-researcher`)。

```text
assistant 消息(单条):
  Task #1 → researcher (Agent1 市场测算师)
  Task #2 → researcher (Agent2 竞争情报员)
  ...
  Task #N → researcher (AgentN 附录编纂者)
↓ 并发执行,每个 researcher 独立搜索 + 写文件
↓ 全部返回后,主 Agent 收集结果
```

**每个 prompt 必须自包含**(见 PROMPTS/subagent.md)。

---

## 图表规范

### 推荐图表类型

| 场景 | 推荐类型 |
|---|---|
| 流程/逻辑 | flowchart |
| 时间演进 | timeline |
| 框架/分类 | mindmap |
| 占比 | pie |
| 关系/依赖 | graph |
| 项目计划 | gantt |

**至少使用 2 种不同类型**(全报告 ≥ 3 种)。

### 禁止使用

- **quadrantChart**:在 Mermaid v10 中对中文兼容性极差(轴标签/象限名/数据点均需 ASCII),禁止使用。需要二维对比时改用 Markdown 表格或 graph。

### 图表分析风格

**三要素**:数据含义(30 字) + 趋势洞察(30 字) + 实操启示(30 字) = 80-150 字。

**禁止简单重复图表数据**。详见 `CONFIG.yaml` 的 `chart_analysis_style` 段。

### Mermaid 语法规则

| 规则 | 说明 |
|---|---|
| flowchart 节点内禁止半角括号 | `()` 在 `[]`/`{}` 内会解析失败 |
| quadrantChart 禁止使用 | 中文环境不可用 |
| timeline 多事件用分号 | 同一时间点多个事件用 `;` 分隔 |
| 边标签禁用 `<br/>` | `\|...\|` 内使用 `<br/>` 可能解析失败 |

---

## 硬指标

| 指标 | extreme | enhanced | minimal |
|---|---|---|---|
| 字数 | ≥ 20,000 | ≥ 15,000 | ≥ 8,000 |
| Mermaid | ≥ 12 | ≥ 8 | ≥ 5 |
| 表格 | ≥ 8 | ≥ 5 | ≥ 3 |
| 图表类型 | ≥ 3 种 | ≥ 3 种 | ≥ 2 种 |
| 段落 ≤ 300 字 | ✓ | ✓ | ✓ |
| 每章导语 + 小结 | ✓ | ✓ | ✓ |

---

## Task 工具调用清单

| 阶段 | 调用 | 并发数 | 说明 |
|---|---|---|---|
| Step 4 | research-researcher × N | N(同消息) | 并行搜索 |
| Step 6 | research-optimizer × 1 | 1 | 统稿 |
| Step 8 | research-reviewer × 1 | 1 | 审核(失败时跳过) |

---

## 关键文件导航

| 文件 | 用途 |
|---|---|
| `CONFIG.yaml` | 唯一定义源:路径、硬指标、风格、模板、审核配置 |
| `WORKFLOW.md` | 流程图、异常处理 |
| `PROMPTS/orchestrator.md` | 主 Agent 编排提示词 |
| `PROMPTS/subagent.md` | researcher prompt 模板 |
| `PROMPTS/structure_optimizer.md` | optimizer prompt |
| `PROMPTS/industry_reviewer.md` | reviewer prompt |
| `TEMPLATES/final_report.md` | 终稿格式参考 |
| `CHECKLISTS/*.md` | 各阶段自检清单 |

---

## 自定义子代理(预置)

| 子代理名 | 角色 |
|---|---|
| `research-researcher` | 通用搜索 + 分析 + 出图(8 角色复用) |
| `research-optimizer` | 长文统稿、图文并茂重构 |
| `research-reviewer` | 8 维度评分审核 |

**模型策略**:三个子代理均设置 `model: inherit`,继承当前会话模型。

---

**核心原则**:**图文并茂、清晰易读、观点清晰**。图表 ≥ 12,表格 ≥ 8,每图表 80-150 字分析(数据含义+趋势洞察+实操启示),每章带导语 + 小结。
