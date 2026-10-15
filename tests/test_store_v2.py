"""Compatibility, bounded recovery, relation integrity, and real Git checkout tests."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_store.py"
SPEC = importlib.util.spec_from_file_location("memory_store_v2_under_test", SCRIPT)
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


class StoreV2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="记忆v2_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = {"goal": "跨会话恢复", "detail": "small", "modules": ["project"],
                      "confirmation_summary": "用户确认本任务范围"}

    def configure(self, task="task", **changes):
        return MEMORY.configure_scope(self.root, task, {**self.scope, **changes}, True)

    def record(self, identifier="event", task="task", **changes):
        return MEMORY.record_event(self.root, task, {"event_id": identifier, "actor": "test",
                                   "changed": "修改已完成", "next": "继续核验", **changes})

    def metadata(self, path):
        return json.loads(path.read_bytes().split(b"\n", 1)[0].decode()[len(MEMORY.PREFIX):-4])

    def rewrite_header(self, path, **changes):
        _, body = path.read_bytes().split(b"\n", 1)
        metadata = {**self.metadata(path), **changes}
        path.write_bytes((MEMORY.PREFIX + json.dumps(metadata) + " -->\n").encode() + body)

    def test_crlf_header_and_cross_chunk_body_are_accepted_but_content_change_is_not(self):
        self.configure()
        # Place a CR at the end of the first 65536-byte body read.
        body = b"x" * 65535 + b"\n" + "下一步\n".encode()
        result = self.record()
        path = self.root / result["path"]
        metadata = self.metadata(path)
        metadata["payload_sha256"] = hashlib.sha256(body).hexdigest()
        header = (MEMORY.PREFIX + json.dumps(metadata) + " -->\r\n").encode()
        path.write_bytes(header + body.replace(b"\n", b"\r\n"))
        report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["records"][0]["body_checksum_verified"])
        self.assertTrue(MEMORY.audit_store(self.root)["checks_passed"])
        path.write_bytes(path.read_bytes() + b"modified")
        self.assertFalse(MEMORY.audit_store(self.root)["checks_passed"])

    @unittest.skipUnless(shutil.which("git"), "Git unavailable")
    def test_real_git_autocrlf_checkout_roundtrip(self):
        self.configure()
        result = self.record(changed="一行\n第二行")
        path = self.root / result["path"]
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        git("init", "--quiet")
        git("config", "core.autocrlf", "true")
        git("add", "--", result["path"])
        path.unlink()
        git("checkout-index", "--force", "--", result["path"])
        self.assertIn(b"\r\n", path.read_bytes())
        self.assertTrue(MEMORY.audit_store(self.root)["checks_passed"])
        self.assertEqual(len(MEMORY.resume_memory(self.root)["records"]), 1)

    def test_scope_limit_rejects_before_any_write_and_preserves_update(self):
        with self.assertRaisesRegex(ValueError, "32768"):
            self.configure(goal="细节" * 10000)
        self.assertFalse((self.root / ".ai-memory").exists())
        created = self.configure()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ValueError, "32768"):
            MEMORY.configure_scope(self.root, "task", {**self.scope, "goal": "细节" * 10000}, True, 1)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_shared_lock_without_registration_is_not_a_broken_project(self):
        directory = self.root / ".ai-memory"
        directory.mkdir()
        (directory / "store.lock").write_bytes(b"0")
        report = MEMORY.audit_store(self.root)
        self.assertFalse(report["registered"])
        self.assertTrue(report["checks_passed"], report)
        scopes = directory / "scopes"
        scopes.mkdir()
        (scopes / "orphan.json").write_bytes(b"{}")
        report = MEMORY.audit_store(self.root)
        self.assertFalse(report["checks_passed"])
        self.assertTrue(any(item["path"] == ".ai-memory/project.json" for item in report["errors"]))

    def test_invalid_scope_path_does_not_hide_later_valid_tasks_from_audit(self):
        self.configure("z-valid")
        self.record(task="z-valid")
        directory = self.root / ".ai-memory/scopes"
        for name, is_directory in (("a-broken.json", True), ("history", False)):
            with self.subTest(path=name):
                invalid = directory / name
                if is_directory:
                    invalid.mkdir()
                else:
                    invalid.write_bytes(b"not a directory")
                before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                report = MEMORY.audit_store(self.root)
                self.assertFalse(report["checks_passed"])
                self.assertEqual((report["scope_count"], report["record_count"]), (1, 1))
                self.assertTrue(report["errors"])
                self.assertTrue(all(item["path"] == f".ai-memory/scopes/{name}" for item in report["errors"]))
                self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
                invalid.rmdir() if is_directory else invalid.unlink()

    def test_utc_timestamp_overflow_is_reported_without_hiding_valid_tasks(self):
        configured = self.configure("bad")
        self.configure("good")
        self.record(task="good")
        path = self.root / configured["path"]
        for timestamp in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(timestamp=timestamp):
                path.write_text(json.dumps({**configured["scope"], "updated_at": timestamp}), encoding="utf-8")
                before = {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()}
                for command in ("audit", "resume"):
                    process = subprocess.run([sys.executable, "-B", str(SCRIPT), command, "--root", str(self.root)],
                                             capture_output=True)
                    self.assertEqual(process.returncode, 1, process.stderr.decode("utf-8"))
                    self.assertEqual(process.stderr, b"")
                    report = json.loads(process.stdout.decode("utf-8"))
                    self.assertEqual((report["scope_count"], report["record_count"]), (1, 1))
                    self.assertTrue(any(item.get("path") == configured["path"] and "UTC" in item["message"]
                                        for item in report["errors"]))
                self.assertEqual(before, {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()})

    def test_project_and_scope_schema_versions_require_integers(self):
        configured = self.configure("bad")
        self.configure("good")
        self.record(task="good")
        for relative in (".ai-memory/project.json", configured["path"]):
            path = self.root / relative
            original = path.read_bytes()
            value = json.loads(original)
            for schema_version in (True, 1.0):
                with self.subTest(path=relative, schema_version=schema_version):
                    path.write_text(json.dumps({**value, "schema_version": schema_version}), encoding="utf-8")
                    report = MEMORY.audit_store(self.root)
                    self.assertFalse(report["checks_passed"])
                    self.assertTrue(any(item["path"] == relative for item in report["errors"]))
                    if relative == configured["path"]:
                        self.assertEqual((report["scope_count"], report["record_count"]), (1, 1))
                    else:
                        self.assertFalse(report["registered"])
            path.write_bytes(original)

    def test_journal_schema_versions_require_integers(self):
        self.configure()
        result = self.record("bad")
        self.record("good")
        path = self.root / result["path"]
        for schema_version in (True, 1.0, 2.0, "2"):
            with self.subTest(schema_version=schema_version):
                self.rewrite_header(path, schema_version=schema_version)
                report = MEMORY.audit_store(self.root)
                self.assertFalse(report["checks_passed"])
                self.assertEqual(report["record_count"], 1)
                self.assertTrue(any(item["path"] == result["path"] and "版本必须是整数" in item["message"]
                                    for item in report["errors"]))

    def test_legacy_oversized_scope_is_path_addressable_and_response_is_bounded(self):
        created = self.configure()
        path = self.root / created["path"]
        scope = json.loads(path.read_text(encoding="utf-8"))
        scope["goal"] = "旧详情" * 20000
        path.write_text(json.dumps(scope, ensure_ascii=False), encoding="utf-8")
        report = MEMORY.resume_memory(self.root, max_bytes=64)
        self.assertTrue(report["scopes"][0]["content_omitted"])
        self.assertEqual(report["scopes"][0]["path"], created["path"])
        self.assertLessEqual(len(MEMORY._json_bytes(report)), 32768)
        self.assertIn("legacy_oversized_scope", [item["code"] for item in report["warnings"]])

    def test_scope_discovery_is_independent_of_logs_and_file_mtimes(self):
        old = self.configure("old")
        self.record(task="old")
        newer = self.configure("new")
        os.utime(self.root / old["path"], (2000000000, 2000000000))
        os.utime(self.root / newer["path"], (1, 1))
        report = MEMORY.resume_memory(self.root, limit=1)
        self.assertEqual(report["scopes"][0]["task_id"], "new")
        self.assertEqual(report["scope_count"], 2)
        self.assertEqual(report["scope_omitted_count"], 1)
        self.assertTrue(report["has_more"])

    def test_plan_cannot_cross_a_nested_project_boundary(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        (nested / "plan.md").write_text("Another project", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "另一个项目"):
            self.configure(plan="nested/plan.md")
        self.assertFalse((self.root / ".ai-memory").exists())

    def test_total_output_budget_bounds_cli_bytes_and_reports_omissions(self):
        self.configure()
        for index in range(8):
            self.record(f"event-{index}", changed="长正文" * 2000)
        for budget in (1024, 2048, 4096, 32768):
            report = MEMORY.resume_memory(self.root, max_bytes=1000000, max_output_bytes=budget)
            self.assertLessEqual(len(MEMORY._json_bytes(report)), budget)
            self.assertEqual(report["output_bytes"], len(MEMORY._json_bytes(report)))
            self.assertTrue(report["output_truncated"])
            self.assertEqual(report["record_count"], 8)
        process = subprocess.run([sys.executable, "-B", str(SCRIPT), "resume", "--root", str(self.root),
                                  "--max-output-bytes", "1024"], capture_output=True)
        self.assertLessEqual(len(process.stdout), 1024)
        cli_report = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(cli_report["output_bytes"], len(process.stdout))

    def test_v2_records_scope_revision_and_structured_verification(self):
        self.configure()
        first = self.record("first")
        MEMORY.configure_scope(self.root, "task", {**self.scope, "goal": "范围更新"}, True, 1)
        second = self.record("second", corrects=["first"], verification={"status": "passed", "summary": "测试完成",
                    "command": "python -m unittest", "result": "OK", "revision": "abc123", "evidence": ["test.log"]})
        header = self.metadata(self.root / second["path"])
        self.assertEqual((header["schema_version"], header["scope_revision"], header["corrects"]), (2, 2, ["first"]))
        body = (self.root / second["path"]).read_text(encoding="utf-8")
        self.assertIn("python -m unittest", body)
        self.assertIn("不执行命令或证明业务正确", body)
        self.assertTrue(MEMORY.audit_store(self.root)["checks_passed"])
        self.assertEqual(self.metadata(self.root / first["path"])["scope_revision"], 1)

    def test_critical_sections_fit_before_optional_large_changes(self):
        self.configure()
        self.record(changed="大量实现细节" * 10000,
                    verification={"status": "passed", "summary": "验证完成"})
        report = MEMORY.resume_memory(self.root, max_bytes=900)
        record = report["records"][0]
        self.assertIn("验证完成", record["markdown"])
        self.assertIn("继续核验", record["markdown"])
        self.assertTrue({"verification", "next"}.issubset(record["covered_sections"]))
        self.assertTrue(record["content_truncated"])
        self.assertEqual(report["required_reads"], [])

    def test_fenced_examples_do_not_replace_real_handoff_sections(self):
        self.configure()
        self.record(changed="实现细节" * 1000,
                    verification="验证已完成\n```markdown\n## 下一步 / next\n示例代码里的假下一步\n```",
                    next="真实下一步")
        report = MEMORY.resume_memory(self.root, max_bytes=900)
        record = report["records"][0]
        self.assertIn("验证已完成", record["markdown"])
        self.assertIn("真实下一步", record["markdown"])
        self.assertTrue({"verification", "next"}.issubset(record["covered_sections"]))

    def test_invalid_binding_never_restores_another_task(self):
        from memory_workflow import bind
        configured = self.configure("bound")
        bind(self.root, "bound", "codex", "session-1")
        self.configure("other")
        self.record(task="other")
        (self.root / configured["path"]).unlink()
        report = MEMORY.resume_memory(self.root, client="codex", session="session-1")
        self.assertEqual(report["records"], [])
        self.assertEqual(report["scopes"], [])
        self.assertTrue(any(item.get("code") == "invalid_binding" for item in report["errors"]))

    def test_module_query_includes_paused_and_completed_tasks_despite_other_active_task(self):
        from memory_workflow import set_task_status
        for task, module, status in (("active", "other", "active"),
                                     ("paused", "target", "paused"),
                                     ("completed", "target", "completed")):
            self.configure(task, modules=[module])
            self.record(task, task=task, modules=[module])
            set_task_status(self.root, task, status)
        report = MEMORY.resume_memory(self.root, module="target")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["selection"]["source"], "module_filter")
        self.assertFalse(report["selection"]["selection_required"])
        self.assertEqual({scope["task_id"] for scope in report["scopes"]}, {"paused", "completed"})
        self.assertEqual({item["metadata"]["task_id"] for item in report["records"]}, {"paused", "completed"})

    def test_bound_or_unique_task_keeps_its_scope_before_limit_and_module_filter(self):
        from memory_workflow import bind, set_task_status
        self.configure("bound", modules=["target"])
        self.record("bound-event", task="bound", modules=["target"])
        self.configure("newer", modules=["target"])
        self.record("newer-event", task="newer", modules=["target"])
        bind(self.root, "bound", "codex", "session-1")
        report = MEMORY.resume_memory(self.root, module="target", limit=1, client="codex", session="session-1")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["selection"]["source"], "binding")
        self.assertEqual([scope["task_id"] for scope in report["scopes"]], ["bound"])
        self.assertEqual([item["metadata"]["task_id"] for item in report["records"]], ["bound"])
        self.assertEqual(report["scope_count"], 1)
        self.assertEqual(report["scope_omitted_count"], 0)
        explicit = MEMORY.resume_memory(self.root, task_id="newer", module="target", limit=1,
                                        client="codex", session="session-1")
        self.assertEqual(explicit["selection"]["source"], "explicit")
        self.assertEqual([scope["task_id"] for scope in explicit["scopes"]], ["newer"])
        self.assertEqual([item["metadata"]["task_id"] for item in explicit["records"]], ["newer"])
        set_task_status(self.root, "newer", "completed")
        automatic = MEMORY.resume_memory(self.root, limit=1)
        self.assertEqual(automatic["selection"]["source"], "unique_active")
        self.assertEqual([scope["task_id"] for scope in automatic["scopes"]], ["bound"])
        self.assertEqual(automatic["scope_count"], 1)

    def test_resume_requires_complete_session_parameters(self):
        self.configure()
        for selection in ({"client": "codex"}, {"session": "session-1"}):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, "同时提供"):
                MEMORY.resume_memory(self.root, module="project", **selection)

    def test_missing_self_cross_project_and_cyclic_relations_are_rejected(self):
        self.configure()
        for changes in ({"corrects": ["absent"]}, {"supersedes": ["event"]}):
            with self.assertRaises(ValueError):
                self.record(**changes)
        first = self.record("first")
        second = self.record("second", corrects=["first"])
        self.rewrite_header(self.root / first["path"], corrects=["second"])
        with self.assertRaisesRegex(ValueError, "循环"):
            self.record("third", supersedes=["second"])
        self.assertTrue(any("循环" in item["message"] for item in MEMORY.audit_store(self.root)["errors"]))
        self.rewrite_header(self.root / first["path"], corrects=[], project_id="00000000-0000-0000-0000-000000000000")
        with self.assertRaisesRegex(ValueError, "另一项目"):
            self.record("third", corrects=["first"])

    def test_archived_correction_remains_visible_and_audit_covers_archived_body(self):
        configured = self.configure()
        old = self.record("old")
        correction = self.record("correction", corrects=["old"])
        archive = self.root / ".ai-memory/journal-archive.json"
        archive.write_text(json.dumps({"schema_version": 1, "project_id": configured["scope"]["project_id"],
                           "revision": 1, "events": {"correction": {"review_id": "review-1", "archived_at": MEMORY.utc_now()}}}), encoding="utf-8")
        report = MEMORY.resume_memory(self.root)
        self.assertEqual([item["metadata"]["event_id"] for item in report["records"]], ["old"])
        self.assertEqual(report["records"][0]["related_by"][0]["event_id"], "correction")
        self.assertTrue(report["records"][0]["related_by"][0]["archived"])
        self.assertEqual(len(MEMORY.resume_memory(self.root, include_archived=True)["records"]), 2)
        path = self.root / correction["path"]
        path.write_bytes(path.read_bytes() + b"corrupted")
        self.assertFalse(MEMORY.audit_store(self.root)["checks_passed"])

    def test_fixed_v1_fixture_stays_unchanged_and_project_accepts_new_events(self):
        configured = self.configure()
        # Fixed v0.1 body and header fields; only local project identity is bound.
        body = b"# Task record\n\n## changed\n\nLegacy evidence remains immutable.\n"
        metadata = {"event_id": "legacy", "project_id": configured["scope"]["project_id"], "task_id": "task",
                    "actor": "v0.1-fixture", "time": "2026-01-01T00:00:00.000000+00:00", "modules": ["project"],
                    "payload_sha256": hashlib.sha256(body).hexdigest()}
        path = self.root / "docs/memory/journal/2026-01-01/legacy.md"
        path.parent.mkdir(parents=True)
        original = (MEMORY.PREFIX + json.dumps(metadata, sort_keys=True) + " -->\n").encode() + body
        path.write_bytes(original)
        self.record("modern", corrects=["legacy"])
        report = MEMORY.resume_memory(self.root)
        legacy = next(item for item in report["records"] if item["metadata"]["event_id"] == "legacy")
        self.assertEqual(legacy["historical_scope_revision"], "unknown")
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(MEMORY.audit_store(self.root)["checks_passed"])

    def test_old_structured_verification_retry_stays_idempotent(self):
        configured = self.configure()
        event = {"event_id": "legacy-retry", "actor": "v0.1", "changed": "已完成",
                 "verification": {"status": "passed", "summary": "旧测试通过", "evidence": ["tests/old.py"]}}
        body = "# 任务记录\n\n## 改动 / changed\n\n已完成\n\n## 验证 / verification\n\n- 状态：passed\n- 说明：旧测试通过\n- 证据引用：tests/old.py\n"
        metadata = {"event_id": "legacy-retry", "actor": "v0.1", "project_id": configured["scope"]["project_id"],
                    "task_id": "task", "time": "2026-01-01T00:00:00.000000+00:00", "modules": ["project"],
                    "payload_sha256": hashlib.sha256(body.encode()).hexdigest()}
        path = self.root / "docs/memory/journal/2026-01-01/legacy-retry.md"
        path.parent.mkdir(parents=True)
        original = (MEMORY.PREFIX + json.dumps(metadata) + " -->\n" + body).encode()
        path.write_bytes(original)
        self.assertEqual(MEMORY.record_event(self.root, "task", event)["status"], "preserved")
        self.assertEqual(path.read_bytes(), original)

    def test_audit_streams_large_bodies_instead_of_loading_full_records(self):
        self.configure()
        result = self.record()
        path = self.root / result["path"]
        metadata = self.metadata(path)
        chunk = b"x" * 65536
        digest = hashlib.sha256()
        for _ in range(256):
            digest.update(chunk)
        metadata["payload_sha256"] = digest.hexdigest()
        with path.open("wb") as stream:
            stream.write((MEMORY.PREFIX + json.dumps(metadata) + " -->\n").encode())
            for _ in range(256):
                stream.write(chunk)
        tracemalloc.start()
        try:
            with patch.object(MEMORY, "_read_event", side_effect=AssertionError("must stream")):
                report = MEMORY.audit_store(self.root)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["record_count"], 1)
        self.assertLess(peak, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
