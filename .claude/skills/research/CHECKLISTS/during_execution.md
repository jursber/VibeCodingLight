# 执行中监控清单

## researcher 阶段(Phase 2)

主 Agent 在 Task 调用全部返回后检查:

- [ ] 02-agents/ 下生成的文件数 = 启动数(用 `Glob` 验证)
- [ ] 每个文件 ≥ 1 KB(失败的会很小或不存在)
- [ ] 失败数:_______ / N

**如失败数 > 2** → 跳过 optimizer,直接执行补偿(主 Agent 自己整合)

## optimizer 阶段(Phase 3)

- [ ] `03-integrated\structure_plan.md` 已生成(Phase 1 产物)
- [ ] `03-integrated\core_chapters.md` 已生成(Phase 2 产物)
- [ ] `04-final\{TOPIC}研究报告.md` 已生成(Phase 3 产物)
- [ ] 最终报告 ≥ 10 KB(否则视为失败)

**如最终报告不存在或 < 10 KB** → 触发补偿

## reviewer 阶段(Phase 4)

- [ ] `03-integrated\review_report.md` 已生成
- [ ] 总分:______ / 100
- [ ] 决策:{通过 / 通过(人工) / 返工 / 失败}

## 补偿触发判定

| 条件 | 是否满足 | 动作 |
|---|---|---|
| optimizer 总耗时 > 1200s | | 触发补偿 |
| optimizer 输出 < 10 KB | | 触发补偿 |
| optimizer Phase 2 > 600s | | 触发补偿 |
| researcher 失败数 > 2 | | 触发补偿 |
