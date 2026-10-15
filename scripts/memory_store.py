#!/usr/bin/env python3
"""任务范围确认、不可覆盖的 Markdown 日志及只读恢复；仅使用标准库。"""

from __future__ import annotations

import argparse
import codecs
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

# Also support callers loading this script with importlib from another directory.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_common import MAX_SCOPE_BYTES, project_lock, safe_path, atomic_write, read_json, json_bytes


STORE = ".ai-memory"
JOURNAL = "docs/memory/journal"
FIELDS = ("changed", "pitfall", "fix", "verification", "next", "long_term_candidates")
LABELS = {
    "changed": "改动 / changed", "pitfall": "踩坑 / pitfall", "fix": "解决 / fix",
    "verification": "验证 / verification", "next": "下一步 / next",
    "long_term_candidates": "长期规则候选（尚未批准，不生效） / long_term_candidates",
}
PREFIX = "<!-- ai-memory "
MAX_HEADER_BYTES = 16384


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str, label: str = "标识") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError(f"{label}须为 1–80 位英文字母、数字、下划线或短横线，并以字母或数字开头。")
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value):
        raise ValueError(f"{label}不能使用 Windows 保留设备名。")
    return value


def _root(value) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("项目根目录必须已存在。")
    return root


def _safe(root: Path, path: Path, directory: bool = False) -> Path:
    return safe_path(root, path, directory=directory)


def _text(value, label: str, *, single_line=False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须是非空字符串。")
    value = value.strip().replace("\r\n", "\n")
    if "\x00" in value or (single_line and ("\n" in value or "\r" in value)):
        raise ValueError(f"{label}包含不允许的控制符或换行。")
    return value


def _strings(value, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label}必须是字符串数组。")
    return list(dict.fromkeys(_text(item, label, single_line=True) for item in value))


def _json_bytes(value) -> bytes:
    return json_bytes(value)


def _load_json(root: Path, path: Path) -> dict:
    return read_json(root, path)


def _publish(root: Path, path: Path, data: bytes, *, replace=False) -> None:
    atomic_write(root, path, data, replace=replace)


@contextmanager
def _writer_lock(root: Path, timeout=10.0):
    with project_lock(root, timeout=timeout):
        yield


def _project(root: Path) -> dict:
    value = _load_json(root, root / STORE / "project.json")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("不支持的项目记忆配置版本。")
    uuid.UUID(value.get("project_id", ""))
    _timestamp(value.get("created_at"))
    return value


def _validate_scope(value: dict, task_id: str, project_id: str) -> dict:
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("task_id") != task_id or value.get("project_id") != project_id):
        raise ValueError("任务范围的项目身份、任务 ID 或格式版本不匹配。")
    if type(value.get("revision")) is not int or value["revision"] < 1:
        raise ValueError("任务范围缺少有效 revision。")
    if value.get("confirmation", {}).get("caller_confirmed") is not True:
        raise ValueError("任务范围尚未记录调用者的确认声明。")
    _text(value["confirmation"].get("summary"), "confirmation.summary")
    _timestamp(value["confirmation"].get("recorded_at"))
    _timestamp(value.get("updated_at"))
    if value.get("detail") not in ("small", "standard", "major"):
        raise ValueError("任务范围的 detail 无效。")
    if value.get("mode") != "task_then_auto" or value.get("long_term_auto") is not False:
        raise ValueError("任务范围的自动记录模式不受支持。")
    if not _strings(value.get("modules"), "modules"):
        raise ValueError("任务范围至少需要一个模块。")
    fields = _strings(value.get("record_fields"), "record_fields")
    if not fields or set(fields) - set(FIELDS):
        raise ValueError("任务范围的记录字段无效。")
    _text(value.get("goal"), "goal")
    if value.get("plan") is not None:
        _text(value["plan"], "plan", single_line=True)
    return value


def _scope(root: Path, task_id: str, project_id: str) -> dict:
    return _validate_scope(_load_json(root, root / STORE / "scopes" / (task_id + ".json")), task_id, project_id)


def _scope_value(project_id, task_id, normalized, revision, timestamp):
    return {"schema_version": 1, "project_id": project_id, "task_id": task_id,
            "revision": revision,
            **{key: item for key, item in normalized.items() if key != "confirmation_summary"},
            "mode": "task_then_auto", "long_term_auto": False,
            "confirmation": {"caller_confirmed": True, "summary": normalized["confirmation_summary"],
                             "recorded_at": timestamp,
                             "notice": "仅记录调用者声明，不构成独立的人类确认凭证。"},
            "updated_at": timestamp}


def _scope_bytes(value):
    data = _json_bytes(value)
    if len(data) > MAX_SCOPE_BYTES:
        raise ValueError(f"任务范围超过 {MAX_SCOPE_BYTES} 字节上限；请将详细内容放入计划，范围仅保留摘要和计划路径。")
    return data


def _plan_path(root, value):
    from memory_workflow import local_path
    return local_path(root, value)


def configure_scope(root, task_id: str, scope_dict: dict, confirmed=False, expected_revision=None) -> dict:
    root = _root(root)
    task_id = _identifier(task_id, "任务 ID")
    if confirmed is not True:
        raise ValueError("须有本任务的真实记录授权，并以 --confirmed 记录调用者声明；已有明确指令无需重复问答。")
    allowed = {"goal", "detail", "modules", "record_fields", "plan", "confirmation_summary"}
    if not isinstance(scope_dict, dict) or set(scope_dict) - allowed:
        raise ValueError("scope 包含未知字段；revision 由存储生成，变更时使用 --expected-revision。")
    goal = _text(scope_dict.get("goal"), "goal")
    detail = scope_dict.get("detail", "standard")
    if detail not in ("small", "standard", "major"):
        raise ValueError("detail 仅支持 small、standard、major。")
    modules = _strings(scope_dict.get("modules", ["project"]), "modules")
    fields = _strings(scope_dict.get("record_fields", list(FIELDS)), "record_fields")
    if not modules or not fields or set(fields) - set(FIELDS):
        raise ValueError("modules 和 record_fields 不能为空，记录字段必须在受支持列表中。")
    summary = _text(scope_dict.get("confirmation_summary"), "confirmation_summary")
    plan = scope_dict.get("plan")
    if plan is not None:
        plan = _text(plan, "plan", single_line=True)
        if Path(plan).is_absolute() or re.match(r"^[A-Za-z]:", plan):
            raise ValueError("plan 必须是项目内的相对路径。")
        target = _plan_path(root, plan)
        if not target.is_file():
            raise ValueError("plan 必须指向实际存在的项目文件。")
        plan = target.relative_to(root).as_posix()
    normalized = {"goal": goal, "detail": detail, "modules": modules, "record_fields": fields,
                  "plan": plan, "confirmation_summary": summary}
    # Reject oversized input before creating the lock, project identity, or history.
    _scope_bytes(_scope_value(str(uuid.UUID(int=0)), task_id, normalized, 1, utc_now()))
    # 所有潜在写入路径在初始化共享配置前预检。
    scope_path = _safe(root, root / STORE / "scopes" / (task_id + ".json"))
    _safe(root, root / STORE / "project.json")
    _safe(root, root / JOURNAL, directory=True)
    with _writer_lock(root):
        if not (root / STORE / "project.json").exists():
            scopes_path = _safe(root, root / STORE / "scopes", directory=True)
            journal_path = _safe(root, root / JOURNAL, directory=True)
            if (scopes_path.exists() and any(scopes_path.iterdir())) or (journal_path.exists() and any(journal_path.iterdir())):
                raise ValueError("发现没有项目身份配置的既存范围或日志，须先人工恢复身份，不能自动重建。")
            _publish(root, root / STORE / "project.json", _json_bytes(
                {"schema_version": 1, "project_id": str(uuid.uuid4()), "created_at": utc_now()}))
        project = _project(root)
        previous = None
        if scope_path.exists():
            previous = _scope(root, task_id, project["project_id"])
            previous_content = {key: previous.get(key) for key in normalized if key != "confirmation_summary"}
            previous_content["confirmation_summary"] = previous["confirmation"].get("summary")
            if previous_content == normalized:
                return {"command": "scope", "status": "preserved", "scope": previous, "path": scope_path.relative_to(root).as_posix()}
            if expected_revision != previous["revision"]:
                raise ValueError(f"范围已存在或发生变化；重新确认后提供 --expected-revision {previous['revision']}，不覆盖未知的新版本。")
        elif expected_revision not in (None, 0):
            raise ValueError("新任务不存在可匹配的 expected_revision。")
        timestamp = utc_now()
        value = _scope_value(project["project_id"], task_id, normalized,
                             previous["revision"] + 1 if previous else 1, timestamp)
        if previous and plan is None and previous.get("task_status") is not None:
            value["task_status"] = previous["task_status"]
        data = _scope_bytes(value)
        if previous:
            backup = root / STORE / "scopes" / "history" / task_id / f"revision-{previous['revision']}.json"
            _safe(root, backup)
            original = scope_path.read_bytes()
            if backup.exists():
                if backup.read_bytes() != original:
                    raise ValueError("旧范围备份内容冲突，拒绝覆盖。")
            else:
                _publish(root, backup, original)
        _publish(root, scope_path, data, replace=previous is not None)
        return {"command": "scope", "status": "updated" if previous else "created", "scope": value,
                "path": scope_path.relative_to(root).as_posix()}


def _timestamp(value) -> str:
    text = _text(value, "time", single_line=True)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time 必须带时区。")
    try:
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except OverflowError as exc:
        raise ValueError("time 转换为 UTC 后超出支持范围。") from exc


def _render_body(event: dict, *, legacy=False) -> str:
    sections = ["# 任务记录"]
    for field in FIELDS:
        if field not in event:
            continue
        value = event[field]
        if field == "verification" and isinstance(value, dict):
            if set(value) - {"status", "summary", "command", "result", "revision", "evidence"} or value.get("status") not in ("passed", "failed", "partial", "not_run"):
                raise ValueError("verification 对象需要有效 status，仅接受 status、summary、command、result、revision、evidence。")
            content = f"- 状态：{value['status']}\n- 说明：{_text(value.get('summary'), 'verification.summary')}"
            for key, label in (("command", "执行命令"), ("result", "结果"), ("revision", "文件或提交版本")):
                if key in value:
                    content += f"\n- {label}：" + _text(value[key], "verification." + key)
            if "evidence" in value:
                evidence = _strings(value["evidence"], "verification.evidence")
                content += "\n- 证据引用：" + ("；".join(evidence) if evidence else "本次未提供")
            if not legacy:
                content += "\n- 证据性质：调用者提供的执行记录；存储仅验证格式与完整性，不执行命令或证明业务正确。"
        elif isinstance(value, str):
            content = _text(value, field)
        elif isinstance(value, list):
            entries = [_text(item, field) for item in value]
            content = "\n".join("- " + item.replace("\n", "\n  ") for item in entries) if entries else "本次未记录。"
        else:
            raise ValueError(f"{field} 必须是字符串或字符串数组。")
        sections.append(f"## {LABELS[field]}\n\n{content}")
    if len(sections) == 1:
        raise ValueError("事件至少需要一项已确认范围内的正文记录。")
    return "\n\n".join(sections) + "\n"


def _event_paths(root: Path, errors: list, warnings: list) -> list[Path]:
    directory = root / JOURNAL
    try:
        _safe(root, directory, directory=True)
        if not directory.exists():
            return []
        days = sorted(directory.iterdir())
    except (OSError, ValueError) as exc:
        errors.append({"path": JOURNAL, "message": str(exc)})
        return []
    found = []
    for day in days:
        try:
            _safe(root, day, directory=True)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
                raise ValueError(f"日志日期目录无效：{day.name}")
            date.fromisoformat(day.name)
            paths = sorted(day.iterdir())
        except (OSError, ValueError) as exc:
            errors.append({"path": day.relative_to(root).as_posix(), "message": str(exc)})
            continue
        for path in paths:
            try:
                _safe(root, path)
                relative = path.relative_to(root).as_posix()
                if path.name.startswith(".tmp-"):
                    warnings.append({"path": relative, "code": "incomplete_write", "message": "发现未发布的临时文件；保留原文，须人工核验。"})
                elif path.suffix == ".md":
                    found.append(path)
                else:
                    warnings.append({"path": relative, "code": "unexpected_file", "message": "日志目录内有未知文件，未读取。"})
            except (OSError, ValueError) as exc:
                errors.append({"path": path.relative_to(root).as_posix(), "message": str(exc)})
    return found


def _matching_event_paths(root: Path, event_id: str) -> list[Path]:
    """写入只逐日检查同名事件；完整目录审计由 audit 单独执行。"""
    directory = _safe(root, root / JOURNAL, directory=True)
    if not directory.exists():
        return []
    found = []
    for day in sorted(directory.iterdir()):
        _safe(root, day, directory=True)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
            raise ValueError(f"日志日期目录无效：{day.name}")
        date.fromisoformat(day.name)
        candidate = _safe(root, day / (event_id + ".md"))
        if candidate.exists():
            found.append(candidate)
    return found


def _parse_metadata(path: Path, header: bytes, project_id: str) -> dict:
    if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n"):
        raise ValueError("日志元数据首行过长或不完整。")
    first = header[:-1].removesuffix(b"\r").decode("utf-8")
    if not first.startswith(PREFIX) or not first.endswith(" -->"):
        raise ValueError("日志缺少完整的元数据首行。")
    metadata = json.loads(first[len(PREFIX):-4])
    base = {"event_id", "project_id", "task_id", "actor", "time", "modules", "payload_sha256"}
    if not isinstance(metadata, dict):
        raise ValueError("日志元数据字段无效。")
    schema_version = metadata.get("schema_version", 1)
    if type(schema_version) is not int:
        raise ValueError("日志格式版本必须是整数。")
    if schema_version == 1:
        if set(metadata) != base:
            raise ValueError("旧版日志元数据字段无效。")
    elif schema_version == 2:
        if not base | {"schema_version", "scope_revision"} <= set(metadata) or set(metadata) - base - {"schema_version", "scope_revision", "corrects", "supersedes"}:
            raise ValueError("v2 日志元数据字段无效。")
        if type(metadata["scope_revision"]) is not int or metadata["scope_revision"] < 1:
            raise ValueError("v2 日志缺少有效 scope_revision。")
        for relation in ("corrects", "supersedes"):
            if relation in metadata:
                for identifier in _strings(metadata[relation], relation):
                    _identifier(identifier, "关联事件 ID")
                    if identifier == metadata.get("event_id"):
                        raise ValueError("事件不能更正或替代自身。")
    else:
        raise ValueError("不支持的日志格式版本。")
    if metadata["project_id"] != project_id:
        raise ValueError("日志属于另一项目，禁止混合恢复。")
    if _identifier(metadata["event_id"], "事件 ID") != path.stem:
        raise ValueError("日志文件名和事件 ID 不匹配。")
    _identifier(metadata["task_id"], "任务 ID")
    _text(metadata["actor"], "actor", single_line=True)
    if not _strings(metadata["modules"], "modules"):
        raise ValueError("日志缺少模块信息。")
    if _timestamp(metadata["time"])[:10] != path.parent.name:
        raise ValueError("日志日期和所在目录不匹配。")
    if not isinstance(metadata["payload_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", metadata["payload_sha256"]):
        raise ValueError("日志缺少有效的正文校验值。")
    return metadata


def _read_header(root: Path, path: Path, project_id: str) -> dict:
    _safe(root, path)
    with path.open("rb") as stream:
        return _parse_metadata(path, stream.readline(MAX_HEADER_BYTES + 1), project_id)


def _read_event(root: Path, path: Path, project_id: str) -> dict:
    _safe(root, path)
    raw = path.read_bytes()
    first, separator, body_bytes = raw.partition(b"\n")
    metadata = _parse_metadata(path, first + separator, project_id)
    normalized = body_bytes.replace(b"\r\n", b"\n")
    body = normalized.decode("utf-8")
    if metadata["payload_sha256"] not in (hashlib.sha256(body_bytes).hexdigest(), hashlib.sha256(normalized).hexdigest()):
        raise ValueError("日志正文校验值不匹配；原文保留，不能静默修复。")
    return {"path": path.relative_to(root).as_posix(), "metadata": metadata, "markdown": body}


def _stream_body(root: Path, path: Path, metadata: dict):
    """Validate UTF-8 plus either original or LF-normalized checksum in bounded space.

    The pending CR preserves CRLF pairs that straddle read boundaries. Bare CR is
    significant and never discarded. Consumers must exhaust this iterator.
    """
    _safe(root, path)
    raw_digest, normalized_digest = hashlib.sha256(), hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = b""
    with path.open("rb") as stream:
        if _parse_metadata(path, stream.readline(MAX_HEADER_BYTES + 1), metadata["project_id"]) != metadata:
            raise ValueError("读取期间日志元数据发生变化，请重试。")
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            raw_digest.update(chunk)
            chunk = pending + chunk
            pending = b"\r" if chunk.endswith(b"\r") else b""
            if pending:
                chunk = chunk[:-1]
            normalized = chunk.replace(b"\r\n", b"\n")
            normalized_digest.update(normalized)
            decoder.decode(normalized)
            yield normalized
        if pending:
            normalized_digest.update(pending)
            decoder.decode(pending)
            yield pending
        decoder.decode(b"", final=True)
    if metadata["payload_sha256"] not in (raw_digest.hexdigest(), normalized_digest.hexdigest()):
        raise ValueError("日志正文校验值不匹配；未输出损坏正文，原文未改动。")


def _validate_new_relations(root, project_id, event_id, relations):
    visiting, visited = set(), set()
    pending = [(target, False) for values in relations.values() for target in values]
    while pending:
        identifier, leaving = pending.pop()
        if leaving:
            visiting.discard(identifier)
            visited.add(identifier)
            continue
        if identifier == event_id or identifier in visiting:
            raise ValueError("更正或替代关系存在循环。")
        if identifier in visited:
            continue
        paths = _matching_event_paths(root, identifier)
        if len(paths) != 1:
            raise ValueError(f"关联事件不存在或不唯一：{identifier}")
        metadata = _read_header(root, paths[0], project_id)
        _scope(root, metadata["task_id"], project_id)
        for _ in _stream_body(root, paths[0], metadata):
            pass
        visiting.add(identifier)
        pending.append((identifier, True))
        pending.extend((target, False) for relation in ("corrects", "supersedes")
                       for target in metadata.get(relation, []))


def _relationship_errors(records):
    """Validate links with metadata only; iterative DFS handles long chains."""
    graph = {item["metadata"]["event_id"]: item for item in records}
    errors, visited = [], set()
    for identifier, item in graph.items():
        for relation in ("corrects", "supersedes"):
            for target in item["metadata"].get(relation, []):
                if target not in graph:
                    errors.append({"path": str(item["path"]), "message": f"{relation} 引用缺失或损坏事件：{target}"})
    for identifier in graph:
        if identifier in visited:
            continue
        visiting = set()
        stack = [(identifier, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                visiting.discard(current)
                visited.add(current)
                continue
            if current in visiting:
                errors.append({"path": str(graph[current]["path"]), "message": "更正或替代关系存在循环。"})
                continue
            if current in visited or current not in graph:
                continue
            visiting.add(current)
            stack.append((current, True))
            stack.extend((target, False) for relation in ("corrects", "supersedes")
                         for target in graph[current]["metadata"].get(relation, []) if target in graph)
    return errors


def record_event(root, task_id: str, event_dict: dict) -> dict:
    root = _root(root)
    task_id = _identifier(task_id, "任务 ID")
    allowed = set(FIELDS) | {"event_id", "actor", "time", "modules", "corrects", "supersedes"}
    if not isinstance(event_dict, dict) or set(event_dict) - allowed:
        raise ValueError("事件包含未知字段。")
    event_id = _identifier(event_dict.get("event_id", uuid.uuid4().hex), "事件 ID")
    actor = _text(event_dict.get("actor"), "actor", single_line=True)
    timestamp = _timestamp(event_dict["time"]) if "time" in event_dict else utc_now()
    body = _render_body(event_dict)
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    relations = {key: [_identifier(item, "关联事件 ID") for item in _strings(event_dict[key], key)]
                 for key in ("corrects", "supersedes") if key in event_dict}
    if any(event_id in targets for targets in relations.values()):
        raise ValueError("事件不能更正或替代自身。")
    # 未确认任务的 record 不创建项目身份或锁文件。
    project = _project(root)
    _scope(root, task_id, project["project_id"])
    with _writer_lock(root):
        project = _project(root)
        scope = _scope(root, task_id, project["project_id"])
        if set(event_dict).intersection(FIELDS) - set(scope["record_fields"]):
            raise ValueError("记录字段超出本任务已确认的范围；须重新确认后修改范围。")
        modules = _strings(event_dict.get("modules", scope["modules"]), "modules")
        if not modules or set(modules) - set(scope["modules"]):
            raise ValueError("记录模块超出本任务已确认的范围。")
        existing_paths = _matching_event_paths(root, event_id)
        if len(existing_paths) > 1:
            raise ValueError("不同日期存在重复 event_id，不能确定唯一记录；请先核验原文。")
        for existing_path in existing_paths:
            existing = _read_event(root, existing_path, project["project_id"])
            metadata = existing["metadata"]
            expected_hash = payload_hash
            if metadata.get("schema_version", 1) == 1:
                expected_hash = hashlib.sha256(_render_body(event_dict, legacy=True).encode("utf-8")).hexdigest()
            if (metadata["task_id"] == task_id and metadata["actor"] == actor and metadata["modules"] == modules
                    and metadata["payload_sha256"] == expected_hash
                    and all(metadata.get(key, []) == relations.get(key, []) for key in ("corrects", "supersedes"))
                    and ("time" not in event_dict or metadata["time"] == timestamp)):
                return {"command": "record", "status": "preserved", "event_id": event_id, "path": existing["path"]}
            raise ValueError("event_id 已存在且内容或归属不同，拒绝覆盖。")
        # References must point to existing, valid records in this project. Following
        # their ancestry catches cycles in imported metadata without scanning bodies
        # of unrelated historical records.
        _validate_new_relations(root, project["project_id"], event_id, relations)
        metadata = {"schema_version": 2, "scope_revision": scope["revision"],
                    "event_id": event_id, "project_id": project["project_id"], "task_id": task_id,
                    "actor": actor, "time": timestamp, "modules": modules, "payload_sha256": payload_hash,
                    **relations}
        first = PREFIX + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + " -->\n"
        if len(first.encode("utf-8")) > MAX_HEADER_BYTES:
            raise ValueError("事件元数据超过首行大小限制；请缩短 actor 或模块标识。正文不会被自动截断。")
        path = root / JOURNAL / timestamp[:10] / (event_id + ".md")
        _publish(root, path, (first + body).encode("utf-8"))
        return {"command": "record", "status": "created", "event_id": event_id, "path": path.relative_to(root).as_posix(),
                "note": "已保存本任务范围内的记录；长期规则候选尚未批准，不自动生效。"}


def _inspect(root: Path) -> tuple[dict | None, list, list, list, list]:
    errors, warnings, scopes, records = [], [], [], []
    project = None
    config = root / STORE / "project.json"
    try:
        store_directory = _safe(root, root / STORE, directory=True)
        # Reversible initialization/inspection commands may acquire the shared
        # lock before a task is registered. The lock alone is not project state.
        has_state = False
        if store_directory.exists():
            for path in store_directory.iterdir():
                if path.name == "store.lock":
                    _safe(root, path)
                else:
                    has_state = True
        if config.exists() or has_state:
            project = _project(root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append({"path": f"{STORE}/project.json", "message": str(exc)})
    try:
        directory = _safe(root, root / STORE, directory=True)
        if directory.exists():
            for path in directory.iterdir():
                if path.name.startswith(".tmp-"):
                    _safe(root, path)
                    warnings.append({"path": path.relative_to(root).as_posix(), "code": "incomplete_write", "message": "发现未发布的项目配置临时文件，未删除。"})
    except (OSError, ValueError) as exc:
        errors.append({"path": STORE, "message": str(exc)})
    scope_directory = root / STORE / "scopes"
    try:
        _safe(root, scope_directory, directory=True)
        if scope_directory.exists():
            for path in sorted(scope_directory.iterdir()):
                try:
                    if path.name == "history":
                        _safe(root, path, directory=True)
                        continue
                    _safe(root, path)
                    if path.name.startswith(".tmp-"):
                        warnings.append({"path": path.relative_to(root).as_posix(), "code": "incomplete_write", "message": "发现未发布的范围临时文件，未删除。"})
                        continue
                    if path.suffix != ".json":
                        warnings.append({"path": path.relative_to(root).as_posix(), "code": "unexpected_file", "message": "未知范围文件，未读取。"})
                        continue
                    _identifier(path.stem, "任务 ID")
                    if project is None:
                        raise ValueError("范围记录缺少可核验的项目身份。")
                    value = _scope(root, path.stem, project["project_id"])
                    scopes.append(value)
                    if path.stat().st_size > MAX_SCOPE_BYTES:
                        warnings.append({"path": path.relative_to(root).as_posix(), "code": "legacy_oversized_scope",
                                         "message": "旧范围超过当前上限；文件保留，请按路径读取，并将详细内容迁入计划。"})
                    if value.get("plan"):
                        target = _plan_path(root, value["plan"])
                        if not target.is_file():
                            warnings.append({"path": value["plan"], "code": "missing_plan", "message": "范围引用的当前计划已不存在；先核验迁移或归档位置。"})
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    errors.append({"path": path.relative_to(root).as_posix(), "message": str(exc)})
    except (OSError, ValueError) as exc:
        errors.append({"path": f"{STORE}/scopes", "message": str(exc)})
    history = root / STORE / "scopes" / "history"
    try:
        _safe(root, history, directory=True)
        historical_tasks = sorted(history.iterdir()) if history.exists() else []
    except (OSError, ValueError) as exc:
        errors.append({"path": history.relative_to(root).as_posix(), "message": str(exc)})
        historical_tasks = []
    for task_directory in historical_tasks:
        try:
            _safe(root, task_directory, directory=True)
            _identifier(task_directory.name, "历史任务 ID")
            historical_files = sorted(task_directory.iterdir())
        except (OSError, ValueError) as exc:
            errors.append({"path": task_directory.relative_to(root).as_posix(), "message": str(exc)})
            continue
        for path in historical_files:
            try:
                _safe(root, path)
                if path.name.startswith(".tmp-"):
                    warnings.append({"path": path.relative_to(root).as_posix(), "code": "incomplete_write", "message": "发现未发布的历史范围临时文件，未删除。"})
                    continue
                match = re.fullmatch(r"revision-([1-9]\d*)\.json", path.name)
                if match is None:
                    raise ValueError("历史范围文件名无效。")
                if project is None:
                    raise ValueError("历史范围缺少可核验的项目身份。")
                old = _validate_scope(_load_json(root, path), task_directory.name, project["project_id"])
                if old["revision"] != int(match.group(1)):
                    raise ValueError("历史范围 revision 与文件名不一致。")
                if not any(scope["task_id"] == task_directory.name for scope in scopes):
                    warnings.append({"path": path.relative_to(root).as_posix(), "code": "orphan_scope_history", "message": "仅存历史范围，当前范围缺失或损坏；未自动恢复。"})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append({"path": path.relative_to(root).as_posix(), "message": str(exc)})
    paths = _event_paths(root, errors, warnings)
    seen = set()
    task_ids = {scope["task_id"] for scope in scopes}
    for path in paths:
        try:
            if project is None:
                raise ValueError("日志缺少可核验的项目身份。")
            metadata = _read_header(root, path, project["project_id"])
            for _ in _stream_body(root, path, metadata):
                pass
            event = {"path": path.relative_to(root).as_posix(), "metadata": metadata}
            identifier = event["metadata"]["event_id"]
            if identifier in seen:
                raise ValueError("重复事件 ID，不能确定唯一记录。")
            seen.add(identifier)
            if event["metadata"]["task_id"] not in task_ids:
                raise ValueError("孤立日志：对应任务范围缺失或损坏。")
            records.append(event)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append({"path": path.relative_to(root).as_posix(), "message": str(exc)})
    errors.extend(_relationship_errors(records))
    for relative, limit, unit in (("AGENTS.md", 8192, "bytes"), ("AGENTS.md", 100, "lines"), ("DEV.md", 100, "lines")):
        path = root / relative
        try:
            _safe(root, path)
            if path.exists():
                data = path.read_bytes()
                text = data.decode("utf-8-sig")
                size = len(data) if unit == "bytes" else len(text.splitlines())
                if size > limit:
                    warnings.append({"path": relative, "code": "budget_exceeded", "size": size, "limit": limit,
                                     "unit": unit, "message": "超出建议预算；请提出保留原文的整理方案并再确认，不截断或删除。"})
        except (OSError, ValueError) as exc:
            errors.append({"path": relative, "message": str(exc)})
    return project, scopes, records, errors, warnings


def audit_store(root) -> dict:
    root = _root(root)
    project, scopes, records, errors, warnings = _inspect(root)
    archived = _archived_events(root, project["project_id"], errors) if project else set()
    if archived is not None:
        known = {item["metadata"]["event_id"] for item in records}
        for identifier in sorted(archived - known):
            errors.append({"path": f"{STORE}/journal-archive.json", "message": f"归档引用缺失或损坏事件：{identifier}"})
    return {"command": "audit", "root": str(root), "registered": project is not None,
            "project_id": project["project_id"] if project else None, "scope_count": len(scopes), "record_count": len(records),
            "archived_count": len(archived) if archived is not None else None,
            "checks_passed": not errors, "errors": errors, "warnings": warnings,
            "business_validation": "not_performed", "note": "只读核验存储身份、正文校验值、编码、关联与预算；不验证事实、不整理、不删除。"}


def _index_identity(info):
    """Identity, not mtime alone: a replaced directory must invalidate its walk."""
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _index_membership(path):
    info = path.lstat()
    return _index_identity(info), info.st_mtime_ns, info.st_ctime_ns


def _index_directory(root: Path, directory: Path):
    """Validate an enumerated directory and retain each ancestor's identity.

    Paths below are assembled solely from names returned by directory listing,
    never cache data or a caller's relative path. Reusing the lexical boundary
    avoids repeated resolve() calls without reusing stale link checks.
    """
    _safe(root, directory, directory=True)
    paths = [root]
    cursor = root
    for part in directory.relative_to(root).parts:
        cursor = cursor / part
        paths.append(cursor)
    return [(path, _index_identity(path.lstat())) for path in paths]


def _index_ancestors(ancestors):
    for directory, identity in ancestors:
        info = directory.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode) or _index_identity(info) != identity):
            raise ValueError(f"日志目录在扫描期间改变或变为链接：{directory}")


def _index_header(path: Path, project_id: str, ancestors) -> dict:
    """Read the real header after fresh boundary and opened-file checks.

    There is deliberately no persistent stat cache: preserving timestamps or
    copying a project cannot hide altered headers. Ancestors are checked again
    for every file; fstat rejects a replacement between lstat and open before
    any bytes are read. This retains the local filesystem trust boundary and
    detects more concurrent replacement cases than a check followed by read.
    """
    _index_ancestors(ancestors)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"不接受符号链接或 junction：{path}")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"路径类型不正确：{path}")
    fingerprint = (_index_identity(before), before.st_size, before.st_mtime_ns)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (_index_identity(opened), opened.st_size, opened.st_mtime_ns) != fingerprint:
            raise ValueError("日志在打开前被替换或修改，请重试恢复。")
        # An ancestor could have changed after the first check but before the
        # file lstat, making both lstat/fstat refer to the same outside file.
        # Recheck after open, before reading any content from that descriptor.
        _index_ancestors(ancestors)
        header = stream.readline(MAX_HEADER_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (_index_identity(after), after.st_size, after.st_mtime_ns) != fingerprint:
            raise ValueError("日志在读取首行期间修改，请重试恢复。")
    return _parse_metadata(path, header, project_id)


def _resume_headers(root: Path, project_id: str, task_id, module, limit: int, errors: list, warnings: list):
    """Rebuild a read-only in-memory index from every real journal header.

    Bodies remain lazy: only selected records are decoded and checksummed. The
    complete header pass must never be represented as an audit of all history.
    Validated directory boundaries are reused, but link, identity and header
    checks are fresh. No cache is written, loaded or trusted across calls.
    """
    records, seen = [], set()
    previous_error_count = len(errors)
    directory = root / JOURNAL
    journal_membership = None
    try:
        _safe(root, directory, directory=True)
        if directory.exists():
            journal_membership = _index_membership(directory)
            days = sorted(directory.iterdir())
        else:
            days = []
    except (OSError, ValueError) as exc:
        errors.append({"path": JOURNAL, "message": str(exc)})
        days = []
    for day in days:
        try:
            ancestors = _index_directory(root, day)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
                raise ValueError(f"日志日期目录无效：{day.name}")
            date.fromisoformat(day.name)
            day_membership = _index_membership(day)
            paths = sorted(day.iterdir())
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append({"path": day.relative_to(root).as_posix(), "message": str(exc)})
            continue
        for path in paths:
            relative = path.relative_to(root).as_posix()
            try:
                if path.name.startswith(".tmp-") or path.suffix != ".md":
                    # Unexpected paths still receive the full boundary/type
                    # validation, and never contribute metadata to the index.
                    _safe(root, path)
                    code = "incomplete_write" if path.name.startswith(".tmp-") else "unexpected_file"
                    message = ("发现未发布的临时文件；保留原文，须人工核验。" if code == "incomplete_write"
                               else "日志目录内有未知文件，未读取。")
                    warnings.append({"path": relative, "code": code, "message": message})
                    continue
                metadata = _index_header(path, project_id, ancestors)
                identifier = metadata["event_id"]
                if identifier in seen:
                    raise ValueError("发现重复事件 ID。")
                seen.add(identifier)
                records.append({"path": path, "metadata": metadata})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append({"path": relative, "message": str(exc)})
        try:
            if _index_membership(day) != day_membership:
                raise ValueError("日志日期目录在扫描期间改变；本次扫描不完整，请重试恢复。")
        except (OSError, ValueError) as exc:
            errors.append({"path": day.relative_to(root).as_posix(), "code": "concurrent_journal_change",
                           "message": str(exc)})
    if journal_membership is not None:
        try:
            if _index_membership(directory) != journal_membership:
                raise ValueError("日志目录在扫描期间改变；本次扫描不完整，请重试恢复。")
        except (OSError, ValueError) as exc:
            errors.append({"path": JOURNAL, "code": "concurrent_journal_change", "message": str(exc)})
    records.sort(key=lambda item: (_timestamp(item["metadata"]["time"]), item["metadata"]["event_id"]), reverse=True)
    return records, len(errors) == previous_error_count, len(records)


def _read_preview(root: Path, path: Path, metadata: dict, budget: int) -> dict:
    """Stream validation; preserve handoff sections before optional detail."""
    prefix = bytearray()
    sections, sizes = {}, {}
    section = None
    fence = None
    line = bytearray()
    continuing = False
    size = 0

    def consume(fragment, start):
        nonlocal section, fence
        if start:
            marker = re.match(rb"^ {0,3}(`{3,}|~{3,})", fragment)
            if fence:
                if re.fullmatch(rb" {0,3}" + re.escape(fence[:1]) + rb"{" + str(len(fence)).encode() + rb",}[ \t\r\n]*", fragment):
                    fence = None
            elif marker:
                fence = marker[1]
            elif fragment.startswith(b"## "):
                heading = fragment.decode("utf-8", errors="ignore").strip()
                section = next((key for key, label in LABELS.items() if heading == "## " + label), None)
        if section:
            sizes[section] = sizes.get(section, 0) + len(fragment)
            saved = sections.setdefault(section, bytearray())
            if len(saved) < budget:
                saved.extend(fragment[:budget - len(saved)])

    for chunk in _stream_body(root, path, metadata):
        size += len(chunk)
        if len(prefix) < budget:
            prefix.extend(chunk[:budget - len(prefix)])
        for fragment in chunk.splitlines(keepends=True):
            line.extend(fragment)
            if fragment.endswith(b"\n") or len(line) >= 4096:
                consume(bytes(line), not continuing)
                continuing = not fragment.endswith(b"\n")
                line.clear()
    if line:
        consume(bytes(line), not continuing)
    truncated = size > budget
    covered, incomplete = [], []
    omitted_bytes = 0
    if not truncated:
        markdown = bytes(prefix).decode("utf-8")
        preview_format = "full"
        covered = list(sections)
    else:
        marker = "[…正文节选；详细证据按需读取原文…]\n\n"
        available = budget - len(marker.encode("utf-8"))
        chosen = [key for key in ("verification", "next", "changed", "fix", "pitfall", "long_term_candidates") if key in sections]
        excerpts = []
        used_content = 0
        if available >= 64 and chosen:
            # Include complete critical sections whenever they fit together;
            # spare space then carries the optional change/evidence excerpts.
            for index, key in enumerate(chosen):
                critical_left = [item for item in chosen[index:] if item in ("verification", "next")]
                if key in critical_left:
                    allocation = (sizes[key] if sum(sizes[item] for item in critical_left) <= available
                                  else available // len(critical_left))
                else:
                    allocation = available if index == len(chosen) - 1 else max(0, available // (len(chosen) - index))
                content = bytes(sections[key][:allocation]).decode("utf-8", errors="ignore")
                if content:
                    excerpts.append(content)
                    consumed = len(content.encode("utf-8"))
                    available -= consumed
                    used_content += consumed
                if len(content.encode("utf-8")) >= sizes[key]:
                    covered.append(key)
                else:
                    incomplete.append(key)
            markdown = marker + "".join(excerpts)
            preview_format = "handoff_sections"
        else:
            markdown = ""
            preview_format = "omitted"
            incomplete = list(sections)
        omitted_bytes = max(0, size - used_content)
    # Missing source details are optional unless the handoff's verification or
    # next step itself was omitted. Do not force all history into every session.
    critical = {key for key in ("verification", "next") if key in sections}
    read_required = truncated and (not critical or not critical.issubset(covered))
    return {"path": path.relative_to(root).as_posix(), "metadata": metadata, "markdown": markdown,
            "full_body_bytes": size, "returned_body_bytes": len(markdown.encode("utf-8")),
            "content_truncated": truncated, "read_required": read_required, "preview_format": preview_format,
            "covered_sections": covered, "incomplete_sections": incomplete,
            "omitted_body_bytes": omitted_bytes, "body_checksum_verified": True}


def _archived_events(root, project_id, errors):
    path = root / STORE / "journal-archive.json"
    try:
        _safe(root, path)
        if not path.exists():
            return set()
        value = _load_json(root, path)
        if (type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or value.get("project_id") != project_id
                or type(value.get("revision")) is not int or value["revision"] < 1
                or not isinstance(value.get("events"), dict)):
            raise ValueError("归档清单版本、项目身份或事件表无效。")
        for identifier, entry in value["events"].items():
            if not isinstance(entry, dict):
                raise ValueError("归档事件条目无效。")
            _identifier(entry.get("review_id"), "归档提案 ID")
            _timestamp(entry.get("archived_at"))
        return {_identifier(identifier, "归档事件 ID") for identifier in value["events"]}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append({"path": path.relative_to(root).as_posix(), "message": str(exc)})
        return None


def _scope_summary(value):
    summary = {key: value[key] for key in ("schema_version", "task_id", "project_id", "revision", "updated_at", "plan", "task_status", "resolved_task_state") if key in value}
    summary.update({"path": f"{STORE}/scopes/{value['task_id']}.json", "content_omitted": True,
                    "read_required": True})
    return summary


def _fit_output(report, budget):
    """Bound the exact pretty-printed JSON, retaining counts when details are lost."""
    report["output_truncated"] = False
    report["output_budget_bytes"] = budget
    report["output_bytes"] = 0
    report["output_omitted_records"] = 0
    report["output_omitted_scopes"] = 0
    report["relationship_count"] = len(report["relationships"])
    report["output_omitted_relationships"] = 0
    report["error_count"] = len(report["errors"])
    report["warning_count"] = len(report["warnings"])

    def fits():
        for _ in range(3):
            report["output_bytes"] = len(_json_bytes(report))
        return report["output_bytes"] <= budget

    if fits():
        return report
    report["output_truncated"] = True
    # First remove optional text, then task detail; never silently imply a whole
    # response or a complete body when the output envelope forced an omission.
    for record in reversed(report["records"]):
        record.update(markdown="", returned_body_bytes=0, content_truncated=True,
                      read_required=True, preview_format="omitted", covered_sections=[],
                      omitted_body_bytes=record["full_body_bytes"])
        report["content_truncated"] = True
        report["required_reads"] = [item["path"] for item in report["records"] if item["read_required"]]
        if fits():
            return report
    report["scopes"] = [_scope_summary(value) for value in report["scopes"]]
    if fits():
        return report
    for field in ("relationships", "warnings", "errors"):
        while report[field] and not fits():
            report[field].pop()
            if field == "relationships":
                report["output_omitted_relationships"] += 1
    report["optional_reads"] = []
    while report["records"] and not fits():
        report["records"].pop()
        report["output_omitted_records"] += 1
        report["required_reads"] = [item["path"] for item in report["records"] if item["read_required"]]
        report["has_more"] = True
    while report["scopes"] and not fits():
        report["scopes"].pop()
        report["output_omitted_scopes"] += 1
        report["has_more"] = True
    if fits():
        return report
    # Even unusually long roots/diagnostics cannot breach the transport budget.
    minimal = {key: report[key] for key in ("command", "project_id", "has_more", "record_count", "scope_count",
                "output_omitted_records", "output_omitted_scopes", "relationship_count", "output_omitted_relationships",
                "error_count", "warning_count", "output_budget_bytes")}
    minimal.update(scopes=[], records=[], errors=[], warnings=[], output_truncated=True,
                   output_bytes=0, business_validation="not_performed", content_truncated=True,
                   read_hint="Increase --max-output-bytes; select --task/--limit. Sources: .ai-memory/scopes and journal.")
    for _ in range(3):
        minimal["output_bytes"] = len(_json_bytes(minimal))
    return minimal


def resume_memory(root, task_id=None, module=None, limit=10, max_bytes=16384,
                  max_output_bytes=32768, include_archived=False, client=None, session=None) -> dict:
    root = _root(root)
    if task_id is not None:
        task_id = _identifier(task_id, "任务 ID")
    if type(limit) is not int or limit < 1 or limit > 1000:
        raise ValueError("limit 必须是 1–1000。")
    if type(max_bytes) is not int or max_bytes < 1 or max_bytes > 10 * 1024 * 1024:
        raise ValueError("max_bytes 必须是 1–10485760，限制返回的 Markdown 正文字节总数。")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 10 * 1024 * 1024:
        raise ValueError("max_output_bytes 必须是 1024–10485760，限制包含换行的完整 JSON 响应。")
    if (client is None) != (session is None):
        raise ValueError("client 和 session 须同时提供；仅按模块查询时均不填写。")
    errors, warnings, scopes, records = [], [], [], []
    project = None
    selection = {"task_id": task_id, "selection_required": False}
    try:
        project = _project(root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append({"path": f"{STORE}/project.json", "message": str(exc)})
    headers, candidates, relationships = [], [], []
    scope_scan_complete = True
    archived = set()
    scanned_all = True
    scope_count = 0
    effective_task = task_id
    blocked_selection = False
    if project:
        try:
            from memory_workflow import resolve_selection
            if module is not None and task_id is None and client is None and session is None:
                # An explicit module query spans tasks, including paused/completed
                # ones. Automatic task selection must not narrow its history.
                selection = {"task_id": None, "selection_required": False, "source": "module_filter",
                             "errors": [], "warnings": [], "candidates": [], "scan_complete": True}
            else:
                selection = resolve_selection(root, task_id=task_id, client=client, session=session)
            errors.extend(selection.get("errors", []))
            warnings.extend(selection.get("warnings", []))
            blocked_selection = any(item.get("code") in ("invalid_binding", "invalid_task")
                                    for item in selection.get("errors", []))
            if selection.get("task_id"):
                effective_task = selection["task_id"]
        except ImportError:
            if client or session:
                raise ValueError("会话选择模块不可用。")
        # Task discovery is independent of journal selection, including tasks that
        # have never produced an event. Stable metadata dates replace file mtime.
        scope_dir = root / STORE / "scopes"
        try:
            _safe(root, scope_dir, directory=True)
            scope_paths = ([] if blocked_selection else [scope_dir / (effective_task + ".json")]
                           if effective_task else sorted(scope_dir.glob("*.json")))
            for path in scope_paths:
                try:
                    _safe(root, path)
                    _identifier(path.stem, "任务 ID")
                    value = _scope(root, path.stem, project["project_id"])
                    if module is not None and module not in value["modules"]:
                        continue
                    if value.get("plan"):
                        target = _plan_path(root, value["plan"])
                        if not target.is_file():
                            warnings.append({"path": value["plan"], "code": "missing_plan", "message": "当前计划路径不存在。"})
                    try:
                        from memory_workflow import task_state
                        value = {**value, "resolved_task_state": task_state(root, value)}
                    except ImportError:
                        pass
                    if path.stat().st_size > MAX_SCOPE_BYTES:
                        warnings.append({"path": path.relative_to(root).as_posix(), "code": "legacy_oversized_scope", "message": "旧范围超限，保留原文；按路径补读并将详情放入计划。"})
                        value = _scope_summary(value)
                    scopes.append(value)
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    scope_scan_complete = False
                    errors.append({"path": path.relative_to(root).as_posix(), "message": str(exc)})
        except (OSError, ValueError) as exc:
            scope_scan_complete = False
            errors.append({"path": f"{STORE}/scopes", "message": str(exc)})
        scopes.sort(key=lambda value: (_timestamp(value["updated_at"]), value["task_id"]), reverse=True)
        scope_count = len(scopes)
        valid_tasks = {value["task_id"] for value in scopes}
        headers, scanned_all, _ = _resume_headers(root, project["project_id"], task_id, module, limit, errors, warnings)
        relation_records = [{"path": item["path"].relative_to(root).as_posix(), "metadata": item["metadata"]} for item in headers]
        relation_errors = _relationship_errors(relation_records)
        errors.extend(relation_errors)
        archived = _archived_events(root, project["project_id"], errors)
        # Invalid archive state cannot silently make archived assertions current.
        if archived is not None and not blocked_selection:
            for item in headers:
                metadata = item["metadata"]
                for relation in ("corrects", "supersedes"):
                    for target in metadata.get(relation, []):
                        relationships.append({"event_id": metadata["event_id"], "relation": relation,
                                              "target": target, "archived": metadata["event_id"] in archived,
                                              "body_checksum_verified": False,
                                              "path": item["path"].relative_to(root).as_posix()})
                if effective_task and metadata["task_id"] != effective_task:
                    continue
                if module is not None and module not in metadata["modules"]:
                    continue
                if metadata["task_id"] not in valid_tasks:
                    errors.append({"path": item["path"].relative_to(root).as_posix(), "message": "日志对应范围缺失、损坏或不匹配。"})
                    continue
                if not include_archived and metadata["event_id"] in archived:
                    continue
                candidates.append(item)
        selected = candidates[:limit]
        remaining = max_bytes
        for index, item in enumerate(selected):
            try:
                record = _read_preview(root, item["path"], item["metadata"], remaining // (len(selected) - index))
                remaining -= record["returned_body_bytes"]
                record["archived"] = item["metadata"]["event_id"] in (archived or set())
                record["related_by"] = [edge for edge in relationships if edge["target"] == item["metadata"]["event_id"]]
                record["historical_scope_revision"] = item["metadata"].get("scope_revision", "unknown")
                records.append(record)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append({"path": item["path"].relative_to(root).as_posix(), "message": str(exc)})
    scope_omitted = max(0, len(scopes) - limit)
    omitted = max(0, len(candidates) - limit)
    report = {"command": "resume", "root": str(root), "project_id": project["project_id"] if project else None,
              "selection": selection, "scopes": scopes[:limit], "records": records,
              "scope_count": scope_count, "record_count": len(candidates),
              "omitted_count": omitted if scanned_all else None, "omitted_at_least": omitted,
              "scope_omitted_count": scope_omitted if scope_scan_complete else None,
              "scope_omitted_at_least": scope_omitted, "scope_scan_complete": scope_scan_complete,
              "has_more": bool(omitted or scope_omitted) or not scanned_all or not scope_scan_complete,
              "scan_complete": scanned_all, "scanned_header_count": len(headers), "history_audited": False,
              "include_archived": include_archived, "archived_count": len(archived) if archived is not None else None,
              "relationships": relationships, "markdown_budget_bytes": max_bytes,
              "content_truncated": any(record["content_truncated"] for record in records),
              "required_reads": [record["path"] for record in records if record["read_required"]],
              "optional_reads": [record["path"] for record in records if record["content_truncated"] and not record["read_required"]],
              "errors": errors, "warnings": warnings, "business_validation": "not_performed",
              "note": "只读重建内存索引，不写入或信任持久缓存；仅校验选中正文，完整首行扫描不是历史审计。优先阅读验证及下一步；省略证据按需补读。更正与替代是待核对的关系，不自动删除历史或批准长期规则。"}
    return _fit_output(report, max_output_bytes)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # Applies through ai_memory.py's delegated entrypoint as well on Windows.
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scope", "record", "resume", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
        if name in ("scope", "record"):
            command.add_argument("--task", required=True)
            command.add_argument("--file", required=True)
        elif name == "resume":
            command.add_argument("--task")
            command.add_argument("--module")
            command.add_argument("--limit", type=int, default=10)
            command.add_argument("--max-bytes", type=int, default=16384)
            command.add_argument("--max-output-bytes", type=int, default=32768)
            command.add_argument("--include-archived", action="store_true")
            command.add_argument("--client")
            command.add_argument("--session")
        if name == "scope":
            command.add_argument("--confirmed", action="store_true")
            command.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command in ("scope", "record"):
            # 请求文件由调用者明确指定；仅读取这个 JSON，不扫描外部项目资料。
            payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            if args.command == "scope":
                result = configure_scope(args.root, args.task, payload, args.confirmed, args.expected_revision)
            else:
                result = record_event(args.root, args.task, payload)
        elif args.command == "resume":
            result = resume_memory(args.root, args.task, args.module, args.limit, args.max_bytes,
                                   args.max_output_bytes, args.include_archived, args.client, args.session)
        else:
            result = audit_store(args.root)
        status = 1 if result.get("errors") or result.get("error_count") else 0
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result = {"command": args.command, "error": str(exc), "business_validation": "not_performed"}
        status = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
