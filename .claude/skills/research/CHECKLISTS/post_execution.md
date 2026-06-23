# 执行后归档清单

## 输出完整性

- [ ] `04-final/{TOPIC}研究报告.md` 已保存
- [ ] `03-integrated/review_report.md` 已保存(若 reviewer 跑过)
- [ ] `01-plan/research_plan.md` 已保存
- [ ] 所有 `02-agents/Agent*.md` 已保存

## 客观指标核查

| 指标 | 要求(extreme) | 实际 | ✓/✗ |
|---|---|---|---|
| 总字数 | ≥ 20,000 | | |
| Mermaid 数 | ≥ 12 | | |
| 图表类型数 | ≥ 3 | | |
| 表格数 | ≥ 8 | | |
| 章节数 | 8-12 | | |
| "本章节小结" 次数 | = 章节数 | | |
| 每段 ≤ 300 字 | 95%+ | | |

## 归档操作(报告完成后)

使用 Bash 执行(不要用 PowerShell):

```bash
base="{{BASE_PATH}}/{PROJECT}"

# 移动子 Agent 输出和中间文件到归档
mv "$base/02-agents/"*.md "$base/05-archive/" 2>/dev/null
mv "$base/03-integrated/"*.md "$base/05-archive/" 2>/dev/null

# 写执行日志
cat > "$base/05-archive/execution_log.json" << EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "project": "{PROJECT}",
  "final_report": "$base/04-final/...",
  "word_count": N,
  "mermaid_count": N,
  "table_count": N,
  "chart_types": N,
  "review_score": X,
  "duration_minutes": N,
  "agent_count": N
}
EOF
```

## 永久保留

- `01-plan/research_plan.md`
- `04-final/*.md`

## 7 天后可清理

- `05-archive/*`(默认保留)
