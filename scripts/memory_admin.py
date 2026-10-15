#!/usr/bin/env python3
"""只读盘点与需显式确认的记忆整理。确认是调用者声明，不验证真人身份。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import uuid
from urllib.parse import quote

from ai_memory import is_reparse, project_root, safe_path, valid_task_id
from memory_common import project_lock


CORE = {"agents.md", "claude.md", "readme.md", "dev.md", "arch.md", "architecture.md", "index.md"}
NOTE = "确认仅记录调用者通过 --confirmed 和 --confirmation 作出的声明；本工具不能验证真人授权。"


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("路径必须是字符串。")
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ":" in value:
        raise ValueError("必须提供项目内的相对路径。")
    parts = value.split("/")
    if any(not part or part in (".", "..") or part.endswith((" ", ".")) for part in parts):
        raise ValueError("路径不能包含空段、.、.. 或尾随空格/点。")
    if any(re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts):
        raise ValueError("路径不能使用 Windows 保留设备名。")
    return path.as_posix()


def checked(root: Path, relative: str, *, directory: bool = False) -> Path:
    path = root / relative_path(relative)
    safe_path(root, path, directory=directory)
    return path


def load_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError(f"预期 JSON 对象：{path}")
    return result


def identity(root: Path) -> str:
    path = checked(root, ".ai-memory/project.json")
    if not path.is_file():
        raise ValueError("缺少 .ai-memory/project.json；先接入项目记忆，再创建整理提案。")
    marker = load_json(path)
    if type(marker.get("schema_version")) is not int or marker.get("schema_version") != 1:
        raise ValueError("项目标记 schema_version 必须为 1。")
    value = marker.get("project_id")
    if not isinstance(value, str):
        raise ValueError("项目标记 project_id 必须是合法 UUID 字符串。")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("项目标记 project_id 必须是合法 UUID 字符串。") from exc
    return value


def budget(relative: str, data: bytes) -> list[str]:
    lines = len(data.decode("utf-8-sig").splitlines())
    name = PurePosixPath(relative).name.casefold()
    messages = []
    if name == "agents.md" and (len(data) > 8192 or lines > 100):
        messages.append(f"AGENTS 超出 8 KiB / 100 行预算（{len(data)} 字节，{lines} 行）；建议经确认后把细节移入专题文档并保留链接，绝不直接截断。")
    if name == "dev.md" and lines > 100:
        messages.append(f"DEV 超出 100 行预算（{lines} 行）；建议经确认后把历史移入归档并保留当前任务入口，绝不直接截断。")
    return messages


def inspect(root: Path) -> dict:
    """只检查已知入口及浅层标记；不写文件、不遍历依赖或业务内容。"""
    if not root.is_dir():
        raise ValueError("盘点的项目目录不存在。")
    warnings: list[dict] = []
    documents = []
    candidates = {"AGENTS.md", "CLAUDE.md", "README.md", "DEV.md", "ARCH.md", "ARCHITECTURE.md", "INDEX.md", "docs/INDEX.md", "docs/ARCH.md", "docs/ARCHITECTURE.md"}
    for folder in (root, root / "docs"):
        if folder != root:
            try:
                safe_path(root, folder, directory=True)
            except (OSError, ValueError) as exc:
                warnings.append({"path": "docs", "message": str(exc)})
                continue
        if folder.is_dir():
            candidates.update(p.relative_to(root).as_posix() for p in folder.iterdir() if p.name.casefold() in CORE)
    for relative in sorted(candidates):
        try:
            path = checked(root, relative)
            if not path.is_file():
                continue
            data = path.read_bytes()
            text = data.decode("utf-8-sig")
            documents.append({"path": relative, "bytes": len(data), "lines": len(text.splitlines()), "sha256": digest(data)})
            warnings.extend({"path": relative, "message": item} for item in budget(relative, data))
        except (OSError, ValueError) as exc:
            warnings.append({"path": relative, "message": str(exc)})
    markers = []
    frameworks = []
    for relative, label in {
        "package.json": "Node", "pyproject.toml": "Python", "requirements.txt": "Python",
        "Cargo.toml": "Rust", "go.mod": "Go", "pom.xml": "Java", "build.gradle": "Gradle",
        "build.gradle.kts": "Gradle", "settings.gradle.kts": "Gradle", ".git": "Git",
    }.items():
        path = root / relative
        try:
            safe_path(root, path, directory=path.is_dir())
            if path.exists():
                markers.append({"path": relative, "type": label})
        except (OSError, ValueError) as exc:
            warnings.append({"path": relative, "message": str(exc)})
    for relative, label in {
        ".planning": "planning-with-files 或 GSD（需进一步确认）",
        ".planning/STATE.md": "GSD", "task_plan.md": "planning-with-files",
        "openspec": "OpenSpec", ".beads": "Beads", ".specify": "Spec Kit",
        ".superpowers": "Superpowers", "docs/superpowers": "Superpowers",
    }.items():
        path = root / relative
        try:
            safe_path(root, path, directory=path.is_dir())
            if path.exists():
                frameworks.append({"path": relative, "framework": label})
        except (OSError, ValueError) as exc:
            warnings.append({"path": relative, "message": str(exc)})
    project_id = None
    try:
        marker = checked(root, ".ai-memory/project.json")
        if marker.exists():
            project_id = identity(root)
        lock = checked(root, ".ai-memory/admin.lock")
        if lock.exists():
            warnings.append({"path": ".ai-memory/admin.lock", "message": "存在整理锁，可能有进程正在操作或上次进程中断。不会自动解锁；确认持有进程已结束后再人工修复。"})
    except (OSError, ValueError) as exc:
        warnings.append({"path": ".ai-memory", "message": str(exc)})
    return {"command": "inspect", "read_only": True, "root": str(root), "project_id": project_id,
            "documents": documents, "project_markers": markers, "framework_markers": frameworks,
            "warnings": warnings, "note": "标记仅代表文件存在，不证明框架已正确安装；不裁剪文档，不运行项目验证。"}


@contextmanager
def exclusive_lock(root: Path):
    with project_lock(root):
        yield


def create_exact(root: Path, path: Path, data: bytes) -> None:
    """排他写入；重试只接受字节完全相同的已存在文件。"""
    safe_path(root, path)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"现有文件内容不同，拒绝覆盖：{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, path)
    # Windows rename 原子且拒绝覆盖；POSIX 用 hard-link 保持同样的 no-clobber 语义。
    fd, name = tempfile.mkstemp(prefix=".ai-memory-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_path(root: Path, value: str) -> tuple[str, Path]:
    relative = relative_path(value)
    lowered = relative.casefold()
    if lowered.startswith("docs/memory/journal/"):
        raise ValueError("受管理 journal 日志仅追加，不能改写或归档；更正请追加后续记录，本命令不支持迁移日志。")
    if not lowered.endswith(".md") or lowered.startswith((".ai-memory/", "docs/memory/archive/")):
        raise ValueError("源文件必须是项目内 Markdown，且不能是内部状态或历史备份。")
    return relative, checked(root, relative)


def validate_kind(kind: str, relative: str, exists: bool) -> None:
    if kind not in {"rule", "compact", "archive"}:
        raise ValueError("未知提案类型。")
    if not exists and not (kind == "rule" and relative.casefold().startswith("docs/memory/rules/")):
        raise ValueError("仅 rule 提案可在 docs/memory/rules/ 下新建文件。")
    if kind == "archive" and PurePosixPath(relative).name.casefold() in CORE:
        raise ValueError("不能归档核心入口；请提议精简并保留入口职责。")


def review_paths(root: Path, review_id: str) -> dict[str, Path]:
    valid_task_id(review_id)
    base = f".ai-memory/reviews/{review_id}"
    result = {name: checked(root, f"{base}/{name}") for name in
              ("proposal.json", "before.md", "after.md", "pending.json", "audit.json", "audit.md")}
    result["seal"] = checked(root, f".ai-memory/review-seals/{review_id}.json")
    checked(root, base, directory=True)
    return result


def propose(root: Path, review_id: str, kind: str, source: str, replacement_file: str | None, reason: str) -> dict:
    project_id = identity(root)
    paths = review_paths(root, review_id)
    relative, source_file = source_path(root, source)
    exists = source_file.exists()
    validate_kind(kind, relative, exists)
    if not reason.strip():
        raise ValueError("提案原因不能为空。")
    before = source_file.read_bytes() if exists else b""
    before.decode("utf-8-sig")
    created_at = stamp()
    archive = f"docs/memory/archive/{created_at[:10]}/{review_id}/{relative}"
    checked(root, archive)
    if kind == "archive":
        if replacement_file:
            raise ValueError("archive 使用固定历史入口，不接收 replacement-file。")
        link = quote(os.path.relpath(root / archive, source_file.parent).replace("\\", "/"), safe="/")
        after = f"# 已归档：{source_file.stem}\n\n[查看历史原文]({link})\n\n旧版仅供追溯，不作为现行规则；本文件保留历史入口。\n".encode("utf-8")
    else:
        if not replacement_file:
            raise ValueError("rule / compact 需要 --replacement-file 提供完整替换稿；工具不自动总结。")
        replacement = Path(replacement_file).expanduser().absolute()
        if is_reparse(replacement) or not replacement.is_file():
            raise ValueError("替换稿必须是普通文件，不能是链接。")
        text = replacement.read_bytes().decode("utf-8-sig")
        if not text.strip():
            raise ValueError("替换稿不能为空。")
        if exists:
            link = quote(os.path.relpath(root / archive, source_file.parent).replace("\\", "/"), safe="/")
            text = text.rstrip() + f"\n\n---\n历史原文：[本次修改前的版本]({link})。旧版仅供追溯，不作为现行规则。\n"
        after = text.encode("utf-8")
    proposal = {"schema": 1, "id": review_id, "project_id": project_id, "kind": kind,
                "source": relative, "source_existed": exists, "source_sha256": digest(before) if exists else None,
                "before_sha256": digest(before), "after_sha256": digest(after), "archive": archive,
                "reason": reason.strip(), "created_at": created_at, "confirmation_notice": NOTE}
    encoded = json_bytes(proposal)
    # 全部目标均先预检，再拿项目级锁并重新检查源 CAS。
    with exclusive_lock(root):
        if identity(root) != project_id:
            raise ValueError("项目标识已变化。")
        if any(path.exists() for path in paths.values()):
            raise ValueError("该提案 ID 已存在，请使用新的 ID；不会覆盖已有提案。")
        safe_path(root, source_file)
        current = source_file.read_bytes() if source_file.exists() else None
        if current != (before if exists else None):
            raise ValueError("读取后源文件已变化，请重新创建提案。")
        create_exact(root, paths["before.md"], before)
        create_exact(root, paths["after.md"], after)
        create_exact(root, paths["proposal.json"], encoded)
        create_exact(root, paths["seal"], json_bytes({"project_id": project_id, "id": review_id, "proposal_sha256": digest(encoded)}))
    return {"command": "propose", "id": review_id, "proposal": str(paths["proposal.json"]),
            "before": str(paths["before.md"]), "after": str(paths["after.md"]), "source_modified": False,
            "warnings": budget(relative, after), "confirmation_notice": NOTE}


def audit_markdown(receipt: dict) -> bytes:
    # JSON 代码块保留调用者原文，避免确认文字被当成审计文档结构。
    return ("# 整理确认记录\n\n" + NOTE + "\n\n```json\n" +
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n").encode("utf-8")


def apply_review(root: Path, review_id: str, confirmed: bool, confirmation: str | None) -> dict:
    if not confirmed or not confirmation or not confirmation.strip():
        raise ValueError("应用需要显式 --confirmed 和非空 --confirmation；不会推断用户同意。")
    project_id = identity(root)
    paths = review_paths(root, review_id)
    kind = load_json(paths["proposal.json"]).get("kind")
    if kind == "organize":
        from memory_documents import apply_organize
        return apply_organize(root, review_id, confirmed, confirmation)
    if kind in ("journal-archive", "journal-unarchive"):
        from memory_archive import apply_archive
        return apply_archive(root, review_id, confirmed, confirmation)
    with exclusive_lock(root):
        encoded = paths["proposal.json"].read_bytes()
        proposal = load_json(paths["proposal.json"])
        seal = load_json(paths["seal"])
        if (type(proposal.get("schema")) is not int or proposal.get("schema") != 1 or proposal.get("id") != review_id or
                proposal.get("project_id") != project_id or identity(root) != project_id or
                seal != {"id": review_id, "project_id": project_id, "proposal_sha256": digest(encoded)}):
            raise ValueError("项目标识或提案内容已变化，拒绝应用。")
        relative, target = source_path(root, proposal["source"])
        existed = proposal.get("source_existed")
        if not isinstance(existed, bool):
            raise ValueError("提案源状态无效。")
        validate_kind(proposal.get("kind"), relative, existed)
        before, after = paths["before.md"].read_bytes(), paths["after.md"].read_bytes()
        before.decode("utf-8-sig")
        after.decode("utf-8-sig")
        if (digest(before) != proposal.get("before_sha256") or digest(after) != proposal.get("after_sha256") or
                proposal.get("source_sha256") != (digest(before) if existed else None)):
            raise ValueError("提案原文或替换稿已变化，拒绝应用。")
        warnings = budget(relative, after)
        if PurePosixPath(relative).name.casefold() in {"agents.md", "dev.md"} and warnings:
            raise ValueError(warnings[0])
        archive_relative = relative_path(proposal["archive"])
        archive_parts = PurePosixPath(archive_relative).parts
        if (len(archive_parts) < 6 or archive_parts[:3] != ("docs", "memory", "archive") or
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", archive_parts[3]) or archive_parts[4] != review_id or
                PurePosixPath(*archive_parts[5:]).as_posix() != relative):
            raise ValueError("提案备份路径无效。")
        backup = checked(root, archive_relative)
        notice = checked(root, "/".join(archive_parts[:5]) + "/.archive-notice.txt")
        notice_data = "本目录保存历史原文，仅供恢复和追溯；不作为现行规则。备份按原始字节保存。\n".encode("utf-8")
        # 在创建备份或修改源之前，预检所有可能写入的位置和已存在内容。
        for path in paths.values():
            safe_path(root, path)
        for path, data in ((backup, before), (notice, notice_data)) if existed else ():
            safe_path(root, path)
            if path.exists() and path.read_bytes() != data:
                raise ValueError(f"备份目标已存在且内容不同：{path}")
        safe_path(root, target)
        current = target.read_bytes() if target.exists() else None
        expected = before if existed else None
        receipt = None
        if paths["pending.json"].exists():
            receipt = load_json(paths["pending.json"])
            if (receipt.get("proposal_sha256") != digest(encoded) or receipt.get("project_id") != project_id or
                    receipt.get("id") != review_id or receipt.get("after_sha256") != digest(after) or
                    not isinstance(receipt.get("confirmation"), str) or not receipt["confirmation"].strip()):
                raise ValueError("待完成应用记录与提案不一致。")
        if paths["audit.json"].exists():
            if receipt is None or load_json(paths["audit.json"]) != receipt or current != after:
                raise ValueError("已应用记录或源文件已变化，拒绝重复应用。")
            if not paths["audit.md"].exists() or paths["audit.md"].read_bytes() != audit_markdown(receipt):
                raise ValueError("已应用审计文档缺失或变化。")
            if existed and (not backup.exists() or backup.read_bytes() != before):
                raise ValueError("已应用备份缺失或变化。")
            return {"command": "apply-review", "id": review_id, "already_applied": True, "audit": str(paths["audit.json"])}
        recovering = receipt is not None and current == after
        if current != expected and not recovering:
            raise ValueError("源文件与提案原始哈希不一致（或新建目标已存在），请重新提案。")
        if receipt is None:
            receipt = {"schema": 1, "id": review_id, "project_id": project_id, "proposal_sha256": digest(encoded),
                       "source": relative, "before_sha256": proposal["source_sha256"], "after_sha256": digest(after),
                       "backup": archive_relative if existed else None, "confirmation": confirmation.strip(),
                       "confirmation_type": "caller_declared", "confirmed_at": stamp(), "notice": NOTE}
        receipt_data, receipt_md = json_bytes(receipt), audit_markdown(receipt)
        if paths["audit.md"].exists() and paths["audit.md"].read_bytes() != receipt_md:
            raise ValueError("已有审计文档内容不同。")
        if existed:
            create_exact(root, backup, before)
            create_exact(root, notice, notice_data)
        create_exact(root, paths["pending.json"], receipt_data)
        if not recovering:
            target.parent.mkdir(parents=True, exist_ok=True)
            safe_path(root, target)
            if (target.read_bytes() if target.exists() else None) != expected:
                raise ValueError("应用前源文件再次变化，保留备份但不替换源文件。")
            if not existed:
                create_exact(root, target, after)
            else:
                fd, name = tempfile.mkstemp(prefix=".ai-memory-", suffix=".tmp", dir=target.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(after)
                        stream.flush()
                        os.fsync(stream.fileno())
                    safe_path(root, target)
                    if target.read_bytes() != before:
                        raise ValueError("原子替换前源文件已变化。")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        create_exact(root, paths["audit.md"], receipt_md)
        create_exact(root, paths["audit.json"], receipt_data)
        return {"command": "apply-review", "id": review_id, "already_applied": False,
                "source": relative, "backup": receipt["backup"], "audit": str(paths["audit.json"]),
                "warnings": warnings, "confirmation_notice": NOTE}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("inspect")
    read.add_argument("--root", required=True)
    read.add_argument("--documents", action="store_true", help="盘点 Markdown 与其他资料，显示未分类及变化")
    proposal = commands.add_parser("propose")
    proposal.add_argument("--root", required=True)
    proposal.add_argument("--id", required=True)
    proposal.add_argument("--kind", choices=("rule", "compact", "archive", "journal-archive", "journal-unarchive"), required=True)
    proposal.add_argument("--source")
    proposal.add_argument("--task")
    proposal.add_argument("--event", action="append", dest="events")
    proposal.add_argument("--replacement-file")
    proposal.add_argument("--reason", required=True)
    apply = commands.add_parser("apply-review")
    apply.add_argument("--root", required=True)
    apply.add_argument("--id", required=True)
    apply.add_argument("--confirmed", action="store_true")
    apply.add_argument("--confirmation")
    organize = commands.add_parser("propose-organize")
    organize.add_argument("--root", required=True)
    organize.add_argument("--id", required=True)
    organize.add_argument("--file", required=True, help="代理阅读后编写的用途与分类 JSON 清单")
    organize.add_argument("--reason", required=True)
    rollback = commands.add_parser("rollback-review")
    rollback.add_argument("--root", required=True)
    rollback.add_argument("--id", required=True)
    rollback.add_argument("--confirmed", action="store_true")
    rollback.add_argument("--confirmation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        root = project_root(args.root)
        if args.command == "inspect":
            if args.documents:
                from memory_documents import inspect_documents
                result = inspect_documents(root)
            else:
                result = inspect(root)
        elif args.command == "propose":
            if args.kind.startswith("journal-"):
                if args.source or args.replacement_file:
                    raise ValueError("journal 归档只接收任务或事件集合，不接收源文件或替换稿。")
                from memory_archive import propose_archive
                result = propose_archive(root, args.id, args.kind, args.reason, args.task, args.events)
            else:
                if not args.source or args.task or args.events:
                    raise ValueError("规则 / 文档整理需要 --source；日志集合另用 journal-archive。")
                result = propose(root, args.id, args.kind, args.source, args.replacement_file, args.reason)
        elif args.command == "propose-organize":
            from memory_documents import propose_organize
            result = propose_organize(root, args.id, args.file, args.reason)
        elif args.command == "rollback-review":
            from memory_documents import rollback_review
            result = rollback_review(root, args.id, args.confirmed, args.confirmation)
        else:
            result = apply_review(root, args.id, args.confirmed, args.confirmation)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"command": args.command, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
