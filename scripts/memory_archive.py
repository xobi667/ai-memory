"""Explicit, reversible journal visibility snapshots; never move journal bytes."""
from __future__ import annotations

import hashlib
from pathlib import Path

from memory_common import atomic_write, json_bytes, project_lock, read_json, safe_path
from memory_admin import identity, valid_task_id, stamp, NOTE, digest, create_exact

MANIFEST = ".ai-memory/journal-archive.json"


def load_manifest(root: Path, project_id: str) -> dict:
    path = safe_path(root, root / MANIFEST)
    if not path.exists():
        return {"schema_version": 1, "project_id": project_id, "revision": 0, "events": {}}
    value = read_json(root, path)
    if (type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or value.get("project_id") != project_id
            or type(value.get("revision")) is not int or value["revision"] < 1
            or not isinstance(value.get("events"), dict)):
        raise ValueError("日志归档清单版本、身份或格式无效。")
    for identifier, item in value["events"].items():
        valid_task_id(identifier)
        if not isinstance(item, dict):
            raise ValueError("日志归档条目无效。")
        valid_task_id(item.get("review_id", ""))
        from memory_store import _timestamp
        _timestamp(item.get("archived_at"))
    return value


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def event_snapshot(root: Path, project_id: str, task: str | None, event_ids: list[str] | None) -> list[dict]:
    from memory_store import _event_paths, _read_header, _stream_body
    errors, warnings, selected, seen = [], [], [], set()
    wanted = set(event_ids or [])
    for identifier in wanted:
        valid_task_id(identifier)
    for path in _event_paths(root, errors, warnings):
        meta = _read_header(root, path, project_id)
        if meta["event_id"] in seen:
            raise ValueError("事件 ID 重复；先修复完整性问题。")
        seen.add(meta["event_id"])
        if (task and meta["task_id"] != task) or (wanted and meta["event_id"] not in wanted):
            continue
        for _ in _stream_body(root, path, meta):
            pass
        selected.append({"event_id": meta["event_id"], "path": path.relative_to(root).as_posix(),
                         "sha256": file_digest(path)})
    if errors:
        raise ValueError(f"日志扫描不完整：{errors[0]}")
    if wanted - {item["event_id"] for item in selected}:
        raise ValueError("指定事件不存在或不属于所选任务。")
    return selected


def paths(root: Path, review_id: str) -> dict:
    valid_task_id(review_id)
    base = root / ".ai-memory/reviews" / review_id
    return {name: safe_path(root, base / name) for name in
            ("proposal.json", "before.json", "after.json", "pending.json", "audit.json")}


def propose_archive(root: Path, review_id: str, kind: str, reason: str,
                    task: str | None = None, event_ids: list[str] | None = None) -> dict:
    if kind not in ("journal-archive", "journal-unarchive") or not reason.strip():
        raise ValueError("需要合法的日志归档类型与非空原因。")
    if not task and not event_ids:
        raise ValueError("指定 --task 或 --event，提案只冻结当前明确的事件集合。")
    if task:
        valid_task_id(task)
    project_id, files = identity(root), paths(root, review_id)
    seal = safe_path(root, root / f".ai-memory/review-seals/{review_id}.json")
    with project_lock(root):
        if any(p.exists() for p in [*files.values(), seal]):
            raise ValueError("提案 ID 已存在，请使用新 ID。")
        if identity(root) != project_id:
            raise ValueError("项目标识已变化。")
        before = load_manifest(root, project_id)
        snapshot = event_snapshot(root, project_id, task, event_ids)
        archived = before["events"]
        snapshot = [item for item in snapshot if (item["event_id"] in archived) == (kind == "journal-unarchive")]
        if not snapshot:
            raise ValueError("没有需要改变归档状态的事件。")
        now = stamp()
        events = dict(archived)
        for item in snapshot:
            if kind == "journal-archive":
                events[item["event_id"]] = {"review_id": review_id, "archived_at": now}
            else:
                events.pop(item["event_id"])
        after = {**before, "revision": before["revision"] + 1, "events": events}
        manifest = safe_path(root, root / MANIFEST)
        original = manifest.read_bytes() if manifest.exists() else b""
        proposal = {"schema_version": 1, "id": review_id, "kind": kind, "project_id": project_id,
                    "reason": reason.strip(), "created_at": now, "events": snapshot,
                    "manifest_existed": manifest.exists(), "before_sha256": digest(original),
                    "after_sha256": digest(json_bytes(after)), "confirmation_notice": NOTE}
        encoded = json_bytes(proposal)
        create_exact(root, files["before.json"], original)
        create_exact(root, files["after.json"], json_bytes(after))
        create_exact(root, files["proposal.json"], encoded)
        create_exact(root, seal, json_bytes({"id": review_id, "project_id": project_id, "proposal_sha256": digest(encoded)}))
    return {"command": "propose", "kind": kind, "id": review_id, "event_count": len(snapshot),
            "proposal": str(files["proposal.json"]), "source_modified": False,
            "note": "只改变默认恢复的可见范围；原始日志路径和内容不变，不减少磁盘占用。"}


def apply_archive(root: Path, review_id: str, confirmed: bool, confirmation: str | None) -> dict:
    if not confirmed or not confirmation or not confirmation.strip():
        raise ValueError("需要 --confirmed 和非空 --confirmation。")
    project_id, files = identity(root), paths(root, review_id)
    with project_lock(root):
        encoded = files["proposal.json"].read_bytes()
        proposal = read_json(root, files["proposal.json"])
        seal = read_json(root, root / f".ai-memory/review-seals/{review_id}.json")
        if (type(proposal.get("schema_version")) is not int or proposal.get("schema_version") != 1 or proposal.get("id") != review_id
                or proposal.get("kind") not in ("journal-archive", "journal-unarchive")
                or proposal.get("project_id") != project_id or identity(root) != project_id
                or seal != {"id": review_id, "project_id": project_id, "proposal_sha256": digest(encoded)}):
            raise ValueError("项目标识或提案内容已变化。")
        before, after = files["before.json"].read_bytes(), files["after.json"].read_bytes()
        if digest(before) != proposal["before_sha256"] or digest(after) != proposal["after_sha256"]:
            raise ValueError("归档提案快照已变化。")
        manifest = safe_path(root, root / MANIFEST)
        current = manifest.read_bytes() if manifest.exists() else None
        expected = before if proposal["manifest_existed"] else None
        receipt = read_json(root, files["pending.json"]) if files["pending.json"].exists() else None
        if receipt and (receipt.get("proposal_sha256") != digest(encoded) or receipt.get("project_id") != project_id
                        or receipt.get("id") != review_id or not receipt.get("confirmation")):
            raise ValueError("归档待完成记录已变化。")
        if files["audit.json"].exists():
            if not receipt or read_json(root, files["audit.json"]) != receipt or current != after:
                raise ValueError("已应用归档状态发生变化，请通过新的反向提案处理。")
            return {"command": "apply-review", "id": review_id, "already_applied": True,
                    "audit": str(files["audit.json"])}
        if current != expected and not (receipt and current == after):
            raise ValueError("归档清单已变化，请重新提案。")
        for item in proposal["events"]:
            path = safe_path(root, root / item["path"])
            if not path.is_file() or file_digest(path) != item["sha256"]:
                raise ValueError("事件原文已变化，请重新审阅归档提案。")
        if receipt is None:
            receipt = {"id": review_id, "project_id": project_id, "kind": proposal["kind"],
                       "proposal_sha256": digest(encoded), "confirmation": confirmation.strip(),
                       "confirmation_type": "caller_declared", "confirmed_at": stamp(), "notice": NOTE}
            create_exact(root, files["pending.json"], json_bytes(receipt))
        if current != after:
            atomic_write(root, manifest, after, replace=manifest.exists())
        create_exact(root, files["audit.json"], json_bytes(receipt))
    return {"command": "apply-review", "id": review_id, "already_applied": False,
            "event_count": len(proposal["events"]), "audit": str(files["audit.json"]),
            "note": "原始日志未移动、未改写；反向操作使用 journal-unarchive / journal-archive 新提案。"}
