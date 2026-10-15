# 长期规则、精简与归档

日常事实在已授权任务范围内自动记录。新增长期约束、修改规则或整理历史，先准备具体提案；已有明确授权覆盖时直接执行，否则再确认。授权仅覆盖相应内容，不扩展为永久免确认。

## 长期规则

经验先记入 `long_term_candidates`，附证据及适用范围，不能自动变成永久约束。规则模板：

```markdown
# RULE-001：发布前维护交接记录

- 状态：拟议，待确认。
- 范围：本项目正式发布。
- 规则：发布前更新变更、已验证/未验证、已知问题与下一步；版本取自真实构建配置。
- 依据：事件或用户要求的链接。
- 确认：实际答复及时间，确认后填写。
- 替代关系：无；修订时链接被替代版本。
```

按需补负责人和复查条件。确认后的短规则可放 AGENTS，详细规则放 `docs/memory/rules/` 并从适用入口链接，核实接手者能找到。

每次规则提案说明“采用 / 修改 / 拒绝 / 暂缓”哪些候选、对应证据、与现有规则的冲突及被替代规则去向。状态变化保留确认摘要，过期规则明确标为失效或被替代；不因重复出现次数自动晋升为规则。

## 入口预算

AGENTS 同时不超过 **8 KiB UTF-8 字节和 100 行**，DEV 不超过 **100 行**。超限时提出保留、移出和被替代内容清单；入口保留关键约束及模块路由，细节按需读。

不得截断末尾或悄悄遗漏规则，也不要拆成多个文件后全部导入。无法保留必要约束时，说明冲突并协商预算或职责拆分。

## 准备与应用提案

项目须已有 `.ai-memory/project.json`。由 AI 准备拟议全文；`propose` 只生成提案，不改源文件：

```text
python "<技能目录>/scripts/ai_memory.py" propose --root "<项目>" --id "rule-001" --kind rule --source "docs/memory/rules/release.md" --replacement-file "proposed-rule.md" --reason "固化已确认的发布记录约定"
python "<技能目录>/scripts/ai_memory.py" propose --root "<项目>" --id "trim-agents" --kind compact --source "AGENTS.md" --replacement-file "short-agents.md" --reason "入口超出预算，保留关键约束并移出历史"
python "<技能目录>/scripts/ai_memory.py" propose --root "<项目>" --id "archive-old-note" --kind archive --source "docs/old-note.md" --reason "已被新设计替代，保留追溯入口"
```

展示实际文件、前后变化、备份位置和影响；精简须说明关键规则的去向。让用户选择应用、修改提案或保留原文。已有明确授权覆盖该提案时沿用，否则等待答复后应用：

```text
python "<技能目录>/scripts/ai_memory.py" apply-review --root "<项目>" --id "trim-agents" --confirmed --confirmation "用户确认采用展示的精简提案，保留关键约束及历史备份。"
```

`--confirmed` 是调用者声明，不能代替真实确认。项目身份、源内容或提案校验变化时拒绝旧提案，须重新审阅。

已有文件先备份至 `docs/memory/archive/YYYY-MM-DD/<review-id>/`，再原子替换。归档留下链接占位；原文标为历史，不再作为现行规则。AGENTS、DEV 最终稿连同历史链接须满足预算。

## 日志与删除

`docs/memory/journal/` 的受管理日志只追加，不用普通 `archive` 移动或替换。通过逻辑归档减少默认恢复的历史：

```text
python "<技能目录>/scripts/ai_memory.py" propose --root "<项目>" --id "journal-old" --kind journal-archive --task "old-task" --reason "该任务已交接，默认恢复不再加载旧记录"
python "<技能目录>/scripts/ai_memory.py" apply-review --root "<项目>" --id "journal-old" --confirmed --confirmation "用户确认所列事件集合"
python "<技能目录>/scripts/ai_memory.py" resume --root "<项目>" --task "old-task" --include-archived
```

也可重复 `--event <ID>` 指定集合。提案冻结当时的明确事件，不自动归档后续记录，不改变任务状态、不减少磁盘占用。原文路径和字节不变，更正/替代关系继续参与恢复；反向操作用 `--kind journal-unarchive` 新建提案。清单版本或原文变化时拒绝旧提案。中断后可重试同一 `apply-review` 完成回执。

散落文档整理另见[文档整理](documents.md)：真实阅读、用途清单、中文待分类、批量修复引用和可恢复回滚。

不按天数自动删除。明确删除需求时，先列出路径、引用、备份和影响，确认覆盖这些文件后执行；不要连同备份与当前规则一并清掉。

应用后检查入口预算、链接、规则状态和备份，并按实际任务演练恢复。结构校验不能证明精简语义无损，必须核对关键约束。
