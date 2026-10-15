# ai-memory

简体中文 · [English](README.en.md)

给 AI Agent 用的项目记忆与文档管理 Skill：创建项目说明、整理已有资料、保存进展，换个会话也能接着做。

## 让 Agent 安装并开始

把下面这段发给 Codex、Claude Code、Cursor 或其他支持 Skills 的 Agent：

```text
帮我安装这个 Skill：https://github.com/xobi667/ai-memory
然后使用 ai-memory，查看当前项目和已有文档，补齐必要的项目说明，
按用途整理文档，并记录当前进展和下一步。
```

Agent 会读取仓库中的技能说明，使用当前客户端的安装方式完成安装，再按你的需求处理项目文档。

## 直接说你要做什么

| 场景 | 发给 Agent 的话 |
|---|---|
| 创建文档 | 使用 ai-memory，为当前项目创建必要的说明文档。 |
| 整理资料 | 使用 ai-memory，读懂已有文档，按用途归类，并修复链接。 |
| 记录进展 | 使用 ai-memory，记录这次改动、验证结果和下一步。 |
| 换会话继续 | 使用 ai-memory，读取项目记录，核对当前状态，继续上次任务。 |
| 核对说明 | 使用 ai-memory，检查文档和实际实现是否一致，修正过时内容。 |

## 文档留在你的项目里

沿用已有目录和命名，不重建一套重复文档。短任务可以只留一份交接；需要多个任务和可追溯历史时，再使用结构化记录。

用途不明的 Markdown 文档可归入 `待分类/`，保留来源和待确认事项。记录写成普通 Markdown，后续 Agent 可以直接读取。

[Skill 入口](SKILL.md) · [文档整理](references/documents.md) · [记录与继续](references/lifecycle.md) · [反馈问题](https://github.com/xobi667/ai-memory/issues)

[LICENSE](LICENSE) · [署名与来源](NOTICE.md)
