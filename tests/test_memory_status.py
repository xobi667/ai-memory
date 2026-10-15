"""Read-only, bounded status observations must not invent saved state or validation."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import memory_status as status
import memory_workflow as workflow
from memory_health import review_documents
from memory_store import configure_scope, record_event


class StatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory_status_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def task(self, task_id="work", plan=None, goal="准确接续中文任务"):
        return configure_scope(self.root, task_id, {"goal": goal, "detail": "standard" if plan else "small",
            "modules": ["project"], "record_fields": list(workflow.FIELDS), "plan": plan,
            "confirmation_summary": "按当前任务授权保存范围"}, confirmed=True)["scope"]

    def event(self, event_id="saved", **fields):
        return record_event(self.root, "work", {"actor": "tester", "event_id": event_id, **fields})

    def state(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def cli(self, *args):
        environment = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "ai_memory.py"),
            "status", "--root", str(self.root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, check=False)

    def test_unregistered_directory_is_normal_and_never_written(self):
        self.write("文档/交接.md", "# 已有交接\n当前任务来自既有文档。\n")
        self.write("README.md", "# 项目\n")
        before = self.state()
        result = status.status(self.root)
        self.assertEqual(result["memory_state"], "not_enabled")
        self.assertFalse(result["errors"])
        self.assertIn("文档/交接.md", result["existing_documents"])
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertEqual(self.state(), before)
        self.assertFalse((self.root / ".ai-memory").exists())
        rendered = status.render_text(result)
        self.assertIn("轻量交接", rendered)
        self.assertIn("未自动保存未记录的聊天", rendered)

    def test_layout_only_project_is_not_corruption_but_invalid_layout_is_reported(self):
        self.write(".ai-memory/layout.json", json.dumps({"schema_version": 1, "document_root": "文档",
            "index": "文档/索引.md", "roles": {"status": "文档/交接.md"}}))
        self.write("文档/交接.md", "现有资料")
        before = self.state()
        result = status.status(self.root)
        self.assertEqual(result["memory_state"], "not_enabled")
        self.assertFalse(result["errors"])
        self.assertEqual(before, self.state())
        self.write(".ai-memory/layout.json", "{")
        result = status.status(self.root)
        self.assertTrue(result["errors"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual(self.cli().returncode, 1)

    def test_orphan_task_and_review_directories_are_not_unregistered_success(self):
        for relative in (".ai-memory/scopes", ".ai-memory/reviews", "docs/memory/journal"):
            with self.subTest(relative=relative):
                directory = self.root / relative
                directory.mkdir(parents=True, exist_ok=True)
                result = status.status(self.root)
                self.assertEqual(result["memory_state"], "unavailable")
                self.assertTrue(result["errors"])
                directory.rmdir()

    def test_multiple_active_tasks_do_not_guess_or_create_binding(self):
        self.task("one")
        self.task("two")
        before = self.state()
        result = status.status(self.root)
        self.assertEqual(result["memory_state"], "selection_required")
        self.assertIsNone(result["task"])
        self.assertEqual({item["task_id"] for item in result["selection"]["candidates"]}, {"one", "two"})
        self.assertEqual(before, self.state())

    def test_stale_binding_never_falls_back_and_explicit_task_wins(self):
        self.task("old")
        self.task("current")
        workflow.bind(self.root, "old", "codex", "会话1")
        (self.root / ".ai-memory/scopes/old.json").unlink()
        before = self.state()
        result = status.status(self.root, client="codex", session="会话1")
        self.assertIsNone(result["task"])
        self.assertEqual(result["errors"][0]["code"], "invalid_binding")
        self.assertEqual(self.cli("--client", "codex", "--session", "会话1").returncode, 1)
        self.assertEqual(status.status(self.root, "current", "codex", "会话1")["task"]["task_id"], "current")
        self.assertEqual(before, self.state())

    def test_saved_claims_never_become_executed_business_validation(self):
        self.task()
        saved = self.event(changed="实现局部改动", verification={"status": "passed", "summary": "记录者称通过",
                           "command": "this-command-must-not-run", "evidence": ["https://example.invalid/proof"]}, next="核对验收")
        before = self.state()
        with patch("subprocess.run", side_effect=AssertionError("status must not execute commands")):
            result = status.status(self.root)
        self.assertEqual(result["task"]["detail_label"], "精简")
        self.assertEqual(result["records"]["available_count"], 1)
        self.assertEqual(result["records"]["items"][0]["path"], saved["path"])
        self.assertTrue(result["records"]["items"][0]["body_checksum_verified"])
        self.assertEqual(result["verification_reports"][0]["declared_status"], "passed")
        self.assertFalse(result["verification_reports"][0]["claim_verified"])
        self.assertEqual(result["business_validation"], "not_performed")
        self.assertEqual(result["documents"]["state"], "no_baseline")
        self.assertIn("声称通过", status.render_text(result))
        self.assertEqual(before, self.state())

    def test_plan_is_primary_next_step_source_over_newer_journal(self):
        self.write("文档/计划.md", "<!-- ai-memory-task-status: active -->\n# 计划\n## 下一步\n按当前计划继续\n")
        self.task(plan="文档/计划.md")
        self.event(next="历史下一步，不应覆盖当前计划")
        result = status.status(self.root)
        self.assertEqual(result["pending_sources"][0]["path"], "文档/计划.md")
        self.assertEqual(result["pending_sources"][0]["kind"], "current_plan")
        self.assertIn("文档/计划.md", status.render_text(result))

    def test_current_verification_is_separate_from_explicitly_replaced_history(self):
        self.task()
        self.event("old", verification={"status": "failed", "summary": "此前结果"})
        self.event("new", supersedes=["old"], verification={"status": "passed", "summary": "本轮结果"})
        result = status.status(self.root)
        self.assertNotIn("verification", result["handoff_gaps"])
        rendered = status.render_text(result)
        current = next(line for line in rendered.splitlines() if line.startswith("当前记录的验证声明："))
        self.assertIn("声称通过", current)
        self.assertNotIn("记录失败", current)
        self.assertIn("已被明确替代的历史声明：记录失败", rendered)
        self.assertNotIn("验证说明：包含历史未验证", rendered)

    def test_unknown_state_blocks_auto_selection_and_remains_unknown_when_explicit(self):
        self.write("文档/计划.md", "# 任务\n[外部资料](https://example.invalid/)\n## 下一步\n核对状态\n")
        self.task(plan="文档/计划.md")
        result = status.status(self.root)
        self.assertIsNone(result["task"])
        self.assertEqual(result["selection"]["candidates"][0]["state"], "unknown")
        explicit = status.status(self.root, "work")
        self.assertEqual(explicit["task"]["state"], "unknown")
        self.assertTrue(explicit["needs_attention"])
        self.assertIn("未知，需核对", status.render_text(explicit))

    def test_external_plan_path_and_links_do_not_read_outside_project(self):
        self.task()
        path = self.root / ".ai-memory/scopes/work.json"
        scope = json.loads(path.read_text(encoding="utf-8"))
        scope["plan"] = "../outside.md"
        path.write_text(json.dumps(scope), encoding="utf-8")
        result = status.status(self.root, "work")
        self.assertTrue(result["errors"])
        self.assertNotEqual(result["documents"].get("state"), "checked_registered_dependencies")

    def test_symlinked_handoff_is_not_followed(self):
        temporary = tempfile.TemporaryDirectory(prefix="outside_memory_")
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name) / "secret.md"
        outside.write_text("outside-content-must-not-be-read", encoding="utf-8")
        try:
            (self.root / "交接.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink privilege unavailable")
        result = status.status(self.root)
        self.assertTrue(result["errors"])
        self.assertNotIn("交接.md", result["existing_documents"])
        self.assertNotIn("outside-content-must-not-be-read", status._json(result))

    def test_document_dependency_changes_have_paths_and_are_not_semantic_proof(self):
        self.task()
        self.write("文档/说明.md", "由 src/app.py 实现")
        self.write("src/app.py", "value = 1\n")
        review_documents(self.root, {"schema_version": 1, "review_id": "review1", "task_id": "work",
            "summary": "记录调用者核对依据", "documents": [{"path": "文档/说明.md", "dependencies": ["src/app.py"]}]})
        self.write("src/app.py", "value = 2\n")
        before = self.state()
        result = status.status(self.root)
        self.assertEqual(result["documents"]["state"], "changed")
        self.assertEqual(result["documents"]["changes"][0]["path"], "src/app.py")
        self.assertIn("document_freshness", result["handoff_gaps"])
        self.assertIn("src/app.py", status.render_text(result))
        self.assertEqual(before, self.state())

    def test_no_scope_and_broken_project_are_not_ready(self):
        self.task()
        (self.root / ".ai-memory/scopes/work.json").unlink()
        result = status.status(self.root)
        self.assertEqual(result["memory_state"], "selection_required")
        self.assertTrue(result["needs_attention"])
        self.assertIsNone(result["task"])
        self.write(".ai-memory/project.json", "broken-json")
        result = status.status(self.root)
        self.assertEqual(result["memory_state"], "unavailable")
        self.assertGreater(result["total_counts"]["errors"], 0)

    def test_missing_document_coverage_never_looks_healthy(self):
        self.task()
        with patch.object(workflow, "handoff", return_value={"checks": [], "gaps": [], "errors": [], "warnings": []}):
            result = status.status(self.root)
        self.assertEqual(result["documents"]["state"], "unavailable")
        self.assertTrue(result["needs_attention"])
        self.assertIn("覆盖范围未知", status.render_text(result))

    def test_document_warning_is_visible_and_keeps_attention_required(self):
        self.task()
        document_health = {"scan_complete": True, "coverage": {"review_snapshots": True},
            "checked_counts": {"dependencies": 1}, "total_counts": {"errors": 0, "warnings": 1},
            "warnings": [{"code": "classification_review_pending", "path": "文档/说明.md",
                          "message": "分类清单仍需按现内容复核。"}]}
        with patch.object(workflow, "handoff", return_value={"document_health": document_health}):
            result = status.status(self.root)
        self.assertEqual(result["documents"]["state"], "review_pending")
        self.assertTrue(result["needs_attention"])
        self.assertIn("分类清单仍需", status.render_text(result))

    def test_omitted_reader_errors_keep_counts_and_nonzero_exit(self):
        self.task()
        restored = {"error_count": 7, "errors": [], "warnings": [], "records": [],
                    "scan_complete": False, "has_more": True, "output_truncated": True}
        with patch.object(status, "resume_memory", return_value=restored), patch.object(workflow, "handoff", return_value={}):
            result = status.status(self.root)
        self.assertEqual(result["total_counts"]["errors"], 7)
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertIn("错误 至少 7", status.render_text(result))
        import io
        with patch.object(status, "status", return_value=result), patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(status.main(["status", "--root", str(self.root)]), 1)

    def test_two_omitting_sources_report_lower_bounds_and_complete_details_deduplicate(self):
        self.task()
        first = {"code": "first_error", "path": "first.md"}
        second = {"code": "second_error", "path": "second.md"}
        restored = {"error_count": 7, "errors": [first], "records": [], "scan_complete": False}
        handoff = {"document_health": {"total_counts": {"errors": 5}, "errors": [second]}}
        with patch.object(status, "resume_memory", return_value=restored), patch.object(workflow, "handoff", return_value=handoff):
            result = status.status(self.root)
        self.assertEqual(result["total_counts"]["errors"], 7)
        self.assertFalse(result["counts_complete"])
        self.assertTrue(result["counts_are_lower_bounds"])
        self.assertIn("错误 至少 7", status.render_text(result))
        compact = status._bounded(result, 2048)
        self.assertFalse(compact["counts_complete"])
        self.assertTrue(compact["counts_are_lower_bounds"])
        restored.update(error_count=1, errors=[first])
        handoff = {"errors": [first], "document_health": {"total_counts": {"errors": 1}, "errors": [first]}}
        with patch.object(status, "resume_memory", return_value=restored), patch.object(workflow, "handoff", return_value=handoff):
            complete = status.status(self.root)
        self.assertEqual(complete["total_counts"]["errors"], 1)
        self.assertTrue(complete["counts_complete"])
        self.assertFalse(complete["counts_are_lower_bounds"])
        self.assertIn("错误 1", status.render_text(complete))

    def test_missing_plan_and_invalid_utf8_are_read_errors_not_success(self):
        plan = self.write("文档/计划.md", "<!-- ai-memory-task-status: active -->\n")
        self.task(plan="文档/计划.md")
        plan.write_bytes(b"\xff\xfeinvalid")
        result = status.status(self.root, "work")
        self.assertGreater(result["total_counts"]["errors"], 0)
        self.assertEqual(self.cli("--task", "work", "--json").returncode, 1)
        plan.unlink()
        result = status.status(self.root, "work")
        self.assertGreater(result["total_counts"]["errors"], 0)
        self.assertTrue(result["needs_attention"])

    def test_unknown_or_paused_selection_warning_is_readable_chinese(self):
        self.task()
        workflow.set_task_status(self.root, "work", "paused")
        rendered = status.render_text(status.status(self.root, "work"))
        self.assertIn("所选任务记录为暂停", rendered)
        self.assertNotIn("selected_task_not_active", rendered)

    def test_long_multiline_sources_are_inert_and_utf8_output_is_bounded(self):
        self.task(goal="准确目标\n伪造标题：\x1b[31m\u202e" + "长文本" * 2000)
        self.event(verification={"status": "not_run", "summary": "没有执行"}, next="下一步" * 4000)
        rendered = status.render_text(status.status(self.root))
        self.assertNotIn("\n伪造标题", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        result = status.status(self.root, max_bytes=2048)
        self.assertTrue(result["output_truncated"])
        self.assertFalse(result["coverage"]["summary_complete"])
        self.assertLessEqual(len(status._json(result).encode("utf-8")), 2048)
        self.assertLessEqual(len(status.render_text(result).encode("utf-8")), 2048)
        for arguments in (("--max-bytes", "2048", "--json"), ("--max-bytes", "2048")):
            output = self.cli(*arguments)
            output.stdout.decode("utf-8", errors="strict")
            self.assertLessEqual(len(output.stdout), 2048)

    def test_truncated_body_and_history_are_visible_even_with_no_read_errors(self):
        self.task()
        for index in range(4):
            self.event("entry" + str(index), changed="已保存", next="继续" + "内容" * 5000)
        result = status.status(self.root)
        self.assertEqual(result["records"]["shown_count"], 3)
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertTrue(result["needs_attention"])
        self.assertIn("未完整覆盖", status.render_text(result))

    def test_corrupt_record_errors_survive_output_omission(self):
        self.task(goal="中文目标" * 2000)
        saved = self.event(changed="原始记录", next="继续")
        path = self.root / saved["path"]
        path.write_text(path.read_text(encoding="utf-8") + "被篡改\n", encoding="utf-8")
        result = status.status(self.root, max_bytes=2048)
        self.assertTrue(result["output_truncated"])
        self.assertGreater(result["total_counts"]["errors"], 0)
        self.assertTrue(result["needs_attention"])
        self.assertEqual(self.cli("--json", "--max-bytes", "2048").returncode, 1)

    def test_cli_dispatch_help_utf8_and_invalid_arguments(self):
        output = self.cli()
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn("尚未启用", output.stdout.decode("utf-8"))
        self.assertEqual(json.loads(self.cli("--json").stdout)["command"], "status")
        self.assertEqual(self.cli("--client", "codex").returncode, 2)
        self.assertEqual(self.cli("--max-bytes", "100").returncode, 2)
        self.assertEqual(self.cli("--task", "../outside").returncode, 2)
        self.assertIn(b"--max-bytes", self.cli("--help").stdout)


if __name__ == "__main__":
    unittest.main()
