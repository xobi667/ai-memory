#!/usr/bin/env python3
"""Read-only document freshness checks and immutable, caller-reviewed evidence snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_common import atomic_write, json_bytes, project_lock, safe_path
from memory_workflow import _identifier, _project, _root, _status_source, load_scope, local_path

MAX_METADATA_BYTES = 1024 * 1024
MAX_REVIEWS = 2048
MAX_DOCUMENTS = 256
MAX_DEPENDENCIES = 128
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVIEW_DIRECTORY = ".ai-memory/reviews"
CATALOG = ".ai-memory/documents.json"


def _read_json(root, path):
    safe_path(root, path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("评审元数据超过 1 MiB；请拆分后重试。")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("评审元数据必须是 JSON 对象。")
    return value


def _relative(root, value):
    from memory_documents import EXCLUDED, _sensitive
    path = local_path(root, value)
    relative = path.relative_to(root).as_posix()
    if any(part.casefold() in EXCLUDED or _sensitive(part) for part in Path(relative).parts):
        raise ValueError("不读取内部状态、依赖、缓存或可能包含凭据的文件。")
    return relative


def _key(path):
    return os.path.normcase(path).replace("\\", "/")


def _catalog_aliases(root, project_id):
    """Use the applied organizer catalog; aliases never come from filename guesses."""
    from memory_documents import _catalog
    path = safe_path(root, root / CATALOG)
    if not path.exists():
        return None, {}
    catalog = _catalog(root)
    if catalog.get("project_id") != project_id:
        raise ValueError("文档清单缺少本项目身份或属于其他项目。")
    aliases = {}
    for entry in catalog["documents"]:
        source = _relative(root, entry["source"])
        originals = entry.get("original_paths", [])
        if not isinstance(originals, list) or any(not isinstance(value, str) for value in originals):
            raise ValueError("文档原路径清单必须是相对路径数组。")
        originals = [_relative(root, value) for value in originals]
        identity = originals[0] if originals else source
        for alias in [source, *originals]:
            key = _key(alias)
            if key in aliases and aliases[key][0] != source:
                raise ValueError("文档原路径指向多个当前文档；不能猜测评审归属：" + alias)
            aliases[key] = (source, identity)
    return catalog, aliases


def _identity_path(item, aliases):
    path = item.get("identity_path", item["path"])
    return aliases.get(_key(path), (path, path))[1]


def _current_path(item, aliases):
    # Persisted identity lets a new-path review follow a later rollback without
    # rewriting the snapshot, while the live catalog remains the mapping source.
    identity = item.get("identity_path", item["path"])
    return aliases.get(_key(identity), aliases.get(_key(item["path"]), (item["path"], identity)))[0]


def _rolled_back_path(root, item, project_id):
    """A removed first catalog can still be explained by its sealed rollback receipt."""
    review_id = item.get("relocation_review")
    identity = item.get("identity_path", item["path"])
    if not review_id or _key(identity) == _key(item["path"]):
        return item["path"]
    if local_path(root, item["path"]).exists() or not local_path(root, identity).is_file():
        return item["path"]
    directory = root / REVIEW_DIRECTORY / _identifier(review_id)
    receipt_path = safe_path(root, directory / "batch-receipt.json")
    if not receipt_path.exists():
        return item["path"]
    receipt = _read_json(root, receipt_path)
    if receipt.get("phase") != "rolled_back":
        return item["path"]
    proposal_path = safe_path(root, directory / "proposal.json")
    with proposal_path.open("rb") as stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("回滚提案超过 1 MiB；请按原路径人工核对。")
    proposal = json.loads(raw.decode("utf-8-sig"))
    seal = _read_json(root, root / f".ai-memory/review-seals/{review_id}.json")
    digest = hashlib.sha256(raw).hexdigest()
    if (not isinstance(proposal, dict) or type(proposal.get("schema")) is not int or proposal["schema"] != 1
            or proposal.get("kind") != "organize" or proposal.get("id") != review_id
            or proposal.get("project_id") != project_id
            or seal != {"id": review_id, "project_id": project_id, "proposal_sha256": digest}
            or receipt.get("id") != review_id or receipt.get("project_id") != project_id
            or receipt.get("proposal_sha256") != digest):
        raise ValueError("文档回滚凭据与本项目的封印提案不一致。")
    for move in proposal.get("moves", []):
        if isinstance(move, dict) and _key(move.get("target", "")) == _key(item["path"]) and _key(
                move.get("source", "")) == _key(identity):
            return identity
    return item["path"]


def _hash(root, relative):
    path = local_path(root, _relative(root, relative))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or (
            after.st_size, after.st_mtime_ns, after.st_ino) != (current.st_size, current.st_mtime_ns, current.st_ino):
        raise ValueError("读取期间文件发生变化；请重新核对。")
    return digest.hexdigest()


def _snapshot(root, path, project_id, task_filter=None):
    from memory_store import _timestamp
    value = _read_json(root, path)
    review_id = path.parent.name
    _identifier(review_id)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("kind") != "document-evidence" or value.get("review_id") != review_id
            or value.get("project_id") != project_id):
        raise ValueError("评审快照的格式、项目身份或评审 ID 无效。")
    if task_filter is not None and value.get("task_id") not in (None, task_filter):
        return None
    _timestamp(value.get("created_at"))
    if (not isinstance(value.get("summary"), str) or not value["summary"].strip()
            or value.get("claim_verified_by_script") is not False):
        raise ValueError("评审快照缺少有效的调用者核对声明。")
    if value.get("task_id") is not None:
        _identifier(value["task_id"])
        load_scope(root, value["task_id"], project_id)
    documents = value.get("documents")
    if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS:
        raise ValueError("评审快照需要 1–256 个文档。")
    seen = set()
    for item in documents:
        if not isinstance(item, dict):
            raise ValueError("评审文档条目必须是对象。")
        relative = _relative(root, item.get("path"))
        if "identity_path" in item:
            _relative(root, item["identity_path"])
        if "relocation_review" in item:
            _identifier(item["relocation_review"])
        if _key(relative) in seen or not isinstance(item.get("sha256"), str) or not SHA256.fullmatch(item["sha256"]):
            raise ValueError("评审文档重复或哈希无效。")
        seen.add(_key(relative))
        dependencies = item.get("dependencies")
        if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
            raise ValueError("每份文档最多登记 128 个依赖。")
        dependency_keys = set()
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                raise ValueError("评审依赖必须是对象。")
            dependency_path = _relative(root, dependency.get("path"))
            if "identity_path" in dependency:
                _relative(root, dependency["identity_path"])
            if "relocation_review" in dependency:
                _identifier(dependency["relocation_review"])
            key = _key(dependency_path)
            if (key in dependency_keys or key == _key(relative) or not isinstance(dependency.get("sha256"), str)
                    or not SHA256.fullmatch(dependency["sha256"])):
                raise ValueError("依赖重复、指向文档自身或哈希无效。")
            dependency_keys.add(key)
        targets = item.get("supersedes", [])
        if not isinstance(targets, list) or len(targets) != len(set(targets)):
            raise ValueError("supersedes 必须是不重复的评审 ID 数组。")
        for target in targets:
            _identifier(target)
    return value


def _snapshots(root, project_id, task_filter=None):
    snapshots, errors, warnings = [], [], []
    directory = safe_path(root, root / REVIEW_DIRECTORY, directory=True)
    if not directory.exists():
        return snapshots, errors, warnings, True
    children = sorted(directory.iterdir(), key=lambda path: path.name)
    complete = len(children) <= MAX_REVIEWS
    if not complete:
        warnings.append({"code": "review_scan_limit", "path": REVIEW_DIRECTORY,
                         "message": "评审目录超过 2048 个；仅核对前 2048 个目录，不能据此认定全部有效。"})
    for child in children[:MAX_REVIEWS]:
        relative = child.relative_to(root).as_posix() + "/evidence.json"
        try:
            safe_path(root, child, directory=True)
            path = safe_path(root, root / relative)
            if path.exists():
                snapshot = _snapshot(root, path, project_id, task_filter)
                if snapshot is not None:
                    snapshots.append(snapshot)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({"code": "invalid_review", "path": relative, "message": str(exc)})
    return snapshots, errors, warnings, complete


def _review_graph(snapshots, aliases=None):
    """Only an explicit, valid same-document/same-task edge retires a prior snapshot."""
    aliases = aliases or {}
    nodes = {(value["review_id"], value.get("task_id"), _key(_identity_path(item, aliases))): (value, item)
             for value in snapshots for item in value["documents"]}
    edges, conflicts = {}, []
    for node, (value, item) in nodes.items():
        targets = [(target, node[1], node[2]) for target in item.get("supersedes", [])]
        valid = []
        for target in targets:
            if target not in nodes:
                conflicts.append({"code": "missing_review_target", "review_id": node[0], "path": item["path"],
                                  "target_review": target[0], "task_id": node[1],
                                  "message": "替代关系没有指向同一任务、同一文档的有效评审。"})
            else:
                valid.append(target)
        edges[node] = valid
    # Iterative Kosaraju finds whole strongly connected components. Marking only
    # DFS back-edge trails misses nodes reached through a previously finished
    # branch, allowing a cyclic source to retire otherwise valid snapshots.
    visited, finished = set(), []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(edges[start]))]
        while stack:
            node, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                finished.append(node)
                stack.pop()
            elif target not in visited:
                visited.add(target)
                stack.append((target, iter(edges[target])))
    reverse = {node: [] for node in nodes}
    for source, targets in edges.items():
        for target in targets:
            reverse[target].append(source)
    assigned, cyclic = set(), set()
    for start in reversed(finished):
        if start in assigned:
            continue
        component, pending = [], [start]
        assigned.add(start)
        while pending:
            node = pending.pop()
            component.append(node)
            for source in reverse[node]:
                if source not in assigned:
                    assigned.add(source)
                    pending.append(source)
        if len(component) > 1 or start in edges[start]:
            cyclic.update(component)
    for node in sorted(cyclic, key=lambda item: (item[0], item[1] or "", item[2])):
        conflicts.append({"code": "review_cycle", "review_id": node[0], "path": nodes[node][1]["path"],
                          "task_id": node[1], "message": "评审替代关系存在循环；涉及快照均保留待核对。"})
    superseded = {target for source, targets in edges.items() if source not in cyclic for target in targets
                  if target not in cyclic}
    heads = {node: values for node, values in nodes.items() if node not in superseded}
    groups = {}
    for node, (value, item) in heads.items():
        groups.setdefault((node[1], node[2]), []).append((value, item))
    for (task_id, _), group in groups.items():
        fingerprints = {json.dumps({"sha256": item["sha256"], "dependencies": sorted([
            {"identity": _identity_path(dependency, aliases), "sha256": dependency["sha256"]}
            for dependency in item["dependencies"]], key=lambda dependency: dependency["identity"])}, sort_keys=True)
            for _, item in group}
        if len(fingerprints) > 1:
            conflicts.append({"code": "parallel_review_heads", "path": group[0][1]["path"], "task_id": task_id,
                              "review_ids": sorted(value["review_id"] for value, _ in group),
                              "message": "同一文档存在未建立替代关系的不同评审依据；未按时间或文件名选择权威。"})
    return heads, conflicts


def review_documents(root, manifest):
    """Record files the caller has actually reviewed; no content or status files are changed."""
    root = _root(root)
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or set(manifest) - {"schema_version", "review_id", "task_id", "summary", "documents"}):
        raise ValueError("评审清单仅接受 schema_version: 1、review_id、task_id、summary、documents。")
    review_id = _identifier(manifest.get("review_id"))
    summary = manifest.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.encode("utf-8")) > 8192:
        raise ValueError("需要不超过 8 KiB 的实际核对摘要；不能只登记哈希代替阅读。")
    requested = manifest.get("documents")
    if not isinstance(requested, list) or not 1 <= len(requested) <= MAX_DOCUMENTS:
        raise ValueError("每批评审需要 1–256 个文档。")
    project = _project(root)  # Do not create state in an unregistered/foreign project.
    task_id = manifest.get("task_id")
    if task_id is not None:
        load_scope(root, task_id, project["project_id"])
    target = safe_path(root, root / REVIEW_DIRECTORY / review_id, directory=True)
    seal = safe_path(root, root / f".ai-memory/review-seals/{review_id}.json")
    if target.exists() or seal.exists():
        raise ValueError("评审 ID 已存在；请使用新 ID，不能覆盖整理提案或历史评审。")
    with project_lock(root):
        if _project(root)["project_id"] != project["project_id"]:
            raise ValueError("项目身份已变化。")
        if task_id is not None:
            load_scope(root, task_id, project["project_id"])
        if target.exists() or seal.exists():
            raise ValueError("评审 ID 已存在；不会覆盖。")
        previous, errors, _, complete = _snapshots(root, project["project_id"])
        if errors or not complete:
            raise ValueError("现有评审有损坏或未扫描完；请先运行 health 核对，原文件保留。")
        catalog, aliases = _catalog_aliases(root, project["project_id"])
        documents, seen, identities = [], set(), set()
        for entry in requested:
            if not isinstance(entry, dict) or set(entry) - {"path", "dependencies", "supersedes"}:
                raise ValueError("文档条目仅接受 path、dependencies 和 supersedes。")
            relative = _relative(root, entry.get("path"))
            if _key(relative) in seen:
                raise ValueError("同一批评审包含重复文档。")
            seen.add(_key(relative))
            dependencies = entry.get("dependencies", [])
            if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
                raise ValueError("每份文档的 dependencies 必须是最多 128 个相对路径的数组。")
            result, dependency_keys = [], set()
            for dependency in dependencies:
                dependency = _relative(root, dependency)
                key = _key(dependency)
                if key == _key(relative) or key in dependency_keys:
                    raise ValueError("依赖不能重复或指向文档自身。")
                dependency_keys.add(key)
                result.append({"path": dependency, "identity_path": _identity_path({"path": dependency}, aliases),
                               "sha256": _hash(root, dependency)})
            supersedes = entry.get("supersedes", [])
            if not isinstance(supersedes, list):
                raise ValueError("supersedes 必须是已阅读并明确替代的评审 ID 数组。")
            for identifier in supersedes:
                _identifier(identifier)
            if len(set(supersedes)) != len(supersedes):
                raise ValueError("supersedes 不能包含重复评审 ID。")
            identity_path = _identity_path({"path": relative}, aliases)
            if _key(identity_path) in identities:
                raise ValueError("同一批评审包含指向同一文档的多个路径别名。")
            identities.add(_key(identity_path))
            applicable = {value["review_id"] for value in previous if value.get("task_id") == task_id
                          and any(_key(_identity_path(item, aliases)) == _key(identity_path) for item in value["documents"])}
            if set(supersedes) - applicable:
                raise ValueError("supersedes 只能引用同一任务、同一文档的现有评审。")
            documents.append({"path": relative, "identity_path": identity_path, "sha256": _hash(root, relative),
                              "dependencies": sorted(result, key=lambda item: item["path"]), "supersedes": sorted(supersedes)})
        if catalog is not None and catalog.get("review_id"):
            relocation_review = _identifier(catalog["review_id"])
            for item in documents:
                for observed in [item, *item["dependencies"]]:
                    if _key(observed["path"]) != _key(observed["identity_path"]):
                        observed["relocation_review"] = relocation_review
        value = {"schema_version": 1, "kind": "document-evidence", "project_id": project["project_id"],
                 "review_id": review_id, "task_id": task_id,
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                 "summary": summary.strip(), "documents": documents,
                 "claim_verified_by_script": False}
        _, relation_conflicts = _review_graph(previous + [value], aliases)
        if any(item["code"] == "review_cycle" and item["review_id"] == review_id for item in relation_conflicts):
            raise ValueError("新增评审会形成循环替代关系；请先核对已有评审。")
        data = json_bytes(value)
        if len(data) > MAX_METADATA_BYTES:
            raise ValueError("评审元数据超过 1 MiB；请拆分评审批次。")
        # External editors need not use our lock. Recheck all observed bytes before publication.
        for item in documents:
            for observed in [item, *item["dependencies"]]:
                if _hash(root, observed["path"]) != observed["sha256"]:
                    raise ValueError("登记期间文件已变化；请重新核对：" + observed["path"])
        atomic_write(root, target / "evidence.json", data)
    return {"command": "review-docs", "review_id": review_id, "task_id": task_id,
            "path": (target / "evidence.json").relative_to(root).as_posix(), "document_count": len(documents),
            "dependency_count": sum(len(item["dependencies"]) for item in documents), "source_modified": False,
            "business_validation": "not_performed", "caller_reviewed": True, "claim_verified_by_script": False,
            "note": "已记录调用者的评审声明和当时文件哈希；脚本没有验证内容正确性，也未改变分类、任务状态或长期规则。"}


def _bounded(report, max_bytes):
    if type(max_bytes) is not int or not 2048 <= max_bytes <= 1024 * 1024:
        raise ValueError("max_bytes 必须是 2048–1048576 的整数。")
    fields = ("changes", "conflicts", "errors", "warnings")
    report["total_counts"] = {field: len(report[field]) for field in fields}
    report["needs_attention"] = any(report[field] for field in ("changes", "conflicts", "errors"))
    report["omitted_counts"] = {field: 0 for field in fields}
    report["output_truncated"] = False
    size = lambda: len(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")) + 1
    if size() <= max_bytes:
        return report
    originals = {field: report[field] for field in fields}
    for field in fields:
        report[field] = []
        report["omitted_counts"][field] = len(originals[field])
    report["output_truncated"] = True
    if size() > max_bytes:
        raise ValueError("输出预算不足以容纳健康检查摘要。")
    # Preserve errors and explicit conflicts first. Binary-search prefixes avoid
    # repeatedly serializing a huge report once for every omitted dependency.
    for field in ("errors", "conflicts", "changes", "warnings"):
        low, high = 0, len(originals[field])
        while low < high:
            middle = (low + high + 1) // 2
            report[field] = originals[field][:middle]
            report["omitted_counts"][field] = len(originals[field]) - middle
            if size() <= max_bytes:
                low = middle
            else:
                high = middle - 1
        report[field] = originals[field][:low]
        report["omitted_counts"][field] = len(originals[field]) - low
    return report


def health(root, task_id=None, max_bytes=16384):
    """Compare observed bytes and explicit state relationships, never infer semantic correctness."""
    root = _root(root)
    if task_id is not None:
        _identifier(task_id)
    report = {"command": "health", "read_only": True, "registered": False, "task_id": task_id,
              "changes": [], "conflicts": [], "errors": [], "warnings": [], "scan_complete": True,
              "checked_counts": {"catalog_documents": 0, "review_documents": 0, "dependencies": 0, "task_states": 0},
              "coverage": {"catalog": False, "review_snapshots": False, "journal_relations": "use audit or resume"},
              "business_validation": "not_performed", "semantic_review_required": True,
              "note": "哈希变化只说明需重新核对；未变化也不证明文档与实现一致。仅核对已登记依据，未执行测试。"}
    changes, conflicts, errors, warnings = (report[key] for key in ("changes", "conflicts", "errors", "warnings"))
    try:
        project_path = safe_path(root, root / ".ai-memory/project.json")
        if not project_path.exists():
            state = safe_path(root, root / ".ai-memory", directory=True)
            if state.exists() and any(path.name != "store.lock" for path in state.iterdir()):
                raise ValueError("记忆资料缺少项目身份；未读取未经归属核验的依据。")
            warnings.append({"code": "unregistered_project", "message": "项目尚未登记，没有可核对的评审依据。"})
            report["checks_passed"] = True
            return _bounded(report, max_bytes)
        project = _project(root)
        report["registered"] = True
        if task_id is not None:
            load_scope(root, task_id, project["project_id"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append({"code": "invalid_project_or_task", "path": ".ai-memory/project.json" if task_id is None
                       else f".ai-memory/scopes/{task_id}.json", "message": str(exc)})
        report.update(checks_passed=False, scan_complete=False)
        return _bounded(report, max_bytes)
    hashes, aliases = {}, {}

    def compare(item, code, **context):
        relative = item["path"]
        try:
            relative = _relative(root, _current_path(item, aliases))
            if relative == item["path"]:
                relative = _rolled_back_path(root, item, project["project_id"])
            if relative != item["path"]:
                context["reviewed_path"] = item["path"]
            key = _key(relative)
            if key not in hashes:
                try:
                    hashes[key] = _hash(root, relative)
                except FileNotFoundError:
                    hashes[key] = None
            current = hashes[key]
            if current != item["sha256"]:
                if current is None and item.get("identity_path", item["path"]) != item["path"]:
                    context["original_path_hint"] = item["identity_path"]
                changes.append({"code": code + ("_missing" if current is None else "_changed"),
                                "path": relative, "expected_sha256": item["sha256"], "current_sha256": current,
                                "semantic_review_required": True, **context})
            return relative, current == item["sha256"]
        except (OSError, ValueError) as exc:
            errors.append({"code": "unreadable_evidence", "path": relative, "message": str(exc), **context})
            return relative, False

    try:
        catalog, aliases = _catalog_aliases(root, project["project_id"])
        if catalog is not None:
            report["coverage"]["catalog"] = True
            for item in catalog["documents"]:
                try:
                    if not SHA256.fullmatch(item["sha256"]):
                        raise ValueError("文档清单哈希无效。")
                    compare({"path": item["source"], "sha256": item["sha256"]}, "catalog_document")
                    report["checked_counts"]["catalog_documents"] += 1
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append({"code": "invalid_catalog_entry", "path": item.get("source", CATALOG), "message": str(exc)})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append({"code": "invalid_catalog", "path": CATALOG, "message": str(exc)})
    try:
        snapshots, snapshot_errors, snapshot_warnings, complete = _snapshots(root, project["project_id"], task_id)
        errors.extend(snapshot_errors)
        warnings.extend(snapshot_warnings)
        report["scan_complete"] = complete
        heads, relation_conflicts = _review_graph(snapshots, aliases)
        conflicts.extend(item for item in relation_conflicts if task_id is None or item.get("task_id") in (None, task_id))
        conflicted = {_key(_identity_path({"path": item["path"]}, aliases)) for item in relation_conflicts}
        reviewed_current = set()
        for node, (snapshot, item) in heads.items():
            if task_id is not None and node[1] not in (None, task_id):
                continue
            report["coverage"]["review_snapshots"] = True
            context = {"review_id": snapshot["review_id"], "document": item["path"], "task_id": node[1]}
            current_path, unchanged = compare(item, "review_document", **context)
            if unchanged and node[2] not in conflicted:
                reviewed_current.add(_key(current_path))
            report["checked_counts"]["review_documents"] += 1
            if not item["dependencies"]:
                warnings.append({"code": "review_without_dependencies", "path": item["path"],
                                 "review_id": snapshot["review_id"], "message": "仅记录了文档自身；未登记实现或证据依赖。"})
            for dependency in item["dependencies"]:
                compare(dependency, "dependency", **context)
                report["checked_counts"]["dependencies"] += 1
        retained = []
        for change in changes:
            if change["code"] == "catalog_document_changed" and _key(change["path"]) in reviewed_current:
                warnings.append({"code": "classification_review_pending", "path": change["path"],
                                 "message": "现内容已有有效评审；分类清单仍是旧哈希，后续按用途复核分类。未自动更新清单。"})
            else:
                retained.append(change)
        changes[:] = retained
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append({"code": "invalid_review_store", "path": REVIEW_DIRECTORY, "message": str(exc)})
    try:
        directory = safe_path(root, root / ".ai-memory/scopes", directory=True)
        paths = [directory / f"{task_id}.json"] if task_id else sorted(directory.glob("*.json")) if directory.exists() else []
        for path in paths:
            try:
                scope = load_scope(root, path.stem, project["project_id"])
                state = _status_source(root, scope)
                report["checked_counts"]["task_states"] += 1
                if state["state"] == "unknown":
                    warnings.append({"code": "unknown_task_state", "task_id": path.stem,
                                     "path": state["path"], "message": "任务状态没有可识别的唯一来源；未猜测完成状态。"})
            except (OSError, ValueError, TypeError, KeyError) as exc:
                message = str(exc)
                conflict = isinstance(exc, ValueError) and any(word in message for word in ("重复", "并存", "恰好出现一次"))
                (conflicts if conflict else errors).append({"code": "task_state_conflict" if conflict else "invalid_task_state",
                    "task_id": path.stem, "path": path.relative_to(root).as_posix(), "message": message})
    except (OSError, ValueError) as exc:
        errors.append({"code": "invalid_scopes", "path": ".ai-memory/scopes", "message": str(exc)})
    if not report["coverage"]["review_snapshots"]:
        warnings.append({"code": "no_review_baseline", "message": "没有适用的文档依赖评审快照；不能判断实现变化是否影响文档。"})
    report["scan_complete"] = report["scan_complete"] and not errors
    report["checks_passed"] = not errors and not conflicts
    return _bounded(report, max_bytes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("health")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--task")
    inspect.add_argument("--max-bytes", type=int, default=16384)
    review = commands.add_parser("review-docs")
    review.add_argument("--root", required=True)
    review.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "health":
            result = health(args.root, args.task, args.max_bytes)
            status = 1 if result["total_counts"]["errors"] or not result["scan_complete"] else 0
        else:
            path = Path(args.file)
            with path.open("rb") as stream:
                raw = stream.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise ValueError("评审清单超过 1 MiB。")
            result = review_documents(args.root, json.loads(raw.decode("utf-8-sig")))
            status = 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result, status = {"command": args.command, "error": str(exc), "business_validation": "not_performed"}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
