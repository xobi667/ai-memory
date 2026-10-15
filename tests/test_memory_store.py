"""持久化故障、并发、范围边界和恢复可读性的行为测试。"""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_store.py"
SPEC = importlib.util.spec_from_file_location("memory_store", SCRIPT)
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="记忆内核_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "中文项目"
        self.root.mkdir()
        self.scope = {"goal": "修复当前模块并留下交接证据", "detail": "small", "modules": ["前端", "后端"],
                      "confirmation_summary": "用户已确认记录本任务改动、踩坑、验证和下一步；候选规则需再确认。"}

    def configure(self, task="fix-1", **kwargs):
        return MEMORY.configure_scope(self.root, task, self.scope, confirmed=True, **kwargs)

    def event(self, identifier="event-1", **changes):
        return {"event_id": identifier, "actor": "codex/agent-1", "changed": ["修复中文路径处理"],
                "pitfall": ["相对路径解析错误"], "fix": ["先验证项目边界"], "next": "核验下一模块",
                "verification": {"status": "passed", "summary": "只报告已运行测试的结果", "evidence": ["tests/test_paths.py"]},
                **changes}

    def request_file(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *arguments):
        process = subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, arguments)],
                                 capture_output=True, encoding="utf-8", check=False)
        return process.returncode, json.loads(process.stdout)

    def test_confirmation_required_before_any_store_creation(self):
        with self.assertRaises(ValueError):
            MEMORY.configure_scope(self.root, "fix-1", self.scope)
        self.assertFalse((self.root / ".ai-memory").exists())
        with self.assertRaises(FileNotFoundError):
            MEMORY.record_event(self.root, "fix-1", self.event())
        self.assertFalse((self.root / ".ai-memory").exists())

    def test_scope_revision_cas_preserves_previous_bytes(self):
        initial = self.configure()
        scope_path = self.root / initial["path"]
        before = scope_path.read_bytes()
        unchanged = self.configure()
        self.assertEqual(unchanged["status"], "preserved")
        self.assertEqual(scope_path.read_bytes(), before)
        self.scope["modules"] = ["前端"]
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(scope_path.read_bytes(), before)
        updated = self.configure(expected_revision=1)
        self.assertEqual(updated["scope"]["revision"], 2)
        self.assertEqual((self.root / ".ai-memory/scopes/history/fix-1/revision-1.json").read_bytes(), before)
        with self.assertRaises(ValueError):
            MEMORY.configure_scope(self.root, "fix-1", {**self.scope, "goal": "再改"}, True, 1)

    def test_markdown_record_is_dated_readable_and_idempotent(self):
        self.configure()
        event = self.event(time="2026-10-15T12:30:00+08:00", long_term_candidates=["考虑统一路径处理；尚未批准"])
        result = MEMORY.record_event(self.root, "fix-1", event)
        path = self.root / result["path"]
        self.assertEqual(path.parent.name, "2026-10-15")
        self.assertEqual(path.suffix, ".md")
        raw = path.read_bytes()
        self.assertIn("## 踩坑 / pitfall", raw.decode("utf-8"))
        self.assertIn("尚未批准，不生效", raw.decode("utf-8"))
        repeated = MEMORY.record_event(self.root, "fix-1", event)
        self.assertEqual(repeated["status"], "preserved")
        self.assertEqual(path.read_bytes(), raw)
        with self.assertRaises(ValueError):
            MEMORY.record_event(self.root, "fix-1", {**event, "changed": "不同内容"})
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(MEMORY.audit_store(self.root)["record_count"], 1)

    def test_automatic_timestamp_does_not_break_retry_idempotence(self):
        self.configure()
        first = MEMORY.record_event(self.root, "fix-1", self.event())
        second = MEMORY.record_event(self.root, "fix-1", self.event())
        self.assertEqual(second["status"], "preserved")
        self.assertEqual(first["path"], second["path"])

    def test_record_checks_matching_name_without_enumerating_each_days_logs(self):
        self.configure()
        MEMORY.record_event(self.root, "fix-1", self.event("previous", time="2026-10-14T00:00:00Z"))
        MEMORY.record_event(self.root, "fix-1", self.event("recent", time="2026-10-15T00:00:00Z"))
        original_iterdir = Path.iterdir
        journal = self.root / MEMORY.JOURNAL

        def guarded_iterdir(path):
            if path.parent == journal:
                raise AssertionError("写入不应枚举日期目录中的所有日志")
            return original_iterdir(path)

        with patch.object(Path, "iterdir", guarded_iterdir):
            result = MEMORY.record_event(self.root, "fix-1", self.event("new", time="2026-10-16T00:00:00Z"))
            self.assertEqual(result["status"], "created")
            with self.assertRaisesRegex(ValueError, "event_id 已存在"):
                MEMORY.record_event(self.root, "fix-1", self.event("previous", time="2026-10-16T00:00:00Z"))

    def test_existing_duplicate_ids_across_dates_are_rejected_even_on_retry(self):
        self.configure()
        event = self.event(time="2026-10-14T00:00:00Z")
        result = MEMORY.record_event(self.root, "fix-1", event)
        original = self.root / result["path"]
        header, body = original.read_bytes().split(b"\n", 1)
        metadata = json.loads(header.decode("utf-8")[len(MEMORY.PREFIX):-4])
        metadata["time"] = "2026-10-15T00:00:00.000000+00:00"
        duplicate = original.parent.parent / "2026-10-15" / original.name
        duplicate.parent.mkdir()
        duplicate.write_bytes((MEMORY.PREFIX + json.dumps(metadata) + " -->\n").encode("utf-8") + body)
        before = {path: path.read_bytes() for path in (original, duplicate)}
        with self.assertRaisesRegex(ValueError, "不同日期存在重复"):
            MEMORY.record_event(self.root, "fix-1", event)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_record_duplicate_lookup_rejects_linked_days_and_matching_paths(self):
        self.configure()
        outside = self.root.parent / "outside-journal"
        outside.mkdir()
        outside_file = outside / "event-1.md"
        outside_file.write_bytes(b"external content")
        journal = self.root / MEMORY.JOURNAL
        journal.mkdir(parents=True)
        day = journal / "2026-10-15"
        try:
            os.symlink(outside, day, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        with self.assertRaisesRegex(ValueError, "符号链接或 junction"):
            MEMORY.record_event(self.root, "fix-1", self.event())
        day.unlink()
        day.mkdir()
        os.symlink(outside_file, day / "event-1.md")
        with self.assertRaisesRegex(ValueError, "符号链接或 junction"):
            MEMORY.record_event(self.root, "fix-1", self.event())
        self.assertEqual(outside_file.read_bytes(), b"external content")

    def test_fields_and_modules_cannot_exceed_confirmed_scope(self):
        self.scope["record_fields"] = ["changed", "next"]
        self.scope["modules"] = ["前端"]
        self.configure()
        with self.assertRaises(ValueError):
            MEMORY.record_event(self.root, "fix-1", self.event())
        with self.assertRaises(ValueError):
            MEMORY.record_event(self.root, "fix-1", {"actor": "claude", "changed": "改动", "modules": ["后端"]})
        self.assertFalse((self.root / "docs/memory/journal").exists())
        MEMORY.record_event(self.root, "fix-1", {"actor": "cursor", "changed": "小修已记录", "next": "核验"})

    def test_resume_filters_without_truncation_or_writes(self):
        self.configure()
        for index in range(3):
            MEMORY.record_event(self.root, "fix-1", self.event(f"e-{index}", modules=["前端"],
                                time=f"2026-10-15T00:00:0{index}+00:00", changed="完整正文" * 3000))
        MEMORY.record_event(self.root, "fix-1", self.event("backend", modules=["后端"], time="2026-10-15T00:00:04Z"))
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = MEMORY.resume_memory(self.root, "fix-1", "前端", 1, max_bytes=65536, max_output_bytes=131072)
        self.assertEqual(len(report["records"]), 1)
        self.assertEqual(report["records"][0]["metadata"]["event_id"], "e-2")
        self.assertEqual(report["omitted_count"], 2)
        self.assertTrue(report["has_more"])
        self.assertFalse(report["content_truncated"])
        self.assertIn("完整正文" * 3000, report["records"][0]["markdown"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_resume_does_not_decode_old_or_unrelated_bodies(self):
        self.configure()
        old = MEMORY.record_event(self.root, "fix-1", self.event("old", time="2026-10-14T00:00:00Z"))
        old_path = self.root / old["path"]
        old_path.write_bytes(old_path.read_bytes() + b"\xff")
        self.configure("other-task")
        unrelated = MEMORY.record_event(self.root, "other-task", self.event("unrelated", time="2026-10-15T00:00:03Z"))
        unrelated_path = self.root / unrelated["path"]
        unrelated_path.write_bytes(unrelated_path.read_bytes() + b"\xff")
        MEMORY.record_event(self.root, "fix-1", self.event("recent-1", time="2026-10-15T00:00:01Z"))
        MEMORY.record_event(self.root, "fix-1", self.event("recent-2", time="2026-10-15T00:00:02Z"))
        report = MEMORY.resume_memory(self.root, "fix-1", limit=1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["records"][0]["metadata"]["event_id"], "recent-2")
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["omitted_count"], 2)
        self.assertFalse(report["history_audited"])
        self.assertEqual(len(report["scopes"]), 1)
        self.assertFalse(MEMORY.audit_store(self.root)["checks_passed"])

    def test_resume_byte_budget_only_truncates_response_not_disk(self):
        self.configure()
        result = MEMORY.record_event(self.root, "fix-1", self.event(changed="完整中文正文" * 1000))
        path = self.root / result["path"]
        original = path.read_bytes()
        report = MEMORY.resume_memory(self.root, "fix-1", max_bytes=101)
        self.assertTrue(report["content_truncated"])
        record = report["records"][0]
        self.assertTrue(record["content_truncated"])
        self.assertLessEqual(record["returned_body_bytes"], 101)
        self.assertGreater(record["full_body_bytes"], 101)
        self.assertNotIn("\ufffd", record["markdown"])
        self.assertEqual(path.read_bytes(), original)

    def test_resume_shares_budget_and_prioritizes_handoff_sections(self):
        self.configure()
        paths = []
        for index in range(3):
            result = MEMORY.record_event(self.root, "fix-1", {
                "event_id": f"long-{index}", "actor": "claude",
                "time": f"2026-10-15T00:00:0{index}Z",
                "changed": f"开头{index} " + "中间内容" * 2000,
                "next": f"尾部{index} 应继续核验实际文件",
            })
            paths.append(self.root / result["path"])
        before = {path: path.read_bytes() for path in paths}
        report = MEMORY.resume_memory(self.root, max_bytes=900)
        self.assertEqual(len(report["records"]), 3)
        self.assertLessEqual(sum(record["returned_body_bytes"] for record in report["records"]), 900)
        self.assertEqual(set(report["optional_reads"]), {path.relative_to(self.root).as_posix() for path in paths})
        self.assertEqual(report["required_reads"], [])
        for record in report["records"]:
            index = record["metadata"]["event_id"].split("-")[-1]
            self.assertIn(f"开头{index}", record["markdown"])
            self.assertIn(f"尾部{index}", record["markdown"])
            self.assertIn("详细证据按需读取原文", record["markdown"])
            self.assertEqual(record["preview_format"], "handoff_sections")
            self.assertFalse(record["read_required"])
            self.assertTrue(record["body_checksum_verified"])
            self.assertNotIn("\ufffd", record["markdown"])
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_resume_tiny_budget_requires_originals_without_broken_utf8(self):
        self.configure()
        for index in range(2):
            MEMORY.record_event(self.root, "fix-1", self.event(f"tiny-{index}"))
        report = MEMORY.resume_memory(self.root, max_bytes=1)
        self.assertEqual(len(report["records"]), 2)
        self.assertEqual(len(report["required_reads"]), 2)
        for record in report["records"]:
            self.assertEqual(record["markdown"], "")
            self.assertEqual(record["returned_body_bytes"], 0)
            self.assertEqual(record["preview_format"], "omitted")
            self.assertTrue(record["read_required"])

    def test_complete_preview_needs_no_additional_read_at_exact_budget(self):
        self.configure()
        result = MEMORY.record_event(self.root, "fix-1", self.event())
        body = (self.root / result["path"]).read_bytes().split(b"\n", 1)[1]
        report = MEMORY.resume_memory(self.root, max_bytes=len(body))
        self.assertEqual(report["required_reads"], [])
        self.assertFalse(report["content_truncated"])
        self.assertEqual(report["records"][0]["markdown"].encode("utf-8"), body)
        self.assertEqual(report["records"][0]["omitted_body_bytes"], 0)

    def test_scope_fallback_filters_before_limit_and_reports_omissions(self):
        for index, modules in enumerate((["前端"], ["前端"], ["后端"])):
            self.scope["modules"] = modules
            result = self.configure(f"scope-{index}")
            os.utime(self.root / result["path"], (1000 + index, 1000 + index))
        report = MEMORY.resume_memory(self.root, module="前端", limit=1)
        self.assertEqual([scope["task_id"] for scope in report["scopes"]], ["scope-1"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["scope_omitted_count"], 1)
        self.assertEqual(report["scope_omitted_at_least"], 1)
        self.assertTrue(report["scope_scan_complete"])
        self.assertTrue(report["has_more"])
        self.assertEqual(report["omitted_count"], 0)
        missing = MEMORY.resume_memory(self.root, module="不存在", limit=1)
        self.assertEqual(missing["scopes"], [])
        self.assertEqual(missing["scope_omitted_count"], 0)
        self.assertFalse(missing["has_more"])

    def test_scope_fallback_reports_uncertainty_and_keeps_readable_matches(self):
        self.configure("good")
        corrupt = self.configure("corrupt")
        (self.root / corrupt["path"]).write_bytes(b"broken JSON")
        report = MEMORY.resume_memory(self.root, module="前端", limit=1)
        self.assertEqual([scope["task_id"] for scope in report["scopes"]], ["good"])
        self.assertFalse(report["scope_scan_complete"])
        self.assertIsNone(report["scope_omitted_count"])
        self.assertTrue(report["has_more"])
        self.assertTrue(report["errors"])

    def test_cross_project_scope_and_journal_are_rejected(self):
        self.configure()
        record = MEMORY.record_event(self.root, "fix-1", self.event())
        other = self.root.parent / "另一个项目"
        other.mkdir()
        MEMORY.configure_scope(other, "fix-1", self.scope, True)
        source = self.root / record["path"]
        imported = other / record["path"]
        imported.parent.mkdir(parents=True)
        imported.write_bytes(source.read_bytes())
        report = MEMORY.resume_memory(other)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("另一项目" in error["message"] for error in report["errors"]))
        (other / ".ai-memory/scopes/fix-1.json").write_bytes((self.root / ".ai-memory/scopes/fix-1.json").read_bytes())
        with self.assertRaises(ValueError):
            MEMORY.record_event(other, "fix-1", self.event("other"))

    def test_corrupt_or_orphan_log_is_reported_and_never_rewritten(self):
        self.configure()
        result = MEMORY.record_event(self.root, "fix-1", self.event())
        path = self.root / result["path"]
        original = path.read_bytes()
        corrupted = original + "\n手工修改".encode("utf-8")
        path.write_bytes(corrupted)
        report = MEMORY.audit_store(self.root)
        self.assertFalse(report["checks_passed"])
        self.assertEqual(path.read_bytes(), corrupted)
        path.write_bytes(original)
        scope_path = self.root / ".ai-memory/scopes/fix-1.json"
        scope_path.rename(scope_path.with_suffix(".saved"))
        report = MEMORY.audit_store(self.root)
        self.assertTrue(any("孤立日志" in item["message"] for item in report["errors"]))
        self.assertEqual(path.read_bytes(), original)

    def test_publish_failure_leaves_recoverable_temp_without_partial_event(self):
        self.configure()
        with patch.object(MEMORY.os, "link", side_effect=OSError("模拟发布前故障")):
            with self.assertRaises(OSError):
                MEMORY.record_event(self.root, "fix-1", self.event())
        self.assertEqual(list((self.root / "docs/memory/journal").glob("*/*.md")), [])
        report = MEMORY.audit_store(self.root)
        self.assertTrue(any(item["code"] == "incomplete_write" for item in report["warnings"]))
        MEMORY.record_event(self.root, "fix-1", self.event())
        self.assertEqual(MEMORY.audit_store(self.root)["record_count"], 1)

    def test_scope_replace_failure_keeps_current_and_backup(self):
        initial = self.configure()
        path = self.root / initial["path"]
        original = path.read_bytes()
        self.scope["goal"] = "更新目标"
        with patch.object(MEMORY.os, "replace", side_effect=OSError("模拟替换前故障")):
            with self.assertRaises(OSError):
                self.configure(expected_revision=1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((self.root / ".ai-memory/scopes/history/fix-1/revision-1.json").read_bytes(), original)
        self.configure(expected_revision=1)

    def test_corrupt_history_and_project_temporary_files_are_audited(self):
        self.configure()
        self.scope["goal"] = "经过再次确认的新目标"
        self.configure(expected_revision=1)
        historical = self.root / ".ai-memory/scopes/history/fix-1/revision-1.json"
        historical.write_bytes(b"\xff")
        temporary = self.root / ".ai-memory/.tmp-failed-project-write"
        temporary.write_bytes(b"recover me")
        report = MEMORY.audit_store(self.root)
        self.assertTrue(any(item["path"].endswith("revision-1.json") for item in report["errors"]))
        self.assertTrue(any(item["path"].endswith(temporary.name) for item in report["warnings"]))
        self.assertEqual(historical.read_bytes(), b"\xff")
        self.assertEqual(temporary.read_bytes(), b"recover me")

    def test_one_malformed_day_does_not_hide_other_readable_records(self):
        self.configure()
        MEMORY.record_event(self.root, "fix-1", self.event())
        (self.root / "docs/memory/journal/invalid-date").mkdir()
        report = MEMORY.resume_memory(self.root)
        self.assertEqual(len(report["records"]), 1)
        self.assertTrue(report["errors"])

    def test_concurrent_process_records_do_not_overwrite(self):
        self.configure()
        inputs = [self.request_file(f"event-{index}.json", self.event(f"parallel-{index}")) for index in range(6)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda path: self.cli("record", "--root", self.root, "--task", "fix-1", "--file", path), inputs))
        for code, report in results:
            self.assertEqual(code, 0, report)
        audit = MEMORY.audit_store(self.root)
        self.assertEqual(audit["record_count"], 6)
        self.assertTrue(audit["checks_passed"], audit)

    def test_concurrent_scope_updates_have_one_cas_winner(self):
        self.configure()
        inputs = [self.request_file(f"scope-{index}.json", {**self.scope, "goal": f"候选目标 {index}"}) for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda path: self.cli("scope", "--root", self.root, "--task", "fix-1", "--file", path,
                                                              "--confirmed", "--expected-revision", "1"), inputs))
        self.assertEqual(sorted(code for code, _ in results), [0, 2])
        self.assertEqual(MEMORY.resume_memory(self.root)["scopes"][0]["revision"], 2)

    def test_concurrent_same_event_id_on_different_dates_has_one_winner(self):
        self.configure()
        inputs = [self.request_file(f"duplicate-{day}.json", self.event(time=f"2026-10-{day}T00:00:00Z"))
                  for day in (14, 15)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda path: self.cli("record", "--root", self.root, "--task", "fix-1", "--file", path), inputs))
        self.assertEqual(sorted(code for code, _ in results), [0, 2])
        self.assertEqual(MEMORY.audit_store(self.root)["record_count"], 1)

    def test_path_escape_and_symlink_are_rejected(self):
        for identifier in ("../bad", "CON", "a/b"):
            with self.assertRaises(ValueError):
                MEMORY.configure_scope(self.root, identifier, self.scope, True)
        outside = self.root.parent / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.root / ".ai-memory", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(list(outside.iterdir()), [])

    def test_audit_budget_warns_without_truncating_documents(self):
        (self.root / "AGENTS.md").write_text("规则" * 2000, encoding="utf-8")
        (self.root / "DEV.md").write_text("保留历史\n" * 101, encoding="utf-8")
        before = {p: p.read_bytes() for p in self.root.iterdir()}
        report = MEMORY.audit_store(self.root)
        self.assertEqual(len(report["warnings"]), 2)
        self.assertFalse(report["registered"])
        self.assertEqual(report["business_validation"], "not_performed")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.iterdir()})

    def test_audit_agents_line_budget_is_independent_of_byte_budget(self):
        path = self.root / "AGENTS.md"
        original = ("短规则\n" * 101).encode("utf-8")
        path.write_bytes(original)
        report = MEMORY.audit_store(self.root)
        self.assertLess(len(original), 8192)
        self.assertEqual([(item["path"], item["unit"], item["size"], item["limit"])
                          for item in report["warnings"]], [("AGENTS.md", "lines", 101, 100)])
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
