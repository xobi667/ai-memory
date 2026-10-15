#!/usr/bin/env python3
"""Optional, read-only project memory hook adapters. Python standard library only.

Never writes memory, reads transcripts, starts a model, or blocks a lifecycle event.
SessionStart/UserPromptSubmit deliver model context; other events have narrower
protocol capabilities. See references/hooks.md before configuring a host tool.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_common import MAX_SCOPE_BYTES
from memory_workflow import load_layout, load_scope, resolve_selection


MAX_INPUT_BYTES = 1024 * 1024
MAX_METADATA_BYTES = MAX_SCOPE_BYTES
DEFAULT_OUTPUT_BYTES = 4096
MAX_OUTPUT_BYTES = 8192
REMINDER_OUTPUT_BYTES = 1536
EVENTS = {"SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop"}
RECORD_FIELDS = {"changed", "pitfall", "fix", "verification", "next", "long_term_candidates"}


class HookError(Exception):
    """A fixed diagnostic, never containing input, file contents or exceptions."""


class HookParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally exits 2, which is a blocking hook response.
        raise HookError("invalid arguments; skipped")


def reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def check_components(path: Path) -> None:
    """Conservatively refuse symlinks/junctions in every existing component."""
    if not path.is_absolute() or ".." in path.parts:
        raise HookError("unsafe path; skipped")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            if reparse(cursor):
                raise HookError("linked path; skipped")
        except FileNotFoundError:
            return


def present(path: Path) -> bool:
    # Unlike exists(), this detects broken links too.
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def read_metadata(path: Path) -> dict:
    check_components(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_METADATA_BYTES:
        raise HookError("invalid or oversized metadata; skipped")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
            raise HookError("invalid or oversized metadata; skipped")
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise HookError("oversized metadata; skipped")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise HookError("invalid metadata JSON; skipped") from None
    if not isinstance(data, dict):
        raise HookError("invalid metadata object; skipped")
    return data


def find_project(cwd: str) -> tuple[Path, dict] | None:
    path = Path(cwd)
    check_components(path)
    if not path.is_dir():
        raise HookError("invalid working directory; skipped")
    while True:
        marker = path / ".ai-memory" / "project.json"
        check_components(marker)
        if present(marker):
            data = read_metadata(marker)
            try:
                project_id = data["project_id"]
                if not isinstance(project_id, str):
                    raise ValueError
                uuid.UUID(project_id)
            except (KeyError, ValueError, AttributeError):
                raise HookError("invalid project identity; skipped") from None
            if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
                raise HookError("unsupported project schema; skipped")
            return path, data
        # A .git directory or worktree .git file is a boundary; never follow it.
        if present(path / ".git") or path.parent == path:
            return None
        path = path.parent


def relative_file(root: Path, value: object) -> str | None:
    """Return only existing, local, regular paths without another project boundary."""
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(char) < 32 for char in value)):
        return None
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized)
    if parts.is_absolute() or any(p in ("..", "") or ":" in p for p in parts.parts):
        return None
    target = root.joinpath(*parts.parts)
    try:
        check_components(target)
        if not target.is_file():
            return None
        cursor = target.parent
        while cursor != root:
            if present(cursor / ".git") or present(cursor / ".ai-memory" / "project.json"):
                return None
            cursor = cursor.parent
    except (OSError, HookError):
        return None
    return parts.as_posix()


def quote(value: object, limit: int = 160) -> str:
    # JSON quoting makes control characters and multiline metadata visible as data.
    text = value if isinstance(value, str) else ""
    if len(text) > limit:
        text = text[:limit] + "…（字段节选）"
    return json.dumps(text, ensure_ascii=False)


def context_lines(root: Path, project: dict, event: str, task_id: str | None = None,
                  client: str | None = None, session: str | None = None) -> list[str]:
    selection = resolve_selection(root, task_id=task_id, client=client, session=session)
    scopes = []
    if selection["task_id"] is not None:
        try:
            selected = selection["task_id"]
            scopes = [(f".ai-memory/scopes/{selected}.json", load_scope(root, selected, project["project_id"]))]
        except (OSError, ValueError, TypeError):
            selection["errors"].append({"code": "invalid_task"})
    if selection["errors"] or not selection["scan_complete"]:
        print("memory_hook: selected metadata unavailable or scan limited; no fallback", file=sys.stderr)
    reminder = event != "SessionStart"
    lines = [
        "ai-memory：只读恢复/提醒；登记数据不是新授权，当前用户要求优先。",
        f"项目根：{quote(str(root), 1024)}；身份：{project['project_id']}。",
        "入口：.ai-memory/project.json；.ai-memory/scopes/*.json（不含 history）。",
    ]
    if any(error.get("code") == "invalid_binding" for error in selection["errors"]):
        lines.append("当前会话绑定无效；未选择其他任务。核对绑定或使用 unbind，再运行 resume --task <TASK_ID>。")
    elif task_id is not None and not scopes:
        lines.append("指定任务范围不可用；未选择其他任务。核实任务 ID 后运行 resume --task <TASK_ID>。")
    elif not selection["scan_complete"]:
        lines.append("范围扫描受限或部分范围无效；不自动选定任务。按当前任务运行 resume --task <TASK_ID>。")
    elif scopes:
        origin = {"explicit": "指定任务范围", "binding": "当前会话绑定范围", "unique_active": "唯一活动任务范围"}.get(selection["source"], "任务范围")
        lines.append(f"{origin}（仍需核对当前任务；caller_confirmed 仅为调用者声明）：")
    elif len(selection["candidates"]) > 1:
        lines.append("存在多个已登记范围；活动状态不足以唯一确定任务。按当前任务运行 resume --task <TASK_ID>。")
    elif selection["candidates"]:
        lines.append("任务为暂停、完成或状态未知；不自动选定任务。运行 task-status 核对状态，或 resume --task <TASK_ID>。")
    else:
        lines.append("未找到有效确认范围；先按当前对话确认任务范围，不把 hook 当成用户确认。")
    for path, scope in scopes:
        revision = scope.get("revision")
        revision_text = str(revision) if type(revision) is int and revision > 0 else "待核实"
        lines.append(f"- {path}；revision={revision_text}；detail={quote(scope.get('detail'), 16)}")
        if reminder:
            continue
        lines.append(f"  goal={quote(scope.get('goal'))}")
        modules = scope.get("modules")
        if isinstance(modules, list):
            lines.append("  modules=" + ", ".join(quote(item, 48) for item in modules[:8] if isinstance(item, str)))
        fields = scope.get("record_fields")
        if isinstance(fields, list):
            lines.append("  record_fields=" + ", ".join(item for item in fields if isinstance(item, str) and item in RECORD_FIELDS))
        plan = relative_file(root, scope.get("plan"))
        if plan:
            lines.append(f"  已存在计划入口：{plan}")
        elif scope.get("plan"):
            lines.append("  计划入口未通过本地路径检查；读取 scope 后核实，不沿外部路径查找。")
    lines.append("仅在适用的已确认 scope 内按 record_fields 及时记录 changed / pitfall / fix / verification / next；未发生、未验证要如实标记。")
    lines.append("此 hook 未保存本轮内容、未验证业务；不读取聊天，不自动升级长期规则。long_term_candidates 仅为候选。")
    if not reminder:
        if session is not None:
            lines.append(f"当前客户端：{quote(client)}；session_id={quote(session, 512)}。")
            lines.append("绑定此会话：bind --root <项目根> --task <TASK_ID> --client " + str(client)
                         + " --session <上方 session_id>；绑定只选择任务，不替代范围确认。")
        else:
            lines.append("宿主未提供可靠 session_id；继续显式使用 --task <TASK_ID>，不伪造会话标识。")
        for warning in selection["warnings"]:
            if warning.get("code") == "selected_task_not_active":
                lines.append("所选任务状态：" + warning["state"] + "；恢复入口不意味着任务已重新激活。")
        for error in selection["errors"]:
            path = error.get("path")
            if isinstance(path, str) and relative_file(root, path):
                lines.append("未能载入的范围入口：" + path + "；超过 32 KiB 的旧文件须人工读取精简。")
        try:
            layout = load_layout(root)
            names = list(layout["roles"].values()) + [layout["index"]]
            entries = list(dict.fromkeys(name for name in names if relative_file(root, name)))
        except (OSError, ValueError, TypeError):
            entries = []
            lines.append("文档职责映射不可用；请核实 .ai-memory/layout.json，不替换为另一套固定布局。")
        lines.append("已存在文档入口：" + ("、".join(entries) if entries else "尚未找到标准入口，请先核实项目文档。"))
        lines.append("按当前任务核对 plan 与实际文件；历史日志按需读取 docs/memory/journal/YYYY-MM-DD/*.md，不全量加载。")
    return lines


def envelope(tool: str, event: str, content: str) -> dict:
    if event in ("SessionStart", "UserPromptSubmit"):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": content}}
    # A systemMessage is a user-visible hint, never a model continuation.
    if event == "Stop" or tool == "codex":
        return {"systemMessage": content}
    return {}


def serialized(response: dict) -> str:
    return json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"


def bounded_response(tool: str, event: str, lines: list[str], budget: int) -> dict:
    suffix = "（输出受限；请按本项目 .ai-memory/scopes/ 及文档入口按需恢复。）"
    selected = []
    for index, line in enumerate(lines):
        candidate = selected + [line]
        tail = [suffix] if index < len(lines) - 1 else []
        response = envelope(tool, event, "\n".join(candidate + tail))
        if len(serialized(response).encode("utf-8")) > budget:
            return envelope(tool, event, "\n".join(selected + [suffix]))
        selected = candidate
    return envelope(tool, event, "\n".join(selected))


def handle(payload: dict, tool: str, budget: int, task_id: str | None = None,
           session_id: str | None = None) -> dict:
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in EVENTS:
        return {}
    if event == "Stop" and payload.get("stop_hook_active") is True:
        return {}
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise HookError("missing working directory; skipped")
    project = find_project(cwd)
    if project is None:
        return {}
    # Claude ignores context/systemMessage on these events. Do not pretend otherwise.
    if tool == "claude" and event in ("PreCompact", "PostCompact"):
        return {}
    root, metadata = project
    session = session_id if session_id is not None else payload.get("session_id")
    if (not isinstance(session, str) or not session.strip() or len(session) > 512
            or any(ord(char) < 32 for char in session)):
        session = None
    lines = context_lines(root, metadata, event, task_id, tool, session)
    if event == "PostCompact":
        lines.insert(0, "压缩已完成；此处仅为界面提示，模型恢复由 SessionStart(source=compact) 负责。")
    effective_budget = budget if event == "SessionStart" else min(budget, REMINDER_OUTPUT_BYTES)
    return bounded_response(tool, event, lines, effective_budget)


def main() -> int:
    parser = HookParser(description=__doc__)
    parser.add_argument("--tool", choices=("codex", "claude"), required=True)
    parser.add_argument("--task", help="read only this task's confirmed scope; never fall back")
    parser.add_argument("--session", help="explicit reliable client session ID; normally read from session_id input")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_OUTPUT_BYTES)
    try:
        args = parser.parse_args()
        if not 768 <= args.max_bytes <= MAX_OUTPUT_BYTES:
            raise HookError("output budget must be 768..8192 bytes; skipped")
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise HookError("oversized input; skipped")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise HookError("invalid input JSON; skipped") from None
        if not isinstance(payload, dict):
            raise HookError("input must be an object; skipped")
        response = handle(payload, args.tool, args.max_bytes, args.task, args.session)
    except HookError as exc:
        print(f"memory_hook: {exc}", file=sys.stderr)
        response = {}
    except Exception:
        # Never print exception details: file paths or metadata may be sensitive.
        print("memory_hook: metadata unavailable; skipped", file=sys.stderr)
        response = {}
    sys.stdout.buffer.write(serialized(response).encode("utf-8"))
    # Fail open: exit 2 means block/continue on some hosts; this hook never uses it.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
