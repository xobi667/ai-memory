"""Observable selection, state, preset and handoff invariants."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import memory_workflow as workflow
from memory_store import configure_scope, record_event


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory_workflow_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write_json(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def task(self, task="work", detail="small", plan=None):
        return configure_scope(self.root, task, {"goal": "继续准确交接", "detail": detail,
            "modules": ["project"], "record_fields": list(workflow.FIELDS), "plan": plan,
            "confirmation_summary": "本任务范围已确认"}, confirmed=True)["scope"]

    def plan(self, task="work", text="<!-- ai-memory-task-status: active -->\n# 当前任务\n"):
        path = self.root / f"文档/任务/{task}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.task(task, "standard", path.relative_to(self.root).as_posix())
        return path

    def test_two_sessions_keep_distinct_tasks_and_explicit_wins(self):
        self.task("first")
        self.task("second")
        workflow.bind(self.root, "first", "codex", "one")
        workflow.bind(self.root, "second", "codex", "two")
        self.assertEqual(workflow.resolve_selection(self.root, client="codex", session="one")["task_id"], "first")
        self.assertEqual(workflow.resolve_selection(self.root, client="codex", session="two")["task_id"], "second")
        self.assertEqual(workflow.resolve_selection(self.root, "second", "codex", "one")["task_id"], "second")
        self.assertTrue(workflow.resolve_selection(self.root, client="claude", session="one")["selection_required"])

    def test_invalid_binding_never_falls_back_and_unbind_recovers(self):
        self.task("old")
        self.task("current")
        workflow.bind(self.root, "old", "claude", "session")
        (self.root / ".ai-memory/scopes/old.json").unlink()
        selected = workflow.resolve_selection(self.root, client="claude", session="session")
        self.assertIsNone(selected["task_id"])
        self.assertEqual(selected["errors"][0]["code"], "invalid_binding")
        workflow.unbind(self.root, "claude", "session")
        self.assertEqual(workflow.resolve_selection(self.root, client="claude", session="session")["task_id"], "current")
        self.assertFalse(workflow.unbind(self.root, "claude", "session")["removed"])

    def test_binding_uses_hash_path_and_does_not_copy_authorization(self):
        scope = self.task()
        before = (self.root / ".ai-memory/scopes/work.json").read_bytes()
        result = workflow.bind(self.root, "work", "codex", "../../中文/session?x=1")
        path = self.root / result["path"]
        self.assertRegex(path.name, r"^[0-9a-f]{64}\.json$")
        binding = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("confirmation", binding)
        self.assertNotIn("revision", binding)
        self.assertEqual(before, (self.root / ".ai-memory/scopes/work.json").read_bytes())
        self.assertEqual(scope["revision"], 1)

    def test_foreign_binding_is_rejected_and_explicit_task_still_works(self):
        self.task()
        result = workflow.bind(self.root, "work", "codex", "session")
        path = self.root / result["path"]
        binding = json.loads(path.read_text(encoding="utf-8"))
        binding["project_id"] = str(uuid.uuid4())
        self.write_json(result["path"], binding)
        self.assertIsNone(workflow.resolve_selection(self.root, client="codex", session="session")["task_id"])
        self.assertEqual(workflow.resolve_selection(self.root, "work", "codex", "session")["task_id"], "work")

    def test_unique_active_ignores_finished_but_not_unknown_tasks(self):
        self.task("active")
        self.task("old")
        workflow.set_task_status(self.root, "old", "completed")
        self.assertEqual(workflow.resolve_selection(self.root)["task_id"], "active")
        self.plan("unmapped", "# 旧计划\n状态暂不明确\n")
        self.assertIsNone(workflow.resolve_selection(self.root)["task_id"])
        workflow.set_task_status(self.root, "unmapped", "paused")
        self.assertEqual(workflow.resolve_selection(self.root)["task_id"], "active")
        selected = workflow.resolve_selection(self.root, "old")
        self.assertEqual(selected["task_id"], "old")
        self.assertEqual(selected["warnings"][0]["state"], "completed")

    def test_invalid_scope_and_scan_limit_do_not_guess_uniqueness(self):
        self.task()
        self.write_json(".ai-memory/scopes/broken.json", {"task_id": "broken"})
        self.assertFalse(workflow.resolve_selection(self.root)["scan_complete"])
        (self.root / ".ai-memory/scopes/broken.json").unlink()
        for index in range(workflow.MAX_SCOPE_SCAN):
            (self.root / f".ai-memory/scopes/{index}.txt").write_text("", encoding="utf-8")
        result = workflow.resolve_selection(self.root)
        self.assertFalse(result["scan_complete"])
        self.assertIsNone(result["task_id"])
        self.assertEqual(workflow.resolve_selection(self.root, "work")["task_id"], "work")

    def test_oversized_legacy_scope_reports_path_without_loading_goal(self):
        self.task()
        path = self.root / ".ai-memory/scopes/work.json"
        scope = json.loads(path.read_text(encoding="utf-8"))
        scope["goal"] = "x" * workflow.MAX_SCOPE_BYTES
        self.write_json(".ai-memory/scopes/work.json", scope)
        selection = workflow.resolve_selection(self.root, "work")
        self.assertIsNone(selection["task_id"])
        self.assertIn("32 KiB", selection["errors"][0]["message"])
        self.assertEqual(selection["errors"][0]["path"], ".ai-memory/scopes/work.json")

    def test_small_status_updates_do_not_change_scope_revision_or_confirmation(self):
        before = self.task()
        result = workflow.set_task_status(self.root, "work", "paused")
        after = workflow.load_scope(self.root, "work")
        self.assertEqual(result["source"], ".ai-memory/scopes/work.json")
        for field in before:
            if field != "task_status":
                self.assertEqual(before[field], after[field])
        self.assertEqual(workflow.task_state(self.root, after), "paused")

    def test_plan_status_ignores_scope_status_and_preserves_other_content(self):
        path = self.plan()
        before = path.read_text(encoding="utf-8")
        scope = workflow.load_scope(self.root, "work")
        scope["task_status"] = "completed"
        self.write_json(".ai-memory/scopes/work.json", scope)
        original = (self.root / ".ai-memory/scopes/work.json").read_bytes()
        self.assertEqual(workflow.task_state(self.root, scope), "active")
        workflow.set_task_status(self.root, "work", "paused")
        self.assertEqual(path.read_text(encoding="utf-8"), before.replace("status: active", "status: paused"))
        self.assertEqual(original, (self.root / ".ai-memory/scopes/work.json").read_bytes())

    def test_missing_plan_does_not_fall_back_to_scope(self):
        path = self.plan()
        path.unlink()
        scope = workflow.load_scope(self.root, "work")
        scope["task_status"] = "active"
        self.assertEqual(workflow.task_state(self.root, scope), "unknown")
        with self.assertRaises(OSError):
            workflow.set_task_status(self.root, "work", "active")

    def test_status_mapping_updates_only_primary_plan_field(self):
        path = self.plan(text="# 外部计划\n- 状态：进行中\n\n## 下一步\n继续实现。\n")
        scope = workflow.load_scope(self.root, "work")
        self.write_json(".ai-memory/status-mappings.json", {"schema_version": 1, "project_id": scope["project_id"],
            "tasks": {"work": {"field": "状态", "values": {"active": "进行中", "paused": "暂停", "completed": "已完成"}}}})
        self.assertEqual(workflow.task_state(self.root, scope), "active")
        workflow.set_task_status(self.root, "work", "completed")
        self.assertIn("状态：已完成", path.read_text(encoding="utf-8"))
        self.assertNotIn("ai-memory-task-status", path.read_text(encoding="utf-8"))
        self.assertNotIn("task_status", workflow.load_scope(self.root, "work"))

    def test_status_conflicts_and_examples_cannot_become_authoritative(self):
        path = self.plan(text="```md\n<!-- ai-memory-task-status: completed -->\n```\n# Plan\n")
        self.assertEqual(workflow.task_state(self.root, workflow.load_scope(self.root, "work")), "unknown")
        workflow.set_task_status(self.root, "work", "active")
        self.assertEqual(workflow.task_state(self.root, workflow.load_scope(self.root, "work")), "active")
        path.write_text(path.read_text(encoding="utf-8") + "\n<!-- ai-memory-task-status: paused -->\n", encoding="utf-8")
        self.assertEqual(workflow.task_state(self.root, workflow.load_scope(self.root, "work")), "unknown")
        with self.assertRaisesRegex(ValueError, "重复"):
            workflow.set_task_status(self.root, "work", "paused")

    def test_preset_only_changes_recommendation_and_requires_confirmation(self):
        self.task()
        before = (self.root / ".ai-memory/scopes/work.json").read_bytes()
        self.assertFalse(workflow.project_preset(self.root)["saved"])
        with self.assertRaises(ValueError):
            workflow.project_preset(self.root, {"detail": "small"})
        saved = workflow.project_preset(self.root, {"detail": "small", "record_fields": ["changed", "verification", "next"]}, True)
        self.assertTrue(saved["requires_task_confirmation"])
        self.assertEqual(workflow.project_preset(self.root)["preset"]["defaults"]["detail"], "small")
        self.assertEqual(before, (self.root / ".ai-memory/scopes/work.json").read_bytes())
        with self.assertRaises(ValueError):
            workflow.project_preset(self.root, {"long_term_auto": True}, True)

    def test_workflow_metadata_rejects_non_integer_schema_versions(self):
        self.plan(text="# 外部计划\n- 状态：进行中\n")
        scope = workflow.load_scope(self.root, "work")
        self.write_json(".ai-memory/status-mappings.json", {"schema_version": 1, "project_id": scope["project_id"],
            "tasks": {"work": {"field": "状态", "values": {"active": "进行中", "paused": "暂停", "completed": "已完成"}}}})
        binding = workflow.bind(self.root, "work", "codex", "session-1")
        workflow.project_preset(self.root, {"detail": "small"}, True)
        files = {"project": ".ai-memory/project.json", "scope": ".ai-memory/scopes/work.json",
                 "mapping": ".ai-memory/status-mappings.json", "binding": binding["path"],
                 "preset": ".ai-memory/presets.json"}
        for kind, relative in files.items():
            path = self.root / relative
            original = path.read_bytes()
            for version in (True, 1.0):
                with self.subTest(metadata=kind, version=version):
                    value = json.loads(original)
                    value["schema_version"] = version
                    self.write_json(relative, value)
                    try:
                        if kind in ("project", "scope"):
                            with self.assertRaises(ValueError):
                                workflow.load_scope(self.root, "work")
                        elif kind == "mapping":
                            result = workflow.set_task_status(self.root, "work")
                            self.assertEqual(result["status"], "unknown")
                            self.assertTrue(result["errors"])
                        elif kind == "binding":
                            result = workflow.resolve_selection(self.root, client="codex", session="session-1")
                            self.assertIsNone(result["task_id"])
                            self.assertTrue(any(item["code"] == "invalid_binding" for item in result["errors"]))
                        else:
                            with self.assertRaises(ValueError):
                                workflow.project_preset(self.root)
                    finally:
                        path.write_bytes(original)

    def test_custom_layout_is_supported_without_creating_default_files(self):
        self.task()
        layout = {"schema_version": 1, "document_root": "文档", "index": "文档/索引.md",
                  "roles": {"status": "文档/当前状态.md", "rules": "AGENTS.md"}}
        self.write_json(".ai-memory/layout.json", layout)
        loaded = workflow.load_layout(self.root)
        self.assertEqual({key: loaded[key] for key in layout}, layout)
        self.assertTrue(loaded["explicit"])
        self.assertFalse((self.root / "DEV.md").exists())
        layout["roles"]["status"] = "../外部.md"
        self.write_json(".ai-memory/layout.json", layout)
        with self.assertRaises(ValueError):
            workflow.load_layout(self.root)

    def test_handoff_identifies_not_run_and_missing_next_without_claiming_success(self):
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "one",
            "changed": "修复文件", "verification": {"status": "not_run", "summary": "尚未执行"}})
        result = workflow.handoff(self.root, "work")
        states = {item["field"]: item["status"] for item in result["checks"]}
        self.assertEqual(states["verification"], "not_run_or_partial")
        self.assertEqual(states["next"], "missing")
        self.assertEqual(result["business_validation"], "not_performed")
        self.assertTrue(result["semantic_review_required"])
        self.assertNotIn("constraints", states)

    def test_handoff_honors_structured_verification_status_without_summary_keywords(self):
        cases = (("partial", "仅覆盖单元测试", "not_run_or_partial"),
                 ("not_run", "检查安排在后续阶段", "not_run_or_partial"),
                 ("failed", "实际结果与预期不符", "failure_reported"),
                 ("passed", "所有断言符合预期", "present"))
        for declared, summary, expected in cases:
            with self.subTest(status=declared):
                self.task(declared)
                record_event(self.root, declared, {"actor": "tester", "event_id": declared,
                    "changed": "完成实现", "next": "继续核验",
                    "verification": {"status": declared, "summary": summary}})
                result = workflow.handoff(self.root, declared)
                states = {item["field"]: item["status"] for item in result["checks"]}
                self.assertEqual(states["verification"], expected)
                self.assertEqual("verification" in result["gaps"], declared != "passed")
                self.assertEqual([item["declared_status"] for item in result["verification_reports"]], [declared])
                self.assertTrue(all(item["claim_verified"] is False for item in result["verification_reports"]))
                self.assertEqual(result["business_validation"], "not_performed")
                self.assertTrue(result["semantic_review_required"])

    def test_handoff_explicit_verified_supersession_keeps_old_claims_as_history(self):
        self.task()
        for event_id, declared in (("partial", "partial"), ("failure", "failed")):
            record_event(self.root, "work", {"actor": "tester", "event_id": event_id,
                "verification": {"status": declared, "summary": "此前执行结果"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "done",
            "time": "2000-01-01T00:00:00Z",  # Explicit relation, not chronological ordering, determines applicability.
            "supersedes": ["partial", "failure"], "verification": {"status": "passed", "summary": "本轮断言符合预期"}})
        result = workflow.handoff(self.root, "work")
        verification = next(item for item in result["checks"] if item["field"] == "verification")
        self.assertEqual(verification["status"], "present")
        self.assertNotIn("verification", result["gaps"])
        reports = {item["event_id"]: item for item in result["verification_reports"]}
        self.assertEqual(reports["done"]["applicability"], "current")
        for event_id in ("partial", "failure"):
            self.assertEqual(reports[event_id]["applicability"], "superseded")
            self.assertEqual(reports[event_id]["superseded_by"], ["done"])
        self.assertFalse(any(item["code"] == "mixed_verification_history" for item in result["warnings"]))
        self.assertTrue(all(item["claim_verified"] is False for item in result["verification_reports"]))
        self.assertEqual(result["business_validation"], "not_performed")

    def test_handoff_does_not_infer_replacement_from_time_or_corrects(self):
        for task_id, relation in (("plain", {}), ("corrected", {"corrects": ["corrected-old"]})):
            self.task(task_id)
            record_event(self.root, task_id, {"actor": "tester", "event_id": task_id + "-old",
                "verification": {"status": "failed", "summary": "此前结果"}})
            record_event(self.root, task_id, {"actor": "tester", "event_id": task_id + "-new", **relation,
                "verification": {"status": "passed", "summary": "本轮结果"}})
            result = workflow.handoff(self.root, task_id)
            self.assertIn("verification", result["gaps"])
            self.assertTrue(all(item["applicability"] == "current" for item in result["verification_reports"]))
            self.assertIn("mixed_verification_history", [item["code"] for item in result["warnings"]])

    def test_handoff_replacement_requires_one_structured_verification_claim(self):
        for task_id, fields in (("absent", {"changed": "仅记录改动"}),
                               ("text", {"verification": "调用者说已通过"}),
                               ("ambiguous", {"verification": "- 状态：passed\n- 状态：failed"}),
                               ("mixed-unknown", {"verification": "- 状态：passed\n- 状态：unknown"}),
                               ("mixed-label", {"verification": "- 状态：passed\n* STATUS: unknown"}),
                               ("unknown", {"verification": "- 状态：unknown"}),
                               ("prefix", {"verification": "- 状态：passed-but-pending"}),
                               ("suffix", {"verification": "- 状态：passed 附加未验证声明"})):
            self.task(task_id)
            record_event(self.root, task_id, {"actor": "tester", "event_id": task_id + "-old",
                "verification": {"status": "failed", "summary": "此前结果"}})
            record_event(self.root, task_id, {"actor": "tester", "event_id": task_id + "-new",
                "supersedes": [task_id + "-old"], **fields})
            result = workflow.handoff(self.root, task_id)
            self.assertIn("verification", result["gaps"])
            old = next(item for item in result["verification_reports"] if item["event_id"] == task_id + "-old")
            self.assertEqual(old["applicability"], "current")

    def test_handoff_changed_examples_cannot_become_replacement_verification(self):
        cases = {
            "nested": "更新文档中的状态格式示例；本事件没有运行验证。\n\n### verification example\n- status: passed",
            "second-level": "更新格式示例。\n\n## 验证 / verification\n- 状态：passed",
            "fenced": "更新格式示例。\n\n```md\n## 验证 / verification\n- 状态：passed\n```",
            "foreign-heading": "更新格式示例。\n\n## verification example\n- status: passed",
        }
        for task_id, changed in cases.items():
            with self.subTest(task_id=task_id):
                self.task(task_id)
                record_event(self.root, task_id, {"actor": "tester", "event_id": task_id + "-old",
                    "verification": {"status": "failed", "summary": "此前结果"}})
                record_event(self.root, task_id, {"actor": "tester", "event_id": task_id + "-new",
                    "supersedes": [task_id + "-old"], "changed": changed})
                result = workflow.handoff(self.root, task_id)
                self.assertIn("verification", result["gaps"])
                old = next(item for item in result["verification_reports"] if item["event_id"] == task_id + "-old")
                self.assertEqual(old["applicability"], "current")

    def test_handoff_duplicate_generated_verification_sections_are_ambiguous(self):
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "old",
            "verification": {"status": "failed", "summary": "此前结果"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "new", "supersedes": ["old"],
            "changed": "更新格式示例。\n\n## 验证 / verification\n- 状态：passed",
            "verification": {"status": "passed", "summary": "本轮结果"}})
        result = workflow.handoff(self.root, "work")
        self.assertIn("verification", result["gaps"])
        old = next(item for item in result["verification_reports"] if item["event_id"] == "old")
        self.assertEqual(old["applicability"], "current")

    def test_handoff_truncated_or_corrupt_replacement_cannot_retire_failure(self):
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "old",
            "verification": {"status": "failed", "summary": "此前结果"}})
        saved = record_event(self.root, "work", {"actor": "tester", "event_id": "new", "supersedes": ["old"],
            "changed": "x" * (2 * workflow.MAX_PLAN_BYTES),
            "verification": {"status": "passed", "summary": "本轮结果"}, "next": "继续"})
        result = workflow.handoff(self.root, "work")
        self.assertIn("verification", result["gaps"])
        self.assertIn("partial_history", [item["code"] for item in result["warnings"]])
        self.assertTrue(all(item["applicability"] == "current" for item in result["verification_reports"]))
        path = self.root / saved["path"]
        path.write_bytes(path.read_bytes() + b"changed")
        result = workflow.handoff(self.root, "work")
        self.assertTrue(result["errors"])
        self.assertIn("verification", result["gaps"])

    def test_handoff_incomplete_or_erroneous_relation_report_does_not_retire_failure(self):
        from memory_store import resume_memory
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "old",
            "verification": {"status": "failed", "summary": "此前结果"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "new", "supersedes": ["old"],
            "verification": {"status": "passed", "summary": "本轮结果"}})
        restored = resume_memory(self.root, "work")
        for changed in ({"output_truncated": True}, {"scan_complete": False}, {"error_count": 1, "errors": []}):
            with self.subTest(changed=changed), patch("memory_store.resume_memory", return_value={**restored, **changed}):
                result = workflow.handoff(self.root, "work")
            self.assertIn("verification", result["gaps"])
            self.assertTrue(all(item["applicability"] == "current" for item in result["verification_reports"]))

    def test_handoff_unread_replacement_chain_does_not_choose_a_selected_pass(self):
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "old", "time": "2030-01-01T00:00:00Z",
            "verification": {"status": "failed", "summary": "此前结果"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "middle", "time": "2031-01-01T00:00:00Z",
            "supersedes": ["old"], "verification": {"status": "passed", "summary": "中间结果"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "unread", "time": "2000-01-01T00:00:00Z",
            "supersedes": ["middle"], "verification": {"status": "failed", "summary": "需读取的替代结果"}})
        for index in range(9):
            record_event(self.root, "work", {"actor": "tester", "event_id": "extra" + str(index),
                "time": "2026-01-01T00:00:00Z", "changed": "其他增量"})
        result = workflow.handoff(self.root, "work")
        self.assertNotIn("unread", [item["event_id"] for item in result["verification_reports"]])
        self.assertIn("verification", result["gaps"])
        self.assertTrue(all(item["applicability"] == "current" for item in result["verification_reports"]))

    def test_handoff_cross_task_supersession_cannot_retire_local_failure(self):
        self.task()
        self.task("other")
        record_event(self.root, "work", {"actor": "tester", "event_id": "old",
            "verification": {"status": "failed", "summary": "本任务结果"}})
        record_event(self.root, "other", {"actor": "tester", "event_id": "other-new", "supersedes": ["old"],
            "verification": {"status": "passed", "summary": "其他任务结果"}})
        result = workflow.handoff(self.root, "work")
        self.assertIn("verification", result["gaps"])
        self.assertEqual(result["verification_reports"][0]["applicability"], "current")

    def test_handoff_supersession_does_not_hide_another_current_failure(self):
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "old",
            "verification": {"status": "partial", "summary": "原始范围"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "pass", "supersedes": ["old"],
            "verification": {"status": "passed", "summary": "替代结果"}})
        record_event(self.root, "work", {"actor": "tester", "event_id": "independent",
            "verification": {"status": "failed", "summary": "仍适用的另一项验证"}})
        result = workflow.handoff(self.root, "work")
        check = next(item for item in result["checks"] if item["field"] == "verification")
        self.assertEqual(check["status"], "failure_reported")
        self.assertIn("mixed_verification_history", [item["code"] for item in result["warnings"]])

    def test_handoff_plain_plan_text_is_not_a_conflicting_structured_status(self):
        self.plan(text="<!-- ai-memory-task-status: active -->\n## Verification\n详见已保存的执行记录。\n")
        record_event(self.root, "work", {"actor": "tester", "event_id": "pass",
            "verification": {"status": "passed", "summary": "断言符合预期"}})
        result = workflow.handoff(self.root, "work")
        self.assertNotIn("verification", result["gaps"])
        self.assertNotIn("mixed_verification_history", [item["code"] for item in result["warnings"]])

    def test_major_handoff_requires_additional_decision_information(self):
        path = self.root / "plan.md"
        path.write_text("<!-- ai-memory-task-status: active -->\n# 任务\n## 下一步\n运行真实验证。\n", encoding="utf-8")
        self.task(detail="major", plan="plan.md")
        result = workflow.handoff(self.root, "work")
        self.assertTrue({"impact", "tradeoffs", "rollback", "acceptance"}.issubset(result["gaps"]))
        self.assertNotIn("next", result["gaps"])

    def test_handoff_current_next_overrides_historical_next_and_checks_evidence(self):
        self.plan(text="<!-- ai-memory-task-status: active -->\n# 当前计划\n## 下一步\n待补充\n")
        record_event(self.root, "work", {"actor": "tester", "event_id": "one", "changed": "已完成旧步骤",
            "next": "旧步骤不再是当前下一步", "verification": "通过，见 [结果](missing.txt)"})
        result = workflow.handoff(self.root, "work")
        self.assertIn("next", result["gaps"])
        self.assertFalse(result["evidence_links"][0]["exists"])
        self.assertFalse(result["evidence_links"][0]["claim_verified"])

    def test_cli_works_through_real_process(self):
        self.task()
        result = subprocess.run([sys.executable, str(SCRIPTS / "memory_workflow.py"), "task-status",
                                 "--root", str(self.root), "--task", "work", "--status", "paused"],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "paused")

    def test_partial_history_reports_unchecked_gaps_instead_of_complete_absence(self):
        self.task()
        record_event(self.root, "work", {"actor": "tester", "event_id": "old", "next": "最早记录的下一步"})
        for index in range(10):
            record_event(self.root, "work", {"actor": "tester", "event_id": f"later-{index}", "changed": "阶段证据"})
        result = workflow.handoff(self.root, "work")
        check = next(item for item in result["checks"] if item["field"] == "next")
        self.assertEqual(check["status"], "not_found_in_checked_sources")
        self.assertIn("partial_history", [item["code"] for item in result["warnings"]])

    def test_handoff_checks_declared_root_relative_evidence_without_running_commands(self):
        self.task()
        (self.root / "结果.log").write_text("测试记录", encoding="utf-8")
        record_event(self.root, "work", {"actor": "tester", "event_id": "proof",
            "verification": {"status": "passed", "summary": "记录者声明通过", "command": "NEVER_EXECUTE_THIS_COMMAND",
                             "result": "0", "revision": "abc1234", "evidence": ["结果.log", "missing.log", "git:abc1234"]},
            "next": "核对真实结果"})
        result = workflow.handoff(self.root, "work")
        links = {item.get("path"): item for item in result["evidence_links"] if "path" in item}
        self.assertTrue(links["结果.log"]["exists"])
        self.assertFalse(links["missing.log"]["exists"])
        self.assertTrue(all(item["claim_verified"] is False for item in result["evidence_links"]))
        self.assertEqual(result["business_validation"], "not_performed")

    def test_failed_binding_to_unregistered_project_does_not_create_metadata(self):
        with self.assertRaises(OSError):
            workflow.bind(self.root, "missing", "codex", "session")
        self.assertFalse((self.root / ".ai-memory").exists())

    def test_handoff_reports_changed_implementation_without_marking_document_wrong(self):
        from memory_health import review_documents
        self.task()
        document = self.root / "说明.md"
        dependency = self.root / "app.py"
        document.write_text("# 运行说明\n返回旧值。\n", encoding="utf-8")
        dependency.write_text("VALUE = 1\n", encoding="utf-8")
        review_documents(self.root, {"schema_version": 1, "review_id": "checked",
            "summary": "核对说明与当前实现", "task_id": "work",
            "documents": [{"path": "说明.md", "dependencies": ["app.py"]}]})
        initial = workflow.handoff(self.root, "work")
        self.assertNotIn("document_freshness", initial["gaps"])
        before = document.read_bytes()
        dependency.write_text("VALUE = 2\n", encoding="utf-8")
        changed = workflow.handoff(self.root, "work")
        self.assertIn("document_freshness", changed["gaps"])
        self.assertTrue(changed["document_health"]["needs_attention"])
        self.assertEqual(changed["business_validation"], "not_performed")
        self.assertEqual(document.read_bytes(), before)

    def test_handoff_does_not_hide_invalid_document_review_metadata(self):
        self.task()
        self.write_json(".ai-memory/reviews/broken/evidence.json", {"schema_version": True})
        result = workflow.handoff(self.root, "work")
        self.assertIn("document_health", result["gaps"])
        self.assertFalse(result["document_health"]["scan_complete"])
        self.assertTrue(any(error["code"] == "document_health_errors" for error in result["errors"]))

    def test_nested_project_plan_is_not_a_status_source(self):
        path = self.plan()
        self.write_json("文档/.ai-memory/project.json", {"schema_version": 1, "project_id": str(uuid.uuid4())})
        scope = workflow.load_scope(self.root, "work")
        self.assertEqual(workflow.task_state(self.root, scope), "unknown")
        with self.assertRaisesRegex(ValueError, "另一个项目"):
            workflow.set_task_status(self.root, "work", "completed")
        self.assertIn("active", path.read_text(encoding="utf-8"))

    def test_existing_chinese_index_is_discovered_by_shared_layout(self):
        self.task()
        (self.root / "文档").mkdir()
        (self.root / "文档/文档索引.md").write_text("# 已有索引", encoding="utf-8")
        layout = workflow.load_layout(self.root)
        self.assertEqual(layout["index"], "文档/文档索引.md")
        self.assertFalse(layout["explicit"])

    def test_selection_rejects_scope_that_store_would_reject(self):
        scope = self.task()
        for field, value in (("mode", "always_auto"), ("long_term_auto", True),
                             ("record_fields", ["unsupported"]), ("updated_at", "not-a-date")):
            with self.subTest(field=field):
                changed = {**scope, field: value}
                self.write_json(".ai-memory/scopes/work.json", changed)
                result = workflow.resolve_selection(self.root, "work")
                self.assertIsNone(result["task_id"])
                self.assertEqual(result["errors"][0]["code"], "invalid_task")


if __name__ == "__main__":
    unittest.main()
