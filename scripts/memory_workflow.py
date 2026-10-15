#!/usr/bin/env python3
"""Project suggestions, session selection and evidence-aware handoff checks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import uuid
from urllib.parse import unquote, urlsplit

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_common import MAX_SCOPE_BYTES, atomic_write, json_bytes, project_lock, safe_path

STATES = ("active", "paused", "completed")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
STATUS_MARKER = re.compile(r"(?m)^[ \t]*<!--[ \t]*ai-memory-task-status:[ \t]*(active|paused|completed)[ \t]*-->[ \t]*$")
MAX_SCOPE_SCAN = 128
MAX_PLAN_BYTES = 128 * 1024
FIELDS = ("changed", "pitfall", "fix", "verification", "next", "long_term_candidates")
DEFAULT_PRESET = {"detail": "standard", "modules": ["project"], "record_fields": list(FIELDS)}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _without_fences(text):
    """Mask fenced examples while preserving offsets used by status updates."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence:
            closing = re.match(r"^[ \t]{0,3}(" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",})[ \t]*(?:\n|$)", line)
            lines.append("".join("\n" if c == "\n" else " " for c in line))
            if closing:
                fence = None
        elif opening:
            fence = opening[1]
            lines.append("".join("\n" if c == "\n" else " " for c in line))
        else:
            lines.append(line)
    return "".join(lines)


def _root(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("项目根目录必须已存在。")
    return root


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("任务 ID 须为 1–80 位英文字母、数字、下划线或短横线。")
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value):
        raise ValueError("任务 ID 不能使用 Windows 保留设备名。")
    return value


def local_path(root, value, directory=False):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("文档路径必须是非空的项目内相对路径。")
    parts = PurePosixPath(value.replace("\\", "/"))
    if parts.is_absolute() or any(p == ".." or ":" in p for p in parts.parts):
        raise ValueError("文档路径不能越出项目目录。")
    target = safe_path(root, root.joinpath(*parts.parts), directory=directory)
    cursor = target if directory else target.parent
    while cursor != root:
        if (cursor / ".git").exists() or (cursor / ".ai-memory/project.json").exists():
            raise ValueError("文档路径属于另一个项目。")
        cursor = cursor.parent
    return target


def _metadata(root, path):
    safe_path(root, path)
    if path.stat().st_size > MAX_SCOPE_BYTES:
        raise ValueError(f"元数据超过 32 KiB，请按路径人工读取并精简：{path.relative_to(root).as_posix()}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SCOPE_BYTES + 1)
    if len(raw) > MAX_SCOPE_BYTES:
        raise ValueError(f"元数据超过 32 KiB，请按路径人工读取并精简：{path.relative_to(root).as_posix()}")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("元数据必须是 JSON 对象。")
    return value


def _project(root):
    project = _metadata(root, root / ".ai-memory/project.json")
    if (type(project.get("schema_version")) is not int or project["schema_version"] != 1
            or not isinstance(project.get("project_id"), str)):
        raise ValueError("项目身份或格式版本无效。")
    uuid.UUID(project["project_id"])
    from memory_store import _timestamp
    _timestamp(project.get("created_at"))
    return project


def load_scope(root, task_id, project_id=None):
    root = _root(root)
    _identifier(task_id)
    project_id = project_id or _project(root)["project_id"]
    scope = _metadata(root, root / f".ai-memory/scopes/{task_id}.json")
    confirmation = scope.get("confirmation")
    if (type(scope.get("schema_version")) is not int or scope["schema_version"] != 1
            or scope.get("project_id") != project_id
            or scope.get("task_id") != task_id or type(scope.get("revision")) is not int
            or scope["revision"] < 1 or not isinstance(confirmation, dict)
            or confirmation.get("caller_confirmed") is not True):
        raise ValueError("任务范围身份、修订号或确认声明无效。")
    from memory_store import _validate_scope
    return _validate_scope(scope, task_id, project_id)


def load_layout(root):
    root = _root(root)
    path = safe_path(root, root / ".ai-memory/layout.json")
    if path.exists():
        _metadata(root, path)  # Keep hook metadata reads within the shared limit.
    from memory_documents import load_layout as document_layout
    layout = document_layout(root)
    for value in layout["roles"].values():
        local_path(root, value)
    local_path(root, layout.get("document_root"), directory=True)
    local_path(root, layout.get("index"))
    return layout


def _plan_text(root, scope):
    path = local_path(root, scope["plan"])
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("计划超过 128 KiB；请先按需拆分，当前状态保持 unknown。")
    with path.open("rb") as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("读取期间计划超出大小限制。")
    return path, raw.decode("utf-8-sig").replace("\r\n", "\n")


def _status_mapping(root, scope):
    path = safe_path(root, root / ".ai-memory/status-mappings.json")
    if not path.exists():
        return None
    mapping = _metadata(root, path)
    if (type(mapping.get("schema_version")) is not int or mapping["schema_version"] != 1
            or mapping.get("project_id") != scope["project_id"]):
        raise ValueError("状态映射的项目身份或格式无效。")
    tasks = mapping.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("状态映射 tasks 必须为对象。")
    item = tasks.get(scope["task_id"])
    if item is None:
        return None
    if (not isinstance(item, dict) or set(item) != {"field", "values"}
            or not isinstance(item.get("field"), str) or not item["field"].strip()
            or re.search(r"[\r\n:：]", item["field"])
            or not isinstance(item.get("values"), dict) or set(item["values"]) != set(STATES)
            or any(not isinstance(v, str) or not v.strip() or re.search(r"[\r\n]", v)
                   for v in item["values"].values())
            or len(set(item["values"].values())) != len(STATES)):
        raise ValueError("状态映射需要唯一 field 和 active/paused/completed 三个不同的单行值。")
    return item


def _status_source(root, scope):
    if not scope.get("plan"):
        state = scope.get("task_status", "active") if scope.get("detail") == "small" else "unknown"
        return {"state": state if state in STATES else "unknown", "kind": "scope",
                "path": f".ai-memory/scopes/{scope['task_id']}.json"}
    path, text = _plan_text(root, scope)
    visible = _without_fences(text)
    mapping = _status_mapping(root, scope)
    marker_count = visible.count("ai-memory-task-status:")
    markers = list(STATUS_MARKER.finditer(visible))
    if marker_count:
        if mapping or marker_count != 1 or len(markers) != 1:
            raise ValueError("计划包含重复、无效或与映射并存的任务状态来源。")
        match = markers[0]
        return {"state": match.group(1), "kind": "marker", "path": scope["plan"],
                "text": text, "span": match.span(), "target": path}
    if mapping:
        pattern = re.compile(r"(?m)^(?:[ \t]*[-*][ \t]+)?[ \t]*" + re.escape(mapping["field"])
                             + r"[ \t]*[:：][ \t]*(.*?)[ \t]*$")
        matches = list(pattern.finditer(visible))
        if len(matches) != 1:
            raise ValueError("计划中的映射状态字段必须恰好出现一次。")
        match = matches[0]
        state = next((key for key, value in mapping["values"].items() if value == match.group(1)), "unknown")
        return {"state": state, "kind": "mapping", "path": scope["plan"], "text": text,
                "span": match.span(1), "target": path, "mapping": mapping}
    return {"state": "unknown", "kind": "unmarked_plan", "path": scope["plan"], "text": text, "target": path}


def task_state(root, scope):
    try:
        return _status_source(_root(root), scope)["state"]
    except (OSError, ValueError, TypeError, KeyError):
        return "unknown"


def set_task_status(root, task_id, status=None):
    root = _root(root)
    if status is not None and status not in STATES:
        raise ValueError("status 仅支持 active、paused、completed。")
    if status is None:
        scope = load_scope(root, task_id)
        try:
            source = _status_source(root, scope)
            error = None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            source, error = {"state": "unknown", "kind": "unavailable", "path": scope.get("plan")}, str(exc)
        return {"command": "task-status", "task_id": task_id, "status": source["state"],
                "source": source["path"], "source_kind": source["kind"], "errors": [error] if error else []}
    load_scope(root, task_id)  # Validate before the writer lock creates any file.
    with project_lock(root):
        scope = load_scope(root, task_id)
        source = _status_source(root, scope)
        if source["kind"] == "scope":
            if scope.get("detail") != "small":
                raise ValueError("standard/major 任务须以唯一计划保存状态，请先登记 plan。")
            scope["task_status"] = status
            raw = json_bytes(scope)
            if len(raw) > MAX_SCOPE_BYTES:
                raise ValueError("范围更新超过 32 KiB，请先精简范围。")
            atomic_write(root, root / source["path"], raw, replace=True)
        else:
            text = source["text"]
            if source["kind"] == "unmarked_plan":
                text = f"<!-- ai-memory-task-status: {status} -->\n" + text
            else:
                start, end = source["span"]
                replacement = (source["mapping"]["values"][status] if source["kind"] == "mapping"
                               else f"<!-- ai-memory-task-status: {status} -->")
                text = text[:start] + replacement + text[end:]
            atomic_write(root, source["target"], text.encode("utf-8"), replace=True)
    return {"command": "task-status", "task_id": task_id, "status": status, "source": source["path"],
            "scope_revision": scope["revision"], "confirmation_changed": False}


def _session(client, session):
    if not isinstance(client, str) or not IDENTIFIER.fullmatch(client):
        raise ValueError("client 必须是 1–80 位英文字母、数字、下划线或短横线。")
    if (not isinstance(session, str) or not session.strip() or len(session) > 512
            or any(ord(c) < 32 for c in session)):
        raise ValueError("session 必须是客户端提供的非空单行标识，最多 512 字符。")
    return hashlib.sha256(json.dumps([client, session], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def bind(root, task_id, client, session):
    root = _root(root)
    key = _session(client, session)
    load_scope(root, task_id)
    with project_lock(root):
        project = _project(root)
        load_scope(root, task_id, project["project_id"])
        path = safe_path(root, root / f".ai-memory/bindings/{key}.json")
        value = {"schema_version": 1, "project_id": project["project_id"], "client": client,
                 "session": session, "task_id": task_id, "updated_at": _now()}
        atomic_write(root, path, json_bytes(value), replace=path.exists())
    return {"command": "bind", "task_id": task_id, "path": path.relative_to(root).as_posix(),
            "client": client, "session": session, "authorization_changed": False}


def unbind(root, client, session):
    root = _root(root)
    key = _session(client, session)
    _project(root)
    with project_lock(root):
        project = _project(root)
        path = safe_path(root, root / f".ai-memory/bindings/{key}.json")
        existed = path.exists()
        if existed:
            value = _metadata(root, path)
            if value.get("project_id") != project["project_id"] or value.get("client") != client or value.get("session") != session:
                raise ValueError("绑定身份不匹配，请人工核实该绑定文件。")
            path.unlink()
    return {"command": "unbind", "client": client, "session": session, "removed": existed,
            "authorization_changed": False}


def resolve_selection(root, task_id=None, client=None, session=None):
    root = _root(root)
    result = {"task_id": None, "selection_required": True, "errors": [], "warnings": [],
              "candidates": [], "source": None, "scan_complete": True}
    try:
        project = _project(root)
    except (OSError, ValueError, TypeError) as exc:
        result["errors"].append({"path": ".ai-memory/project.json", "code": "invalid_project", "message": str(exc)})
        return result
    selected = task_id
    if selected is None and session is not None:
        try:
            key = _session(client, session)
            path = safe_path(root, root / f".ai-memory/bindings/{key}.json")
            if path.exists():
                value = _metadata(root, path)
                if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
                        or value.get("project_id") != project["project_id"]
                        or value.get("client") != client or value.get("session") != session):
                    raise ValueError("绑定身份不匹配。")
                selected = value.get("task_id")
                _identifier(selected)
                result["source"] = "binding"
        except (OSError, ValueError, TypeError) as exc:
            result["errors"].append({"code": "invalid_binding", "message": str(exc)})
            return result
    if selected is not None:
        try:
            scope = load_scope(root, selected, project["project_id"])
        except (OSError, ValueError, TypeError) as exc:
            result["errors"].append({"path": f".ai-memory/scopes/{selected}.json" if isinstance(selected, str) and IDENTIFIER.fullmatch(selected) else ".ai-memory/scopes/",
                                     "code": "invalid_binding" if result["source"] == "binding" else "invalid_task", "message": str(exc)})
            return result
        result.update(task_id=selected, selection_required=False, source=result["source"] or "explicit")
        state = task_state(root, scope)
        if state != "active":
            result["warnings"].append({"code": "selected_task_not_active", "state": state})
        return result
    directory = root / ".ai-memory/scopes"
    try:
        safe_path(root, directory, directory=True)
        if directory.exists():
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_SCOPE_SCAN:
                        result["scan_complete"] = False
                        break
                    if not entry.name.endswith(".json"):
                        continue
                    try:
                        scope = load_scope(root, entry.name[:-5], project["project_id"])
                        result["candidates"].append({"task_id": scope["task_id"], "state": task_state(root, scope),
                                                     "path": f".ai-memory/scopes/{entry.name}"})
                    except (OSError, ValueError, TypeError) as exc:
                        result["scan_complete"] = False
                        result["errors"].append({"path": f".ai-memory/scopes/{entry.name}", "code": "unavailable_scope", "message": str(exc)})
    except (OSError, ValueError) as exc:
        result["scan_complete"] = False
        result["errors"].append({"path": ".ai-memory/scopes", "code": "incomplete_scan", "message": str(exc)})
    result["candidates"].sort(key=lambda item: item["task_id"])
    active = [item for item in result["candidates"] if item["state"] == "active"]
    unknown = [item for item in result["candidates"] if item["state"] == "unknown"]
    if result["scan_complete"] and not unknown and len(active) == 1:
        result.update(task_id=active[0]["task_id"], selection_required=False, source="unique_active")
    elif not result["scan_complete"]:
        result["warnings"].append({"code": "incomplete_scan"})
    elif unknown:
        result["warnings"].append({"code": "unknown_task_state", "tasks": [item["task_id"] for item in unknown]})
    return result


def project_preset(root, preset=None, confirmed=False):
    root = _root(root)
    project = _project(root)
    path = safe_path(root, root / ".ai-memory/presets.json")
    if preset is None:
        value = _metadata(root, path) if path.exists() else {"schema_version": 1, "project_id": project["project_id"], "defaults": DEFAULT_PRESET}
        if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("project_id") != project["project_id"]):
            raise ValueError("预设格式或项目身份不匹配。")
        return {"command": "preset", "preset": value, "saved": path.exists(), "requires_task_confirmation": True}
    if confirmed is not True:
        raise ValueError("修改项目预设须有实际授权并使用 --confirmed；预设仅是建议，不能代替新任务的真实授权。")
    if not isinstance(preset, dict) or set(preset) - {"detail", "modules", "record_fields", "notes"}:
        raise ValueError("预设仅接受 detail、modules、record_fields、notes。")
    value = {**DEFAULT_PRESET, **preset}
    if value["detail"] not in ("small", "standard", "major"):
        raise ValueError("预设 detail 无效。")
    for field in ("modules", "record_fields"):
        if (not isinstance(value[field], list) or not value[field]
                or any(not isinstance(item, str) or not item.strip() or re.search(r"[\r\n\x00]", item) for item in value[field])):
            raise ValueError(f"预设 {field} 必须是非空的单行字符串数组。")
        value[field] = list(dict.fromkeys(value[field]))
    if set(value["record_fields"]) - set(FIELDS):
        raise ValueError("预设 record_fields 存在未知字段。")
    if "notes" in value and (not isinstance(value["notes"], str) or "\x00" in value["notes"]):
        raise ValueError("预设备注必须是字符串。")
    value = {"schema_version": 1, "project_id": project["project_id"], "defaults": value,
             "updated_at": _now(), "requires_task_confirmation": True,
             "confirmation": {"caller_confirmed": True, "notice": "项目预设只用于建议，不是任务授权。"}}
    raw = json_bytes(value)
    if len(raw) > MAX_SCOPE_BYTES:
        raise ValueError("项目预设超过 32 KiB。")
    with project_lock(root):
        if _project(root)["project_id"] != project["project_id"]:
            raise ValueError("项目身份在写入前发生变化。")
        atomic_write(root, path, raw, replace=path.exists())
    return {"command": "preset", "preset": value, "saved": True, "requires_task_confirmation": True}


CHECK_LABELS = {
    "constraints": ("约束", "constraints", "限制", "边界"),
    "completed": ("已完成", "完成情况", "改动", "changed", "completed"),
    "verification": ("验证", "verification"), "blockers": ("阻塞", "blockers"),
    "next": ("下一步", "next"), "impact": ("影响", "impact"),
    "tradeoffs": ("取舍", "tradeoffs", "方案选择"),
    "rollback": ("迁移", "回退", "rollback", "migration"),
    "acceptance": ("验收", "acceptance"),
}
VERIFICATION_STATE = re.compile(r"(?m)^\s*-?\s*(?:状态|status)[:：]\s*(passed|failed|partial|not_run)\b")
VERIFICATION_DECLARATION = re.compile(r"(?im)^[ \t]*(?:[-*+][ \t]*)?(?:状态|status)[ \t]*[:：][ \t]*(.*?)[ \t]*$")


def _sections(text, aliases):
    """Find explicit labeled content; headings alone never count as evidence."""
    matches = []
    lines = _without_fences(text).splitlines()
    for index, line in enumerate(lines):
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        label = re.match(r"^\s*(?:[-*]\s+)?([^:：\n]+)[:：]\s*(.*)$", line)
        if heading and any(alias.lower() in heading[1].lower() for alias in aliases):
            end = index + 1
            while end < len(lines) and not re.match(r"^#{1,6}\s+", lines[end]):
                end += 1
            matches.append("\n".join(lines[index + 1:end]).strip())
        elif label and any(alias.lower() == label[1].strip().lower() for alias in aliases):
            matches.append(label[2].strip())
    return [value for value in matches if value]


def _verification_replacements(restored, task_id):
    """Apply only explicit, fully read verification replacements within this task."""
    if (restored.get("scan_complete") is not True or restored.get("error_count") != 0
            or restored.get("errors") or restored.get("output_truncated") is not False):
        return {}
    records = {item["metadata"]["event_id"]: item for item in restored.get("records", [])}
    complete = {identifier: item for identifier, item in records.items()
                if item.get("body_checksum_verified") is True and item.get("content_truncated") is False
                and item.get("read_required") is False and item.get("archived") is False
                and item["metadata"].get("task_id") == task_id}
    eligible = {}
    for identifier, item in complete.items():
        if _journal_verification_claim(item) is not None:
            eligible[identifier] = item
    edges = [edge for edge in restored.get("relationships", []) if edge.get("relation") == "supersedes"]
    # A selected replacement may itself have an unread replacement (including
    # an archived one). Do not silently choose its conclusion in that case.
    if any(edge.get("target") in records and edge.get("event_id") not in eligible for edge in edges):
        return {}
    verified_pairs = {(edge["event_id"], edge["target"]) for edge in edges}
    replacements = {}
    for identifier, item in eligible.items():
        for target in item["metadata"].get("supersedes", []):
            if target in complete and (identifier, target) in verified_pairs:
                replacements.setdefault(target, []).append(identifier)
    return {target: sorted(sources) for target, sources in replacements.items()}


def _journal_verification_claim(record):
    """Recognize a complete v2 generated section, never a loose Markdown heading."""
    from memory_store import FIELDS, LABELS
    text = record.get("markdown", "")
    if record["metadata"].get("schema_version") != 2 or not text.startswith("# 任务记录\n\n"):
        return None
    visible = _without_fences(text)
    headings = list(re.finditer(r"(?m)^##[ \t]+[^\n]*$", visible))
    expected = ["## " + LABELS[field] for field in FIELDS]
    names = [match[0] for match in headings]
    if (any(name not in expected for name in names) or len(names) != len(set(names))
            or [expected.index(name) for name in names] != sorted(expected.index(name) for name in names)
            or "## " + LABELS["verification"] not in names):
        return None
    index = names.index("## " + LABELS["verification"])
    end = headings[index + 1].start() if index + 1 < len(headings) else len(visible)
    section = visible[headings[index].end():end].strip()
    lines = section.splitlines()
    footer = "- 证据性质：调用者提供的执行记录；存储仅验证格式与完整性，不执行命令或证明业务正确。"
    if (len(lines) < 3 or not lines[1].startswith("- 说明：") or not lines[1][5:].strip()
            or lines[-1] != footer or re.search(r"(?m)^#{1,6}[ \t]+", section)):
        return None
    declarations = VERIFICATION_DECLARATION.findall(section)
    if (len(declarations) != 1 or declarations[0] not in ("passed", "failed", "partial", "not_run")
            or lines[0] != "- 状态：" + declarations[0]):
        return None
    return declarations[0]


def handoff(root, task_id):
    root = _root(root)
    scope = load_scope(root, task_id)
    errors, warnings, sources = [], [], []
    if scope.get("plan"):
        try:
            path, text = _plan_text(root, scope)
            sources.append({"path": path.relative_to(root).as_posix(), "text": text, "kind": "current_plan"})
        except (OSError, ValueError) as exc:
            errors.append({"path": scope["plan"], "message": str(exc)})
    elif scope.get("detail") != "small":
        errors.append({"code": "missing_plan", "message": "standard/major 任务缺少唯一活动计划。"})
    # Read checksum-verified bounded journal excerpts, never execute their commands.
    from memory_store import resume_memory
    restored = resume_memory(root, task_id=task_id, limit=10, max_bytes=MAX_PLAN_BYTES,
                             max_output_bytes=2 * MAX_PLAN_BYTES)
    replacements = _verification_replacements(restored, task_id)
    for record in restored.get("records", []):
        event_id = record["metadata"]["event_id"]
        sources.append({"path": record["path"], "text": record.get("markdown", ""), "kind": "historical_evidence",
                        "event_id": event_id, "verification_superseded_by": replacements.get(event_id, [])})
    errors.extend(restored.get("errors", []))
    incomplete = bool(restored.get("has_more") or restored.get("content_truncated") or restored.get("output_truncated")
                      or any(record.get("read_required") for record in restored.get("records", [])))
    if incomplete:
        warnings.append({"code": "partial_history", "message": "只检查当前计划和最近 10 条日志的有界正文；未覆盖内容不能据此判定缺失。"})
    required = ["completed", "verification", "next"]
    if scope.get("detail") != "small":
        required = ["constraints", "completed", "verification", "blockers", "next"]
    if scope.get("detail") == "major":
        required += ["impact", "tradeoffs", "rollback", "acceptance"]
    verification_reports = []
    checks = [{"field": "goal", "status": "present" if str(scope.get("goal", "")).strip() else "missing",
               "sources": [f".ai-memory/scopes/{task_id}.json"]}]
    for field in required:
        applicable = sources
        if field == "next" and scope.get("plan"):
            applicable = [source for source in sources if source["kind"] == "current_plan"]
        values = [(source, content) for source in applicable for content in _sections(source["text"], CHECK_LABELS[field])]
        meaningful = [(source, content) for source, content in values
                      if not re.fullmatch(r"[\s\-]*(?:待补充|待填写|TODO|TBD|尚未记录|待确认)[。.!！\s]*", content, re.I)]
        if field == "verification" and meaningful:
            for source, content in meaningful:
                declared = VERIFICATION_STATE.search(content)
                verification_reports.append({"source": source["path"], "source_kind": source["kind"],
                                             "event_id": source.get("event_id"),
                                             "declared_status": declared[1] if declared else "unstructured",
                                             "applicability": "superseded" if source.get("verification_superseded_by") else "current",
                                             "superseded_by": source.get("verification_superseded_by", []),
                                             "claim_verified": False})
            meaningful = [(source, content) for source, content in meaningful if not source.get("verification_superseded_by")]
        status = "present" if meaningful else ("not_found_in_checked_sources" if incomplete else "missing")
        if field == "verification" and meaningful:
            declared_states = {report["declared_status"] for report in verification_reports if report["applicability"] == "current"}
            if (declared_states.intersection({"partial", "not_run"})
                    or any(re.search(r"未(?:执行|验证|做)|not_run|尚未(?:执行|验证|开始)", content, re.I)
                           for _, content in meaningful)):
                status = "not_run_or_partial"
            elif "failed" in declared_states or any(re.search(r"failed|失败", content, re.I) for _, content in meaningful):
                status = "failure_reported"
        checks.append({"field": field, "status": status,
                       "sources": list(dict.fromkeys(source["path"] for source, _ in meaningful))})
    if len({report["declared_status"] for report in verification_reports
            if report["applicability"] == "current" and report["declared_status"] != "unstructured"}) > 1:
        warnings.append({"code": "mixed_verification_history", "message": "仍适用的记录包含不同结构化验证声明；请按适用文件和证据核对，未按时间自动选取结论。"})
    try:
        layout = load_layout(root)
        entries = list(layout["roles"].values()) + [layout["index"]]
        for entry in entries:
            if not local_path(root, entry).is_file():
                warnings.append({"code": "missing_entrypoint", "path": entry})
    except (OSError, ValueError) as exc:
        errors.append({"path": ".ai-memory/layout.json", "message": str(exc)})
    # Check local Markdown links and record_event's explicit evidence references;
    # existence never proves that a command was run or the linked claim is true.
    evidence = []
    for source in sources:
        visible = _without_fences(source["text"])
        targets = [(value, False) for value in re.findall(r"\[[^\]\n]+\]\((<[^>]+>|[^)\s]+)\)", visible)]
        targets += [(value, False) for value in re.findall(r"(?m)^\s*\[[^\]\n]+\]:\s*(<[^>]+>|\S+)", visible)]
        for declared in re.findall(r"(?m)^\s*-\s*证据引用[:：]\s*(.+)$", visible):
            for value in re.split(r"[；;]", declared):
                value = value.strip().strip("`")
                if not value or value == "本次未提供" or re.search(r"\[[^]]+\]\(", value):
                    continue
                if value.startswith(("git:", "commit:")) or re.fullmatch(r"[0-9a-f]{7,64}", value):
                    evidence.append({"source": source["path"], "reference": value, "kind": "revision",
                                     "claim_verified": False})
                else:
                    targets.append((value, True))
        for target, project_relative in targets:
            target = target.strip("<>")
            try:
                parsed = urlsplit(target)
            except ValueError:
                warnings.append({"code": "invalid_evidence_link", "source": source["path"], "path": target})
                continue
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            parent = root if project_relative else root / Path(source["path"]).parent
            path = Path(os.path.abspath(parent / unquote(parsed.path)))
            try:
                relative = path.relative_to(root).as_posix()
                valid = local_path(root, relative).is_file()
            except (OSError, ValueError):
                relative, valid = target, False
            evidence.append({"source": source["path"], "path": relative, "exists": valid,
                             "claim_verified": False})
            if not valid:
                warnings.append({"code": "unavailable_evidence", "source": source["path"], "path": relative})
    from memory_health import health
    try:
        document_health = health(root, task_id=task_id, max_bytes=8192)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        document_health = {"command": "health", "read_only": True, "scan_complete": False,
                           "needs_attention": True, "total_counts": {"errors": 1},
                           "errors": [{"code": "health_unavailable", "message": str(exc)}],
                           "business_validation": "not_performed"}
    health_counts = document_health.get("total_counts", {})
    gaps = [check["field"] for check in checks if check["status"] != "present"]
    for field, count_key in (("document_freshness", "changes"), ("document_conflicts", "conflicts")):
        if health_counts.get(count_key, 0):
            gaps.append(field)
    if health_counts.get("errors", 0) or not document_health.get("scan_complete", True):
        gaps.append("document_health")
        errors.append({"code": "document_health_errors", "count": health_counts.get("errors", 0),
                       "message": "文档核对未能完整完成，按 document_health 的路径检查。"})
    if document_health.get("output_truncated"):
        warnings.append({"code": "partial_document_health", "message": "文档核对报告已按预算省略；使用 health 查看其余线索。"})
    return {"command": "handoff", "task_id": task_id, "detail": scope.get("detail"),
            "task_status": task_state(root, scope), "checks": checks, "evidence_links": evidence,
            "verification_reports": verification_reports,
            "checked_sources": [{"path": source["path"], "kind": source["kind"]} for source in sources],
            "gaps": gaps, "document_health": document_health,
            "errors": errors, "warnings": warnings, "business_validation": "not_performed",
            "semantic_review_required": True,
            "note": "检查记录结构、入口引用及已登记文档依赖变化。命令、测试结论和完成状态均为记录者声明；未执行项目命令，也未证明业务通过。请由 AI 核对实际文件、取舍和证据真实性。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bind", "unbind", "task-status", "preset", "handoff"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
        if name in ("bind", "task-status", "handoff"):
            command.add_argument("--task", required=True)
        if name in ("bind", "unbind"):
            command.add_argument("--client", required=True)
            command.add_argument("--session", required=True)
        if name == "task-status":
            command.add_argument("--status", choices=STATES)
        if name == "preset":
            command.add_argument("--file")
            command.add_argument("--confirmed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "bind":
            result = bind(args.root, args.task, args.client, args.session)
        elif args.command == "unbind":
            result = unbind(args.root, args.client, args.session)
        elif args.command == "task-status":
            result = set_task_status(args.root, args.task, args.status)
        elif args.command == "preset":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig")) if args.file else None
            result = project_preset(args.root, payload, args.confirmed)
        else:
            result = handoff(args.root, args.task)
        status = 1 if result.get("errors") else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result, status = {"command": args.command, "error": str(exc), "business_validation": "not_performed"}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
