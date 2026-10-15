# 可选 hooks

`scripts/memory_hook.py` 只读项目元数据，在会话事件中提供恢复入口和记录提醒。它不保存聊天、不提炼规则、不自动安装；普通使用无需启用 hooks。

## 配置

选择客户端，在项目配置里合并下面的 `hooks`，保留已有设置：

| 客户端 | 配置位置 | `<tool>` |
|---|---|---|
| Codex | `.codex/hooks.json`，或同层 `config.toml` 内联配置，二选一 | `codex` |
| Claude Code | `.claude/settings.local.json` | `claude` |

将 `<技能绝对路径>` 和 `<tool>` 替换为实际值；Python 命令也须在客户端环境可用。绝对路径可避免从子目录启动时找错脚本。

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear|compact|fork",
      "hooks": [{"type": "command", "command": "python \"<技能绝对路径>/scripts/memory_hook.py\" --tool <tool>", "timeout": 5}]
    }],
    "UserPromptSubmit": [{
      "hooks": [{"type": "command", "command": "python \"<技能绝对路径>/scripts/memory_hook.py\" --tool <tool>", "timeout": 5}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "python \"<技能绝对路径>/scripts/memory_hook.py\" --tool <tool>", "timeout": 5}]
    }]
  }
}
```

按客户端要求信任工作区及具体 hooks，用 `/hooks` 或日志确认加载。保持同步运行，不添加 `async`；不同时在多个配置层重复注册。接入后实际验证启动、恢复及压缩事件，旧版本或组织策略可能不支持这些事件。

### 指定任务与会话绑定

多个任务共用项目时，可在上面每条命令末尾加 `--task <TASK_ID>`，直接读取该任务的已确认范围；任务 ID 与 `scope --task` 使用的值相同。切换任务时同步修改此参数；它只选择恢复入口，不产生新授权。

宿主传入可靠的 `session_id` 时，SessionStart 输出会展示真实 ID 和绑定入口。运行 `bind --root <项目> --task <TASK_ID> --client codex --session <真实ID>`（Claude 使用 `--client claude`），后续会话事件自动使用同一绑定；不必修改每条 hook 配置。显式 `--task` 优先于绑定。某些宿主通过外部环境提供可靠会话 ID 时，也可显式传入 `--session`；不要在固定配置里给所有会话写同一个 ID。

选择顺序为显式任务、有效绑定、唯一活动任务。未提供会话 ID 时仍可显式选择；扫描完整、全部状态已知且恰好一个 active 任务时，才自动展示其目标。多个活动任务、未知状态、扫描受限或存在无效范围时只给恢复入口。绑定失效不会回退到其他任务，先核对再重新 `bind` 或 `unbind`。指定范围缺失、无效、未确认或属于其他项目时也不改选。

状态来自唯一 plan 的机器标记或明确字段映射；无 plan 的 small 任务可使用 scope 的 `task_status`。绑定和恢复不改变确认，也不自动重新激活暂停／完成任务。命令及映射见[工作流](workflow.md)。

## 事件行为

| 事件 | 输出 |
|---|---|
| `SessionStart`（含恢复、压缩） | `additionalContext`：项目入口及可确定的确认范围；不全量加载历史 |
| `UserPromptSubmit` | `additionalContext`：短记录提醒 |
| `Stop` | `systemMessage`：用户可见提醒；不阻止退出、不续写模型 |
| `PreCompact / PostCompact` | Codex 仅用户提示；Claude 返回 `{}`，不注入上下文 |

压缩后的模型恢复依赖 `SessionStart(compact)`。默认片段省略压缩前后提示以减少重复；需要时可用相同命令注册对应事件。`stop_hook_active=true` 返回 `{}`；不返回 block、continue=false 或退出码 2。

## 读取范围与诊断

- 输入为 stdin JSON，使用 `cwd`、`hook_event_name`、`stop_hook_active` 和可靠的 `session_id`。不读取 prompt、transcript 或日志正文；只为状态选择读取有界计划内容，不将其正文注入 hook 输出。
- 从绝对 cwd 向上找最近 `.ai-memory/project.json`，到 Git 边界停止。未登记项目输出 `{}`。
- 只读匹配项目身份、任务 ID 和确认声明的 scope、绑定和职责映射。入口来自 `.ai-memory/layout.json`，没有配置时用标准布局；无效映射明确诊断。cwd 或读取路径含 symlink/junction 时跳过，应使用真实目录。
- 每份元数据最多 32 KiB，计划状态读取最多 128 KiB；自动选择最多扫描 128 个目录条目，`--task` 或绑定直接读取指定文件，不受此扫描数量限制。超限旧 scope 保留路径供人工读取精简。stdout 默认最多 4096 UTF-8 字节，`--max-bytes` 可设 768–8192；工作中提醒另限 1536 字节。输出节选不改源文件。
- 无效输入或项目元数据读取失败时，stderr 给出诊断，stdout 返回 `{}`、退出 0；任务范围不可用时仍给出项目入口，不回退到其他任务。检查客户端日志，不能把降级提示当成恢复成功。

提醒不代表已经保存。关键进展仍须及时落盘；中断或崩溃不能依赖 Stop 兜底。

stdin/stdout 合约测试可以验证上述脚本行为，不能代替当前客户端版本、认证状态和实际压缩事件的验收。宿主未产生 `session_id` 时不承诺自动会话绑定。

协议参考：[Codex](https://learn.chatgpt.com/docs/hooks)、[Claude Code](https://code.claude.com/docs/en/hooks)。
