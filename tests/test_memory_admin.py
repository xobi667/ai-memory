from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_admin as admin


PROJECT_ID = "70a6c80a-79ac-45c9-a0cd-092afc286512"


class MemoryAdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文项目"
        self.root.mkdir()
        self.write(".ai-memory/project.json", json.dumps({"schema_version": 1, "project_id": PROJECT_ID}))
        self.write("docs/设计.md", "# 原始设计\n\n旧规则：允许离线运行。\n")
        self.replacement = Path(self.temporary.name) / "替换全文.md"
        self.replacement.write_text("# 当前设计\n\n保留离线功能；补充状态提示。\n", encoding="utf-8")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def propose(self, review_id="review-1", kind="compact", source="docs/设计.md"):
        return admin.propose(self.root, review_id, kind, source,
                             None if kind == "archive" else str(self.replacement), "保留设计原因并整理入口")

    def apply(self, review_id="review-1"):
        return admin.apply_review(self.root, review_id, True, "用户已审阅提案并确认应用（调用者声明）")

    def test_inspect_is_read_only_and_reports_budgets_and_frameworks(self):
        self.write("AGENTS.md", "规则\n" * 101)
        self.write("DEV.md", "状态\n" * 101)
        self.write("package.json", "{}")
        (self.root / "openspec").mkdir()
        before = self.snapshot()
        result = admin.inspect(self.root)
        self.assertTrue(result["read_only"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["project_id"], PROJECT_ID)
        self.assertTrue(any(x["framework"] == "OpenSpec" for x in result["framework_markers"]))
        self.assertTrue(any(x["path"] == "AGENTS.md" for x in result["warnings"]))
        self.assertTrue(any(x["path"] == "DEV.md" for x in result["warnings"]))

    def test_inspect_unregistered_project_does_not_create_marker(self):
        root = Path(self.temporary.name) / "unregistered"
        root.mkdir()
        result = admin.inspect(root)
        self.assertIsNone(result["project_id"])
        self.assertEqual(list(root.iterdir()), [])

    def test_proposal_does_not_change_source_and_needs_registration(self):
        original = (self.root / "docs/设计.md").read_bytes()
        result = self.propose()
        self.assertFalse(result["source_modified"])
        self.assertEqual((self.root / "docs/设计.md").read_bytes(), original)
        self.assertEqual(Path(result["before"]).read_bytes(), original)
        self.assertIn("旧版仅供追溯", Path(result["after"]).read_text(encoding="utf-8"))
        empty = Path(self.temporary.name) / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            admin.propose(empty, "one", "rule", "docs/memory/rules/new.md", str(self.replacement), "new")
        self.assertEqual(list(empty.iterdir()), [])

    def test_apply_requires_explicit_confirmation(self):
        self.propose()
        before = self.snapshot()
        for flag, text in ((False, "yes"), (True, ""), (True, None)):
            with self.subTest(flag=flag, text=text), self.assertRaises(ValueError):
                admin.apply_review(self.root, "review-1", flag, text)
        self.assertEqual(self.snapshot(), before)

    def test_archive_date_matches_utc_creation_time_across_local_midnight(self):
        cases = (
            ("east", datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc), 14),
            ("west", datetime(2026, 10, 15, 0, 30, tzinfo=timezone.utc), -12),
        )
        for review_id, instant, offset in cases:
            local_time = instant.astimezone(timezone(timedelta(hours=offset)))
            self.assertNotEqual(local_time.date(), instant.date())
            local_clock = Mock()
            local_clock.astimezone.return_value = local_time
            with self.subTest(offset=offset), patch.object(admin, "datetime") as clock:
                clock.now.side_effect = lambda tz=None: instant.astimezone(tz) if tz is not None else local_clock
                self.propose(review_id)
            proposal = json.loads((self.root / f".ai-memory/reviews/{review_id}/proposal.json").read_text(encoding="utf-8"))
            self.assertEqual(proposal["created_at"], instant.isoformat(timespec="seconds"))
            self.assertEqual(proposal["archive"].split("/")[3], instant.date().isoformat())

    def test_source_drift_is_rejected_without_backup_or_source_change(self):
        self.propose()
        self.write("docs/设计.md", "新编辑，提案已过期。")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "docs/memory/archive").exists())

    def test_each_proposal_artifact_drift_is_rejected(self):
        for filename in ("proposal.json", "before.md", "after.md"):
            review_id = filename.replace(".", "-")
            self.propose(review_id)
            path = self.root / ".ai-memory/reviews" / review_id / filename
            if filename == "proposal.json":
                value = json.loads(path.read_text(encoding="utf-8"))
                value["reason"] = "changed"
                path.write_text(json.dumps(value), encoding="utf-8")
            else:
                path.write_bytes(path.read_bytes() + b"changed")
            original = (self.root / "docs/设计.md").read_bytes()
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.apply(review_id)
            self.assertEqual((self.root / "docs/设计.md").read_bytes(), original)

    def test_project_identity_drift_is_rejected(self):
        self.propose()
        self.write(".ai-memory/project.json", '{"schema_version":1,"project_id":"30a5745c-4f71-4401-883f-9f9a7ff8da14"}')
        with self.assertRaisesRegex(ValueError, "项目标识"):
            self.apply()

    def test_invalid_project_identity_is_rejected_without_writes(self):
        invalid = [
            {"project_id": PROJECT_ID},
            {"schema_version": True, "project_id": PROJECT_ID},
            {"schema_version": 1.0, "project_id": PROJECT_ID},
            {"schema_version": 2, "project_id": PROJECT_ID},
            {"schema_version": "1", "project_id": PROJECT_ID},
            {"schema_version": 1},
            {"schema_version": 1, "project_id": "test-project"},
            {"schema_version": 1, "project_id": ""},
            {"schema_version": 1, "project_id": None},
            {"schema_version": 1, "project_id": 123},
            {"schema_version": 1, "project_id": []},
        ]
        for marker in invalid:
            self.write(".ai-memory/project.json", json.dumps(marker))
            before = self.snapshot()
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                self.propose()
            result = admin.inspect(self.root)
            self.assertIsNone(result["project_id"])
            self.assertTrue(any(item["path"] == ".ai-memory" for item in result["warnings"]))
            self.assertEqual(before, self.snapshot())

    def test_invalid_identity_after_proposal_blocks_apply(self):
        self.propose()
        self.write(".ai-memory/project.json", '{"schema_version":1,"project_id":"invalid"}')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.apply()
        self.assertEqual(before, self.snapshot())

    def test_apply_preserves_exact_backup_and_has_caller_audit(self):
        original = b"\xef\xbb\xbf# old\r\nsecond line\r\n"
        (self.root / "docs/设计.md").write_bytes(original)
        self.propose()
        result = self.apply()
        self.assertEqual((self.root / result["backup"]).read_bytes(), original)
        self.assertIn("历史原文", (self.root / "docs/设计.md").read_text(encoding="utf-8"))
        self.assertIn("%E8%AE%BE%E8%AE%A1.md", (self.root / "docs/设计.md").read_text(encoding="utf-8"))
        receipt = json.loads(Path(result["audit"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["confirmation_type"], "caller_declared")
        self.assertIn("不能验证真人授权", receipt["notice"])
        self.assertFalse((self.root / ".ai-memory/admin.lock").exists())

    def test_archive_creates_stub_and_recoverable_original(self):
        original = (self.root / "docs/设计.md").read_bytes()
        self.propose(kind="archive")
        result = self.apply()
        self.assertEqual((self.root / result["backup"]).read_bytes(), original)
        self.assertIn("查看历史原文", (self.root / "docs/设计.md").read_text(encoding="utf-8"))

    def test_cannot_archive_core_entry(self):
        for name in ("AGENTS.md", "DEV.md", "README.md", "ARCH.md", "ARCHITECTURE.md", "docs/INDEX.md"):
            self.write(name, "core")
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.propose(kind="archive", source=name)

    def test_managed_journal_rejects_all_review_kinds_without_writes(self):
        journal = "docs/memory/journal/2026-10-15/event-1.md"
        self.write(journal, '<!-- immutable event header with payload hash -->\n原始执行记录\n')
        before = self.snapshot()
        for kind in ("rule", "compact", "archive"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "更正请追加后续记录"):
                self.propose(f"journal-{kind}", kind, journal)
            self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / ".ai-memory/reviews").exists())
        self.assertFalse((self.root / ".ai-memory/review-seals").exists())

    def test_legacy_journal_proposal_cannot_be_applied(self):
        journal = "docs/memory/journal/2026-10-15/event-1.md"
        path = self.write(journal, "原始不可变日志\n")
        # 模拟旧版本曾允许生成的合法、已封存提案。
        with patch.object(admin, "source_path", return_value=(journal, path)):
            self.propose("legacy-journal", "compact", journal)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "不支持迁移日志"):
            self.apply("legacy-journal")
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "docs/memory/archive").exists())

    def test_new_rules_only_and_no_existing_target_overwrite(self):
        self.propose(kind="rule", source="docs/memory/rules/离线.md")
        self.write("docs/memory/rules/离线.md", "other actor wrote this")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual((self.root / "docs/memory/rules/离线.md").read_text(encoding="utf-8"), "other actor wrote this")
        self.propose("new-rule", "rule", "docs/memory/rules/another.md")
        result = self.apply("new-rule")
        self.assertIsNone(result["backup"])
        self.assertEqual((self.root / "docs/memory/rules/another.md").read_bytes(), self.replacement.read_bytes())
        with self.assertRaises(ValueError):
            self.propose("bad-new", "rule", "docs/elsewhere.md")

    def test_outside_and_ambiguous_paths_rejected(self):
        for source in ("../escape.md", "docs/../../escape.md", "C:/escape.md", "/escape.md", "docs/con.md", "docs/file.md:stream", "docs//file.md"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.propose(source=source)

    def test_symlink_or_junction_source_and_destinations_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "linked.md").write_text("outside original", encoding="utf-8")
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlink unavailable")
            # Windows junction creation does not require Developer Mode.
            command = f'New-Item -ItemType Junction -Path "{link}" -Target "{outside}" | Out-Null'
            result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True)
            if result.returncode:
                self.skipTest("junction unavailable")
        with self.assertRaises(ValueError):
            self.propose(source="linked/linked.md")
        self.propose()
        archive_dir = self.root / "docs/memory/archive"
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            archive_dir.symlink_to(outside, target_is_directory=True)
        except OSError:
            command = f'New-Item -ItemType Junction -Path "{archive_dir}" -Target "{outside}" | Out-Null'
            subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(list(p.name for p in outside.iterdir()), ["linked.md"])

    def test_agents_final_budget_refused_before_any_backup(self):
        self.write("AGENTS.md", "# rules\n")
        for review_id, content in (("bytes", "规" * 3000), ("lines", "rule\n" * 100)):
            self.replacement.write_text(content, encoding="utf-8")
            self.propose(review_id, "rule", "AGENTS.md")
            before = self.snapshot()
            with self.subTest(review_id=review_id), self.assertRaisesRegex(ValueError, "预算"):
                self.apply(review_id)
            self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "docs/memory/archive").exists())

    def test_dev_final_budget_refused_before_backup_and_allows_100_lines(self):
        self.write("DEV.md", "# 当前任务\n")
        self.replacement.write_text("state\n" * 100, encoding="utf-8")
        self.propose("dev-too-long", "compact", "DEV.md")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "DEV 超出 100 行预算"):
            self.apply("dev-too-long")
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "docs/memory/archive").exists())
        # 历史链接也计入预算：97 行替换稿加历史入口后恰好 100 行。
        self.replacement.write_text("state\n" * 97, encoding="utf-8")
        self.propose("dev-within-budget", "compact", "DEV.md")
        self.apply("dev-within-budget")
        self.assertEqual(len((self.root / "DEV.md").read_text(encoding="utf-8").splitlines()), 100)

    def test_repeated_apply_is_idempotent(self):
        self.propose()
        self.apply()
        before = self.snapshot()
        result = self.apply()
        self.assertTrue(result["already_applied"])
        self.assertEqual(before, self.snapshot())

    def test_existing_lock_is_not_broken_and_inspect_diagnoses(self):
        self.write(".ai-memory/admin.lock", '{"pid":0,"created_at":"old"}')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "锁"):
            self.propose()
        self.assertEqual(before, self.snapshot())
        self.assertTrue(any(x["path"] == ".ai-memory/admin.lock" for x in admin.inspect(self.root)["warnings"]))

    def test_preflight_rejects_bad_audit_path_before_source_or_backup(self):
        self.propose()
        (self.root / ".ai-memory/reviews/review-1/audit.md").mkdir()
        before = (self.root / "docs/设计.md").read_bytes()
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual((self.root / "docs/设计.md").read_bytes(), before)
        self.assertFalse((self.root / "docs/memory/archive").exists())

    def test_interrupted_audit_can_resume_without_replacing_source_again(self):
        self.propose()
        original_create = admin.create_exact

        def fail_audit(root, path, data):
            if path.name == "audit.md":
                raise OSError("simulated disk interruption after source replacement")
            return original_create(root, path, data)

        with patch.object(admin, "create_exact", side_effect=fail_audit), self.assertRaises(OSError):
            self.apply()
        after = (self.root / "docs/设计.md").read_bytes()
        with patch.object(admin.os, "replace", side_effect=AssertionError("should not replace twice")):
            result = self.apply()
        self.assertFalse(result["already_applied"])
        self.assertEqual((self.root / "docs/设计.md").read_bytes(), after)
        self.assertTrue(Path(result["audit"]).is_file())

    def test_cli_returns_structured_error(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = admin.main(["apply-review", "--root", str(self.root), "--id", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
