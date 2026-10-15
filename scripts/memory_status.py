#!/usr/bin/env python3
"""只读查看已保存的任务记忆；不执行项目验证，也不自动保存聊天。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unicodedata

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_common import safe_path
import memory_workflow as workflow
from memory_store import resume_memory

DETAILS = {"small": "精简", "standard": "标准", "major": "详细"}
STATES = {"active": "进行中", "paused": "已暂停", "completed": "记录为已完成", "unknown": "未知，需核对"}
CLAIMS = {"passed": "声称通过", "failed": "记录失败", "partial": "部分验证", "not_run": "未执行", "unstructured": "文字声明"}
FIELDS = {"goal": "目标", "constraints": "约束", "completed": "已完成事项", "verification": "验证记录",
          "blockers": "阻塞", "next": "下一步", "impact": "影响", "tradeoffs": "取舍", "rollback": "迁移或回退",
          "acceptance": "验收", "document_freshness": "文档变化", "document_conflicts": "文档冲突",
          "document_health": "文档核对未完成"}
WARNINGS = {"selected_task_not_active": "所选任务记录为暂停、完成或未知；请核对当前适用性。",
            "unknown_task_state": "任务状态未知，未自动选择或推断完成。", "incomplete_scan": "扫描未完整完成。",
            "missing_entrypoint": "登记的文档入口不存在。", "unavailable_evidence": "证据引用不存在或不在安全读取边界内。",
            "invalid_evidence_link": "证据链接无效。", "classification_review_pending": "分类清单仍需按现内容复核。",
            "review_without_dependencies": "评审只登记文档自身，未覆盖实现依赖。",
            "no_review_baseline": "缺少适用的文档依赖评审基线。", "unexpected_file": "日志目录发现未知文件，未读取。"}
NOTES = "只观察磁盘上的记录；验证和完成状态是记录者声明。本次未执行项目命令，也未自动保存未记录的聊天。"
EXISTING_DOCUMENTS = ("交接.md", "文档/交接.md", "docs/交接.md", "HANDOFF.md", "docs/HANDOFF.md",
                      "DEV.md", "文档/当前状态.md", "docs/当前状态.md", "文档/索引.md", "docs/INDEX.md", "README.md")


def _json(report):
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def _line(value, limit=180):
    # Render source text as one inert line, including terminal/bidi controls.
    value = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(value)).split()
    text = " ".join(value)
    return text if len(text) <= limit else text[:limit] + "…〔展示省略〕"


def _documents(root, errors):
    candidates = list(EXISTING_DOCUMENTS)
    try:
        layout_path = safe_path(root, root / ".ai-memory/layout.json")
        if layout_path.exists():
            layout = workflow.load_layout(root)
            candidates = [*layout["roles"].values(), layout["index"], *candidates]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append({"code": "invalid_layout", "path": ".ai-memory/layout.json", "message": str(exc)})
    found = []
    for relative in dict.fromkeys(candidates):
        try:
            if workflow.local_path(root, relative).is_file():
                found.append(relative)
        except (OSError, ValueError) as exc:
            errors.append({"code": "unavailable_document", "path": relative, "message": str(exc)})
    return found


def _health_summary(health):
    coverage = health.get("coverage", {})
    counts = health.get("total_counts", {})
    checked = health.get("checked_counts", {})
    if health.get("scan_complete") is not True or counts.get("errors", 0):
        state = "unavailable"
    elif counts.get("changes", 0) or counts.get("conflicts", 0):
        state = "changed"
    elif coverage.get("review_snapshots") is not True:
        state = "no_baseline"
    elif not checked.get("dependencies"):
        state = "documents_only"
    elif counts.get("warnings", 0) or health.get("warnings"):
        state = "review_pending"
    else:
        state = "checked_registered_dependencies"
    return {"state": state, "coverage": coverage, "checked_counts": checked, "total_counts": counts,
            "scan_complete": health.get("scan_complete") is True,
            "output_truncated": health.get("output_truncated", False),
            "changes": health.get("changes", []), "conflicts": health.get("conflicts", []),
            "errors": health.get("errors", []), "warnings": health.get("warnings", [])}


def _bounded(report, max_bytes):
    report["total_counts"] = {"errors": max(len(report["errors"]), report["source_counts"]["errors"]),
                              "warnings": max(len(report["warnings"]), report["source_counts"]["warnings"]),
                              "gaps": len(report["handoff_gaps"])}
    report["counts_are_lower_bounds"] = not report["counts_complete"]
    report["output_budget_bytes"] = max_bytes
    report["output_truncated"] = False
    if len(_json(report).encode("utf-8")) <= max_bytes:
        return report
    report["output_truncated"] = True
    report["coverage"]["summary_complete"] = False
    report["needs_attention"] = True
    report["read_hint"] = "摘要已省略；增大 --max-bytes，或按来源路径使用 resume / handoff / health 补读。"
    # Preserve identities, coverage and aggregate counts even when details cannot fit.
    def shorten(value):
        if isinstance(value, str):
            return value if len(value) <= 160 else value[:160] + "…〔省略〕"
        if isinstance(value, list):
            return [shorten(item) for item in value[:3]]
        if isinstance(value, dict):
            return {key: shorten(item) for key, item in value.items()}
        return value
    report = shorten(report)
    if len(_json(report).encode("utf-8")) <= max_bytes:
        return report
    # The compact fallback never reports missing detail as an empty successful check.
    return {"command": "status", "read_only": True, "memory_state": report["memory_state"],
            "task_id": report.get("task", {}).get("task_id") if report.get("task") else None,
            "needs_attention": True, "total_counts": report["total_counts"],
            "counts_complete": report["counts_complete"], "counts_are_lower_bounds": report["counts_are_lower_bounds"],
            "document_counts": report.get("documents", {}).get("total_counts", {}),
            "coverage": {"summary_complete": False, "history_audited": False},
            "output_truncated": True, "output_budget_bytes": max_bytes,
            "business_validation": "not_performed", "read_hint": report["read_hint"], "note": NOTES}


def status(root, task_id=None, client=None, session=None, max_bytes=16384):
    """Observe saved state using the shared selector, recovery and handoff readers."""
    if type(max_bytes) is not int or not 2048 <= max_bytes <= 1024 * 1024:
        raise ValueError("max_bytes 必须是 2048–1048576 的整数，限制完整 UTF-8 输出。")
    if (client is None) != (session is None):
        raise ValueError("client 和 session 须同时提供。")
    if task_id is not None:
        workflow._identifier(task_id)
    if session is not None:
        workflow._session(client, session)
    root = workflow._root(root)
    report = {"command": "status", "root": str(root), "read_only": True, "memory_state": "unavailable",
              "task": None, "selection": None, "records": {"items": [], "count_complete": False},
              "verification_reports": [], "pending_sources": [], "handoff_gaps": [], "documents": {},
              "existing_documents": [], "errors": [], "warnings": [], "suggestions": [],
              "source_counts": {"errors": 0, "warnings": 0},
              "counts_complete": True,
              "coverage": {"summary_complete": True, "history_audited": False, "history_complete": False},
              "business_validation": "not_performed", "note": NOTES}
    errors, warnings = report["errors"], report["warnings"]
    try:
        project_path = safe_path(root, root / ".ai-memory/project.json")
        if not project_path.exists():
            # A layout or an initializer's lock alone is not structured task memory.
            for relative in (".ai-memory/scopes", ".ai-memory/reviews", ".ai-memory/bindings", "docs/memory/journal"):
                path = safe_path(root, root / relative, directory=True)
                if path.exists():
                    raise ValueError("存在任务或记录目录但缺少项目身份，请核对：" + relative)
            report["memory_state"] = "not_enabled"
            report["existing_documents"] = _documents(root, errors)
            report["suggestions"] = ["沿用已有交接或项目文档；需要接续时保存目标、已做事项、未验证项和下一步的轻量交接。"]
            if task_id or session:
                warnings.append({"code": "no_structured_task", "message": "尚未启用结构化记忆，无法解析任务或会话绑定。"})
        else:
            selection = workflow.resolve_selection(root, task_id, client, session)
            report["selection"] = selection
            errors.extend(selection.get("errors", []))
            warnings.extend(selection.get("warnings", []))
            selected = selection.get("task_id")
            if not selected:
                report["memory_state"] = "selection_required" if not errors else "unavailable"
                report["suggestions"] = ["按实际任务指定 --task；多个活动任务或未知状态时不会自动猜测。" if selection.get("candidates")
                                         else "当前没有可读取的已确认任务范围；沿用现有文档或保存当前任务的轻量交接。"]
                if any(item.get("code") == "invalid_binding" for item in errors):
                    report["suggestions"] = ["会话绑定已失效；核对原任务，显式指定 --task 或修正绑定后重试。"]
                report["existing_documents"] = _documents(root, errors)
            else:
                scope = workflow.load_scope(root, selected)
                current = workflow.set_task_status(root, selected)
                report["memory_state"] = "enabled"
                report["task"] = {"task_id": selected, "goal": scope["goal"], "detail": scope["detail"],
                                  "detail_label": DETAILS[scope["detail"]], "state": current["status"],
                                  "state_label": STATES.get(current["status"], STATES["unknown"]),
                                  "source": f".ai-memory/scopes/{selected}.json", "plan": scope.get("plan"),
                                  "state_source": current.get("source")}
                errors.extend({"code": "invalid_task_state", "path": current.get("source"), "message": error}
                              for error in current.get("errors", []))
                restored = resume_memory(root, task_id=selected, limit=3, max_bytes=8192, max_output_bytes=32768)
                errors.extend(restored.get("errors", []))
                warnings.extend(restored.get("warnings", []))
                for field in ("errors", "warnings"):
                    source_count = restored.get(field[:-1] + "_count", len(restored.get(field, [])))
                    report["source_counts"][field] = max(report["source_counts"][field], source_count)
                    if source_count > len(restored.get(field, [])):
                        report["counts_complete"] = False
                        report[field].append({"code": "omitted_" + field, "count": source_count,
                            "message": "恢复报告省略了部分" + ("错误" if field == "errors" else "提示") + "明细；使用 resume 补读。"})
                records = restored.get("records", [])
                report["records"] = {"available_count": restored.get("record_count"),
                    "count_complete": restored.get("scan_complete") is True and not restored.get("error_count", 0),
                    "shown_count": len(records), "items": [{"path": record["path"],
                        "event_id": record.get("metadata", {}).get("event_id"), "time": record.get("metadata", {}).get("time"),
                        "body_checksum_verified": record.get("body_checksum_verified") is True,
                        "content_truncated": record.get("content_truncated", True)} for record in records]}
                report["coverage"]["history_complete"] = (restored.get("scan_complete") is True
                    and not restored.get("has_more", True) and not restored.get("content_truncated", True)
                    and not restored.get("output_truncated", True) and not restored.get("error_count", 0))
                handoff = workflow.handoff(root, selected)
                errors.extend(handoff.get("errors", []))
                warnings.extend(handoff.get("warnings", []))
                report["verification_reports"] = handoff.get("verification_reports", [])
                report["handoff_gaps"] = handoff.get("gaps", [])
                report["documents"] = _health_summary(handoff.get("document_health", {}))
                documents = report["documents"]
                for field in ("errors", "warnings"):
                    report[field].extend(documents[field])
                    if (documents["total_counts"].get(field, len(documents[field])) > len(documents[field])
                            or handoff.get(field[:-1] + "_count", len(handoff.get(field, []))) > len(handoff.get(field, []))):
                        report["counts_complete"] = False
                    report["source_counts"][field] = max(report["source_counts"][field],
                        documents["total_counts"].get(field, len(documents[field])),
                        handoff.get(field[:-1] + "_count", len(handoff.get(field, []))))
                # A plan remains authoritative; journal next steps are historical snapshots.
                if scope.get("plan"):
                    report["pending_sources"] = [{"path": scope["plan"], "kind": "current_plan",
                        "purpose": "未完成事项、阻塞和下一步的主要来源"}]
                elif records:
                    report["pending_sources"] = [{"path": records[0]["path"], "kind": "historical_evidence",
                        "purpose": "最近保存的下一步快照，当前适用性需核对"}]
                if current["status"] == "unknown":
                    report["suggestions"].append("按任务状态来源核对当前阶段，未知状态不能视为已完成。")
                if report["documents"]["state"] == "changed":
                    report["suggestions"].append("按文档变化路径核对文档和实现；文件变化本身不证明文档错误。")
                if report["handoff_gaps"]:
                    report["suggestions"].append("先按来源补读交接缺口，确认真实情况后更新相应记录。")
                if not report["coverage"]["history_complete"]:
                    report["suggestions"].append("历史或正文未完整覆盖；用 resume 或按日志路径补读相关内容。")
                report["suggestions"].append("从主要来源核对未完成事项和下一步；只对实际执行过的验证写入结论。")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append({"code": "status_unavailable", "message": str(exc)})
        report["memory_state"] = "unavailable"
        report["suggestions"] = ["按错误路径核对记忆配置或来源；保留原文件，不自动初始化或修复。"]
    # Reused readers may report the same underlying error; retain one copy.
    for field in ("errors", "warnings"):
        report[field] = list({json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in report[field]}.values())
    report["needs_attention"] = bool(report["errors"] or report["warnings"] or report["source_counts"]["errors"]
        or report["source_counts"]["warnings"] or report["handoff_gaps"]
        or report["memory_state"] in ("unavailable", "selection_required")
        or (report["memory_state"] == "enabled" and (not report["coverage"]["history_complete"]
            or report["documents"].get("state") != "checked_registered_dependencies"
            or report["task"]["state"] == "unknown")))
    return _bounded(report, max_bytes)


def render_text(report):
    state = report["memory_state"]
    labels = {"not_enabled": "尚未启用结构化记忆", "selection_required": "需要选择任务或登记当前任务范围",
              "unavailable": "记忆状态暂不可读取，需核对", "enabled": "已读取保存的任务记忆"}
    lines = ["记忆状态：" + labels[state]]
    task = report.get("task")
    if task:
        lines += [f"当前任务：{_line(task['task_id'])}（{task['state_label']}）", "目标：" + _line(task["goal"]),
                  f"记录档位：{task['detail_label']}；范围来源：{_line(task['source'])}"]
        records = report["records"]
        count = records.get("available_count")
        lines.append(f"已保存记录：本次列出 {records.get('shown_count', 0)} 条；检索到{'至少 ' if not records['count_complete'] else ''}{count if count is not None else '未知数量'} 条。")
        for item in records.get("items", []):
            lines.append("  记录：" + _line(item["path"]))
        claims = [item for item in report.get("verification_reports", []) if item.get("applicability") != "superseded"]
        historical_claims = [item for item in report.get("verification_reports", []) if item.get("applicability") == "superseded"]
        lines.append("当前记录的验证声明：" + ("；".join(dict.fromkeys(CLAIMS.get(item.get("declared_status"), "未知声明") for item in claims)) if claims else "当前检查来源未找到；不能视为已验证"))
        for item in claims[:3]:
            lines.append("  声明来源：" + _line(item.get("source", "未知")))
        if historical_claims:
            lines.append("已被明确替代的历史声明：" + "；".join(dict.fromkeys(CLAIMS.get(item.get("declared_status"), "未知声明") for item in historical_claims)) + "；保留追溯，不计入当前验证缺口。")
        for item in report.get("pending_sources", []):
            lines.append("未完成与下一步来源：" + _line(item["path"]) + "（" + item["purpose"] + "）")
        if not report.get("pending_sources"):
            lines.append("未完成与下一步来源：尚未找到保存的计划或日志。")
        gaps = report.get("handoff_gaps", [])
        lines.append("交接待核对：" + ("、".join(FIELDS.get(field, field) for field in gaps) if gaps else "本次检查未列出结构缺口；仍须核对内容和覆盖范围"))
        if any(item.get("declared_status") in ("not_run", "partial", "failed") for item in claims):
            lines.append("验证说明：包含历史未验证、部分验证或失败声明时，须核对其当前适用性。")
        documents = report.get("documents", {})
        document_labels = {"unavailable": "核对未完成或覆盖范围未知", "changed": "发现变化或冲突，需按路径核对",
            "no_baseline": "没有适用的依赖评审基线，无法判断", "documents_only": "仅核对已登记文档自身，未覆盖实现依赖",
            "review_pending": "已登记范围内未发现变化，仍有文档核对提示",
            "checked_registered_dependencies": "已登记依赖范围内未发现变化；不证明语义正确"}
        lines.append("文档变化：" + document_labels.get(documents.get("state"), "覆盖范围未知"))
        for item in (documents.get("changes", []) + documents.get("conflicts", []))[:3]:
            lines.append("  核对路径：" + _line(item.get("path", "未知")))
        if not report["coverage"].get("history_complete"):
            lines.append("读取范围：历史或正文未完整覆盖，须按需补读。")
        if documents.get("output_truncated"):
            lines.append("文档核对明细已省略，使用 health 补读。")
    candidates = (report.get("selection") or {}).get("candidates", [])
    if not task and candidates:
        lines.append("候选任务：" + "；".join(_line(item["task_id"]) + "（" + STATES.get(item["state"], STATES["unknown"]) + "）" for item in candidates[:6]))
        if len(candidates) > 6:
            lines.append("候选任务展示已省略；使用 --json 查看。")
    for path in report.get("existing_documents", [])[:5]:
        lines.append("已有文档：" + _line(path))
    for item in report.get("errors", [])[:3]:
        lines.append("读取错误：" + _line(item.get("path", "")) + " " + _line(item.get("message", item.get("code", "未知"))))
    for item in report.get("warnings", [])[:3]:
        lines.append("读取提示：" + _line(item.get("path", "")) + " " + _line(item.get("message", WARNINGS.get(item.get("code"), "保存记录存在提示，详见 --json。"))))
    counts = report.get("total_counts", {})
    if counts.get("errors", 0) or counts.get("warnings", 0):
        qualifier = "至少 " if report.get("counts_are_lower_bounds", True) else ""
        lines.append(f"读取问题计数：错误 {qualifier}{counts.get('errors', 0)}，提示 {qualifier}{counts.get('warnings', 0)}；明细以 --json 为准。")
    for suggestion in report.get("suggestions", [])[:3]:
        lines.append("建议：" + _line(suggestion))
    if report.get("output_truncated"):
        lines.append(report["read_hint"])
    lines.append(NOTES)
    text = "\n".join(lines) + "\n"
    budget = report["output_budget_bytes"]
    if len(text.encode("utf-8")) > budget:
        tail = "\n展示已省略；使用 --json 或增大 --max-bytes 查看。\n" + NOTES + "\n"
        text = text.encode("utf-8")[:budget - len(tail.encode("utf-8"))].decode("utf-8", errors="ignore") + tail
    return text


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status",))
    parser.add_argument("--root", required=True)
    parser.add_argument("--task")
    parser.add_argument("--client")
    parser.add_argument("--session")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON，保留来源和覆盖范围")
    parser.add_argument("--max-bytes", type=int, default=16384, help="完整 UTF-8 输出预算，2048–1048576，默认 16384")
    args = parser.parse_args(argv)
    try:
        report = status(args.root, args.task, args.client, args.session, args.max_bytes)
        exit_code = 1 if report["total_counts"]["errors"] else 0
        text = _json(report) if args.json else render_text(report)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        text = _json({"command": "status", "error": str(exc), "business_validation": "not_performed"}) if args.json else "参数错误：" + _line(exc) + "\n"
        exit_code = 2
    sys.stdout.write(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
