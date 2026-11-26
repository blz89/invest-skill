# §10 编排框架（摘要）

> 完整说明（角色/流程/契约/命令/计费/评分/网格/反思）以 `references/team-framework.md` 为准，`SKILL.md` §10 只留指针。

**三条要点**：

1. **第一原则**：可重复执行的动作写代码，只判断一次的才交 LLM。token 实花以 `out/budget.json` 的 `spent_cny` 为准（净利润/净收益率与验收线见 `references/paper-ops.md` §8.4）。
2. **LLM 步骤必须真起执行者**：`team.py --run` 只跑代码并出任务书；T2/T3/T4/T9/T10 由 Lead 起 fresh 子代理并逐段 `--ingest` 验收（文件/表头/枚举/深取齐全性/复盘两节），T9 默认 Lead 亲自写。子代理只写约定产物、不改 config/账本/脚本。
3. **月报不只统计**：T10 的 `out/team/retro.md` 必须含「策略复盘」「技能优化」两节（缺节 `--ingest` fail-closed）；A 类由 Lead 当场执行并 git 提交，B 类写 `out/team/pending_changes.md` 等拍板。
