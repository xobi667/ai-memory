#!/usr/bin/env python3
"""建立可移植的项目记忆文档；仅使用 Python 标准库，不执行项目命令。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


KINDS = ("generic", "web", "android", "desktop", "library", "data", "content")
REQUIRED = ("AGENTS.md", "README.md", "DEV.md", "ARCHITECTURE.md", "docs/INDEX.md")
TASK_FILES = ("plan.md", "findings.md", "progress.md")
KIND_LABELS = {
    "generic": "通用项目", "web": "Web 项目", "android": "Android 项目",
    "desktop": "桌面应用", "library": "库 / SDK", "data": "数据项目", "content": "内容项目",
}
KIND_RECORDS = {
    "generic": "- 项目特有的约束与交付方式：待确认。",
    "web": "- 运行环境与配置来源路径：待确认。\n- 部署入口 / 发布流程路径：待确认。\n- 构建命令与构建输出目录：待确认。",
    "android": "- 版本名 / 版本码的配置来源路径：待确认。\n- 签名配置所在路径：待确认；只记录位置，不记录密码或私钥。\n- 构建命令与安装包输出目录：待确认。",
    "desktop": "- 目标操作系统：待确认。\n- 打包命令与打包配置路径：待确认。\n- 安装包 / 可执行产物位置：待确认。",
    "library": "- 公开 API 与兼容性约定的文档路径：待确认。\n- 构建 / 发布流程路径：待确认。\n- 包产物位置：待确认。",
    "data": "- 输入数据来源、格式与位置：待确认。\n- 输出数据 / 报告位置：待确认。\n- 可复现命令、环境与随机种子来源：待确认。",
    "content": "- 素材来源、授权依据与位置：待确认。\n- 内容格式与导出流程：待确认。\n- 成品位置：待确认。",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def project_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("项目根路径必须是目录。")
    return root


def safe_path(root: Path, target: Path, *, directory: bool = False) -> None:
    """在任何写入前检查全部路径；保守拒绝根目录以下的链接 / junction。"""
    if not inside(target, root):
        raise ValueError(f"路径不在项目根目录内：{target}")
    relative = target.relative_to(root)
    # 根目录可来自系统别名（如 macOS /var、Windows 短路径）。边界比较使用
    # 同一规范路径，但逐段检查保留调用者路径，不能先解析 target 而跳过子链接。
    canonical_root = root.resolve()
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if is_reparse(cursor):
            raise ValueError(f"目标路径含符号链接或 junction：{cursor}")
        if not inside(cursor.resolve(), canonical_root):
            raise ValueError(f"解析后的路径越出项目根目录：{cursor}")
        if cursor != target and cursor.exists() and not cursor.is_dir():
            raise ValueError(f"目标的父路径不是目录：{cursor}")
    if target.exists():
        if directory and not target.is_dir():
            raise ValueError(f"预期目录却遇到文件：{target}")
        if not directory and not target.is_file():
            raise ValueError(f"预期文件却遇到目录或特殊文件：{target}")


def atomic_create_text(root: Path, target: Path, content: str) -> None:
    """完整写入临时文件，再以硬链接排他发布；不会暴露写到一半的正式文件。"""
    safe_path(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, target)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ai-memory-", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(root, target)
        # 同目录发布，若别的写入者已创建目标则失败，绝不覆盖。
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def create_missing(root: Path, documents: dict[str, str]) -> dict[str, list[str]]:
    # 完成全部目标预检后才创建目录，避免遇到不安全路径时部分初始化。
    for relative in documents:
        safe_path(root, root / relative)
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    preserved: list[str] = []
    for relative, content in documents.items():
        target = root / relative
        safe_path(root, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_path(root, target)
        try:
            atomic_create_text(root, target, content)
        except FileExistsError:
            preserved.append(relative)
        else:
            created.append(relative)
    return {"created": created, "preserved": preserved}


def initialization_documents(name: str, kind: str, brief: str, tools: list[str]) -> dict[str, str]:
    name = " ".join(name.splitlines()).strip()
    brief = brief.strip()
    if not name or not brief:
        raise ValueError("项目名称和项目简述不能为空。")
    environment_record = "- 技术栈与依赖配置来源：待确认，初始化尚未识别或创建实现。"
    version_record = "- 当前版本的权威来源路径：待确认；未核实前不填写版本值，普通改动不自动升版。\n"
    version_rule = "- 改动不自动要求升版；版本从项目真实配置或发布记录核实。\n"
    verification_record = "- 构建、测试、运行与发布：均未执行。"
    if kind in ("data", "content"):
        version_record = ""
        version_rule = ""
        if kind == "data":
            environment_record = "- 数据处理工具、依赖与运行环境来源：待确认。"
            verification_record = "- 数据处理、可复现性检查与输出核验：均未执行。"
        else:
            environment_record = "- 编辑、生成和导出工具与环境来源：待确认。"
            verification_record = "- 素材来源核查、内容审阅与导出验证：均未执行。"
    documents = {
        "AGENTS.md": f"""# 项目接手规则

此文件约定读取和维护方式；实际任务范围与授权以用户当前要求为准。

## 开始或恢复任务

1. 阅读 [项目说明](README.md)、[当前状态](DEV.md)；涉及设计时阅读 [架构](ARCHITECTURE.md)。
2. 从 [文档索引](docs/INDEX.md) 和 DEV 中定位本次任务，仅按需阅读专题和历史。
3. 核对实际文件、工作区改动和相关配置；区分文档记录、亲自验证与待确认。若不一致，记录依据后修正文档，不凭猜测补事实。
4. 确认当前目标、已验证进展、未完成事项与具体下一步后继续；跨会话或上下文压缩后重复这一步。
5. 用户明确要求使用本技能记录当前任务时，可采用最低充分档位，按实际指令保存本任务范围；已有匹配的 `.ai-memory/scopes/` 确认记录直接沿用。仅在目标、范围或授权确有未知或需要扩大时询问；项目若明确要求每个任务先问则遵守。默认预设只用于建议，不是永久授权。未登记且未获当前任务记录授权时先只读盘点。

## 工作中保存

- 多阶段任务沿用已有活动计划；无既有计划时才使用 docs/tasks/<任务ID>/ 下的 plan.md、findings.md、progress.md，分别记录计划、关键发现和执行证据。由 DEV 和索引链接到实际任务，范围记录的 plan 指向同一来源。
- 在阶段完成、关键决定、失败尝试或方向变化后及时写回。不要只等结束或压缩前保存；磁盘上未记录的信息不能保证恢复。
- 在已确认的任务范围内自动保存：小改记录改动、验证和下一步；较大改动增加问题、踩坑、解决办法、设计取舍和影响范围。大改建立阶段计划；无新发现不编造经验。
- 使用 ai记忆时，按日期保存 Markdown 工作记录到 docs/memory/journal/；`.ai-memory/` 只保存项目标识和已确认范围等机器元数据。若已有任务框架，以其计划为主要来源，日志只记增量证据，不再复制一套进度。
- 长期规则候选不自动生效。新增或修改长期规则、精简、归档和删除前，先展示具体方案并得到确认；已授权的具体方案不重复询问。先保留原文备份，不按年龄自动删除。
- 决策记录说明原因、取舍与适用范围，按需放入 docs/decisions/ 并加入索引；目录和记录需要时再创建。
- 保持一项可变事实一个主要来源，其他入口引用它。README 管项目简介与配置来源；DEV 只保留项目级摘要和活动任务入口；架构记录当前设计并标明实施状态。
- 以 DEV / 范围记录登记的唯一活动计划作为任务当前阶段与下一步的主要来源；内置布局中是 plan.md。progress 或独立日志保存带时间的证据与交接快照。入口摘要或历史快照冲突时，结合实际证据校正当前计划，再更新引用处。
- 多个代理共享任务时指定一个文档汇总者，其他代理使用各自记录；涉及同一代码范围时先协调，必要时使用独立工作区。

## 结束或交接

- 更新任务记录与 DEV：改了什么、证据路径、哪些尚未验证、阻塞、下一条可执行动作。旧流水归入任务记录，不无限追加到 DEV。
- 分清“方案已写、执行已完成、相关验证通过、交付已确认”；无对应证据不写完成。文档检查不能代替与任务有关的实际验证。
- 不编造路径、命令、配置或环境。
{version_rule}- 不记录真实密码、令牌、私钥或签名口令，只记录安全的配置位置。初始化文档不代表用户授权部署或其他外部操作。
- 保持此入口简短；已有规则与用户要求冲突时先识别适用范围，不用模板覆盖项目实际约定。
- 本项目约定入口最多 8 KiB 且 100 行；超限先提出拆分与精简方案，经确认后处理，禁止静默截断。详细规则按模块保存并从入口链接；此预算不是工具的通用硬上限。
""",
        "README.md": f"""# {name}

## 项目简述

{brief}

## 项目资料

- 项目类型：{KIND_LABELS[kind]}（{kind}）。
{environment_record}
{version_record}- 主要产物位置：待确认；只有实际生成后才记录产物及验证证据。

## 本类型需要确认

{KIND_RECORDS[kind]}

## 接手入口

- [规则与工作流程](AGENTS.md)
- [当前状态、验证与下一步](DEV.md)
- [架构与设计状态](ARCHITECTURE.md)
- [完整文档索引](docs/INDEX.md)
""",
        "DEV.md": f"""# 当前状态

此文件只保留项目级摘要和活动任务入口；任务的阶段状态与具体下一步以登记的唯一活动计划为主要来源（内置布局为 plan.md）。

## 当前任务

- 本次仅初始化记忆文档；当前实现状况尚未核验。
- 正在进行的任务记录：尚未登记。创建任务后将其真实链接写在这里。

## 已确认与待确认

- 已确认：本文件作为当前工作入口。
- 待确认：现有代码、环境、工作区改动、运行方式及任务范围。
- 阻塞：尚未检查；不能据此断言没有阻塞。

## 验证

{verification_record}
- 文档初始化或文档检查通过不代表业务实现通过验证。

## 下一步

1. 读取项目说明和实际目录，核对实现与配置来源。
2. 按用户当前目标建立或定位任务记录，补充验证证据与下一条动作。
""",
        "ARCHITECTURE.md": """# 架构与设计状态

## 当前事实

初始化尚未检查现有实现；模块、数据流、接口和依赖均待核验。

## 建议设计（尚未实施）

根据实际项目规模补充模块职责、边界与依赖关系。这里是待补充的设计提示，不代表已有代码或确定的技术选型。

## 决策与证据

- 重要选择需要记录原因、替代方案与约束；按需建立 docs/decisions/。
- 每条设计注明“建议 / 已采用 / 已实施 / 待验证”，并链接对应代码或验证记录。
- 已存在的决策从 [文档索引](docs/INDEX.md) 查找。
""",
        "docs/INDEX.md": """# 文档索引

## 入口

- [接手规则](../AGENTS.md)
- [项目说明与配置来源](../README.md)
- [当前状态与下一步](../DEV.md)
- [架构与设计状态](../ARCHITECTURE.md)

## 任务记录

尚未登记。多阶段任务优先沿用已有计划；无既有计划时按需放在 docs/tasks/<任务ID>/。创建后在这里登记实际存在的活动计划链接，并在 DEV 中指向正在进行的任务。

## 决策与历史

尚未登记。重要设计原因按需放在 docs/decisions/；已结束任务可继续保留在 tasks 中并标记状态。只登记实际存在的文件。
""",
    }
    if "claude" in tools:
        documents["CLAUDE.md"] = "@AGENTS.md\n"
    return documents


def valid_task_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("任务 ID 必须为 1–80 位英文字母、数字、下划线或短横线，且以字母或数字开头。")
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value):
        raise ValueError("任务 ID 不能使用 Windows 保留设备名。")
    return value


def task_documents(task_id: str, goal: str, status_entry: str | None = "DEV.md") -> dict[str, str]:
    task_id = valid_task_id(task_id)
    goal = goal.strip()
    if not goal:
        raise ValueError("任务目标不能为空。")
    prefix = f"docs/tasks/{task_id}/"
    status_link = ("[当前项目状态](" + quote(os.path.relpath(status_entry, prefix).replace("\\", "/"), safe="/") + ")"
                   if status_entry else "已有项目入口")
    return {
        prefix + "plan.md": f"""# 任务：{task_id}

本文件的阶段状态和下一步是任务当前事实的主要来源。DEV 提供项目级摘要与入口，progress 保存带时间的历史快照；不以旧快照覆盖本文件。

## 目标

{goal}

## 当前状态

<!-- ai-memory-task-status: active -->

- 当前阶段：待开始。
- 实施范围和验收标准：待根据实际项目明确。
- 本记录创建时间（UTC）：{now()}

## 阶段

1. 核对目标、现状和约束：待开始。
2. 执行所需改动：待开始。
3. 运行相关验证、保存证据并更新交接：待开始。

## 下一步

阅读 {status_link}，核实目标涉及的实际文件，明确第一项具体动作。

## 任务资料

- [关键发现和决策](findings.md)
- [进展、验证和失败尝试](progress.md)
""",
        prefix + "findings.md": """# 关键发现与决策

## 已确认的事实

尚未记录。每项注明证据路径或来源，以及核验时间；推测与待确认事项单列。

## 决策及原因

尚未记录。说明取舍、约束和适用范围，必要时链接独立决策文档。

## 待确认

尚未核查；此状态不代表不存在问题。

[返回任务计划](plan.md)
""",
        prefix + "progress.md": f"""# 执行记录

本文件按实际时间保存操作与交接快照；快照只描述记录时的情况，不覆盖当前 plan.md 的阶段状态或下一步。

## 进展与验证证据

尚未开始。执行后按实际时间记录：动作、涉及文件、验证命令或方法、结果及证据路径。未执行的检查明确写“未执行”。

## 失败尝试

尚未记录。遇到失败后保存尝试、错误、原因与下次调整，避免重复无效操作。

## 交接快照（UTC：{now()}）

- 已完成：仅创建任务记录。
- 未完成：任务目标与实际验收。
- 记录时的下一步：按 [任务计划](plan.md) 核对现状并明确第一项动作；恢复时读取 plan 的最新内容。
""",
    }


def markdown_targets(text: str) -> list[str]:
    # 支持普通内联和引用式链接；不将代码示例中的链接当作导航。
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    text = re.sub(r"`[^`\n]*`", "", text)
    targets = re.findall(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^)\n]+)\)", text)
    targets += re.findall(r"(?m)^\s*\[[^\]\n]+\]:\s*(<[^>]+>|\S+)", text)
    normalized = []
    for target in targets:
        target = target.strip()
        if target.startswith("<"):
            target = target[1:target.index(">")]
        else:
            target = re.split(r"\s+[\"']", target, maxsplit=1)[0].strip()
        normalized.append(target)
    return normalized


def check_project(root: Path) -> dict[str, object]:
    if any(os.path.lexists(root / name) for name in (".ai-memory/layout.json", ".ai-memory/documents.json")):
        from memory_documents import check_documents
        return check_documents(root)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    paths = [root / relative for relative in REQUIRED]
    claude = root / "CLAUDE.md"
    if os.path.lexists(claude):
        paths.append(claude)
    tasks = root / "docs" / "tasks"
    try:
        safe_path(root, tasks, directory=True)
        if tasks.is_dir():
            for task in sorted(tasks.iterdir()):
                if is_reparse(task):
                    errors.append({"file": str(task.relative_to(root)), "reason": "任务目录含符号链接或 junction，未读取。"})
                elif task.is_dir():
                    # 只检查本工具的三种任务记录，不递归读取任意项目资料。
                    if any(os.path.lexists(task / filename) for filename in TASK_FILES) and not os.path.lexists(task / "plan.md"):
                        errors.append({"file": str((task / "plan.md").relative_to(root)), "reason": "任务记录不完整：缺少 plan.md，无法定位任务当前状态。"})
                    paths.extend(task / filename for filename in TASK_FILES if os.path.lexists(task / filename))
    except (OSError, ValueError) as exc:
        errors.append({"file": "docs/tasks", "reason": str(exc)})
    checked: list[str] = []
    task_plans: list[Path] = []
    entry_links: dict[str, set[Path]] = {"DEV.md": set(), "docs/INDEX.md": set()}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            safe_path(root, path)
            if not path.is_file():
                errors.append({"file": relative, "reason": "缺少必需入口文件。"})
                continue
            data = path.read_bytes()
            text = data.decode("utf-8-sig", errors="strict")
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append({"file": relative, "reason": str(exc)})
            continue
        checked.append(relative)
        if path.name == "plan.md" and path.parent.parent == tasks:
            task_plans.append(path)
        if relative == "AGENTS.md" and (len(data) > 8192 or len(text.splitlines()) > 100):
            warnings.append({"file": relative, "reason": "超过本方案 8 KiB / 100 行入口预算，需经确认后拆分或精简；不能静默截断原文。"})
        if relative == "DEV.md" and len(text.splitlines()) > 100:
            warnings.append({"file": relative, "reason": "超过建议的 100 行，建议将历史进展移入任务记录。"})
        targets = markdown_targets(text)
        if relative == "CLAUDE.md":
            targets += re.findall(r"(?m)^@([^\s]+)\s*$", text)
        for target in targets:
            if target.startswith("#") or target.startswith("//"):
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) and not re.match(r"^[A-Za-z]:[\\/]", target):
                continue
            try:
                local = unquote(urlsplit(target).path) if not re.match(r"^[A-Za-z]:[\\/]", target) else target
                if not local:
                    continue
                destination = Path(os.path.abspath(path.parent / local))
                safe_path(root, destination, directory=destination.is_dir())
                if not destination.exists():
                    raise ValueError("本地链接目标不存在。")
                if relative in entry_links:
                    entry_links[relative].add(destination.resolve())
            except (OSError, ValueError) as exc:
                errors.append({"file": relative, "link": target, "reason": str(exc)})
    for plan in task_plans:
        if plan not in entry_links["docs/INDEX.md"]:
            warnings.append({
                "file": "docs/INDEX.md", "code": "unregistered_task",
                "task": plan.relative_to(root).as_posix(),
                "reason": "此任务计划尚未登记到文档索引，新的 AI 可能无法从入口找到它；请添加真实链接。",
            })
    if task_plans and not set(task_plans).intersection(entry_links["DEV.md"]):
        warnings.append({
            "file": "DEV.md", "code": "no_active_task_link",
            "reason": "已有任务记录，但 DEV 未链接任何任务计划；请核实当前活动任务并添加链接。DEV 不需要列出全部历史任务。",
        })
    return {
        "command": "check", "root": str(root), "document_checks_passed": not errors,
        "checked_files": checked, "errors": errors, "warnings": warnings,
        "business_validation": "not_performed",
        "scope": "仅适用于本技能标准新项目布局：检查固定必需入口、已存在的生成文档 UTF-8、本地链接目标、任务登记和建议长度；不验证锚点、远程链接或业务事实。已有项目若采用不同命名入口，应由代理人工核验，不使用本命令的固定必需文件检查。",
        "note": "文档检查不代表代码、构建、测试、功能、发布或 AI 恢复能力已通过验证。",
    }


def register_task(root: Path, task_id: str) -> dict:
    """Called under project_lock; append navigation without replacing manual prose."""
    from memory_common import atomic_write
    from memory_documents import load_layout
    layout = load_layout(root)
    entries = list(dict.fromkeys([layout["roles"].get("status"), layout["index"]]))
    plan = root / f"docs/tasks/{task_id}/plan.md"
    start, end = "<!-- ai-memory:tasks:start -->", "<!-- ai-memory:tasks:end -->"
    changed, required, warnings, prepared = [], [], [], []
    for relative in entries:
        if not relative:
            continue
        path = root / relative
        safe_path(root, path)
        if not path.is_file():
            required.append(relative)
            continue
        before = path.read_bytes()
        text = before.decode("utf-8")
        newline = "\r\n" if b"\r\n" in before else "\n"
        target = quote(os.path.relpath(plan, path.parent).replace("\\", "/"), safe="/")
        line = f"- [{task_id}]({target})"
        if text.count(start) != text.count(end) or text.count(start) > 1:
            required.append(relative)
            warnings.append({"file": relative, "reason": "任务导航管理区块不完整，保留原文等待修复。"})
            continue
        if start in text:
            begin, finish = text.index(start) + len(start), text.index(end)
            if finish < begin:
                required.append(relative)
                continue
            body = text[begin:finish]
            if line in body.splitlines():
                continue
            after = text[:finish] + line + newline + text[finish:]
        else:
            after = text + ("" if text.endswith("\n") else newline) + newline + start + newline + line + newline + end + newline
        if relative == layout["roles"].get("status") and len(after.splitlines()) > 100:
            required.append(relative)
            warnings.append({"file": relative, "reason": "自动登记会超过状态入口 100 行预算，请整理后重试。"})
            continue
        prepared.append((relative, path, before, after.encode("utf-8")))
    for relative, path, before, after in prepared:
        safe_path(root, path)
        if path.read_bytes() != before:
            raise ValueError("任务入口在登记期间变化，请重试。")
        atomic_write(root, path, after, replace=True)
        changed.append(relative)
    return {"registered_entries": changed, "registration_required": required, "warnings": warnings}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="记忆：status（中文只读状态）、scope、record、resume、audit；工作流：bind、unbind、task-status、preset、handoff；文档核对：health、review-docs；整理：inspect、propose、propose-organize、apply-review、rollback-review。使用 <命令> --help 查看参数。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="只创建缺失的项目入口文档")
    init.add_argument("--root", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--kind", choices=KINDS, default="generic")
    init.add_argument("--brief", required=True)
    init.add_argument("--tools", choices=("codex", "claude", "cursor"), nargs="+", default=["codex", "claude", "cursor"])
    task = commands.add_parser("task", help="只创建缺失的任务记录")
    task.add_argument("--root", required=True)
    task.add_argument("--id", required=True)
    task.add_argument("--goal", required=True)
    check = commands.add_parser("check", help="只读文档结构检查；不运行项目验证")
    check.add_argument("--root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "status":
        from memory_status import main as status_main
        return status_main(arguments)
    if arguments and arguments[0] in {"scope", "record", "resume", "audit"}:
        from memory_store import main as memory_main
        return memory_main(arguments)
    if arguments and arguments[0] in {"inspect", "propose", "propose-organize", "apply-review", "rollback-review"}:
        from memory_admin import main as admin_main
        return admin_main(arguments)
    if arguments and arguments[0] in {"bind", "unbind", "task-status", "preset", "handoff"}:
        from memory_workflow import main as workflow_main
        return workflow_main(arguments)
    if arguments and arguments[0] in {"health", "review-docs"}:
        from memory_health import main as health_main
        return health_main(arguments)
    args = make_parser().parse_args(arguments)
    try:
        root = project_root(args.root)
        if args.command == "check":
            result = check_project(root)
            exit_code = 0 if result["document_checks_passed"] else 1
        elif args.command == "init":
            documents = initialization_documents(args.name, args.kind, args.brief, args.tools)
            for relative in documents:
                safe_path(root, root / relative)
            from memory_common import project_lock
            root.mkdir(parents=True, exist_ok=True)
            with project_lock(root):
                created = create_missing(root, documents)
            result = {"command": "init", "root": str(root), **created,
                      "note": "仅创建缺失文档；现有文件按字节保留。需要代理核对真实项目并补充待确认项。"}
            exit_code = 0
        else:
            from memory_documents import load_layout
            layout = load_layout(root)
            documents = task_documents(args.id, args.goal, layout["roles"].get("status"))
            plan_path = root / f"docs/tasks/{args.id}/plan.md"
            safe_path(root, plan_path)
            from memory_common import project_lock
            root.mkdir(parents=True, exist_ok=True)
            with project_lock(root):
                safe_path(root, plan_path)
                if plan_path.is_file():
                    existing = plan_path.read_text(encoding="utf-8-sig")
                    match = re.search(r"(?ms)^## 目标\s*\n(.*?)(?=^## |\Z)", existing)
                    if not match or match.group(1).strip() != args.goal.strip():
                        raise ValueError("该任务 ID 已存在且目标不同或已被重新组织；请读取原计划继续，或用新 ID 建立新任务。原文未改动。")
                created = create_missing(root, documents)
                registration = register_task(root, args.id)
            result = {"command": "task", "root": str(root), **created, **registration,
                      "task_plan": f"docs/tasks/{args.id}/plan.md",
                      "note": "必要入口仅更新 ai-memory 管理区块；人工内容保留。当前状态由活动计划维护。"}
            exit_code = 0
    except (OSError, ValueError) as exc:
        result = {"command": args.command, "error": str(exc), "business_validation": "not_performed"}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
