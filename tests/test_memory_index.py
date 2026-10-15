"""Fresh recovery indexing, unchanged trust boundaries and real file races."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_store.py"
SPEC = importlib.util.spec_from_file_location("memory_index_store_under_test", SCRIPT)
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


class RecoveryIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory-index-")
        self.addCleanup(temporary.cleanup)
        # Match the canonical paths used by the public API, including macOS
        # /var aliases and Windows temporary-directory short names. Race hooks
        # must intercept the actual indexed path on every platform.
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "project"
        self.root.mkdir()
        MEMORY.configure_scope(self.root, "task", {
            "goal": "Recover actual source records", "detail": "small", "modules": ["module"],
            "confirmation_summary": "This is a synthetic test fixture."}, True)

    def record(self, identifier="event", **changes):
        result = MEMORY.record_event(self.root, "task", {
            "event_id": identifier, "actor": "test", "time": "2026-01-01T12:00:00Z",
            "changed": "Actual source", "verification": "Executed", "next": "Continue", **changes})
        return self.root / result["path"]

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def rewrite(self, path, **changes):
        header, body = path.read_bytes().split(b"\n", 1)
        metadata = json.loads(header.decode()[len(MEMORY.PREFIX):-4])
        metadata.update(changes)
        path.write_bytes((MEMORY.PREFIX + json.dumps(metadata, sort_keys=True) + " -->\n").encode() + body)

    def test_recovery_is_read_only_and_ignores_untrusted_persistent_index(self):
        self.record()
        cache = self.root / ".ai-memory/cache"
        cache.mkdir()
        (cache / "recovery-index.json").write_text(
            '{"events": [], "paths": ["../../outside.md"], "checks_passed": true}', encoding="utf-8")
        before = self.snapshot()
        for _ in range(2):
            report = MEMORY.resume_memory(self.root)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["record_count"], 1)
            self.assertEqual(report["records"][0]["metadata"]["event_id"], "event")
        self.assertEqual(self.snapshot(), before)
        (cache / "recovery-index.json").write_bytes(b"\xffbroken")
        self.assertEqual(MEMORY.resume_memory(self.root)["record_count"], 1)

    def test_recovery_detects_edits_even_with_unchanged_size_and_mtime(self):
        path = self.record()
        MEMORY.resume_memory(self.root)
        before = path.stat()
        raw = path.read_bytes()
        self.assertIn(b'"actor": "test"', raw)
        path.write_bytes(raw.replace(b'"actor": "test"', b'"actor": "edit"', 1))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["records"][0]["metadata"]["actor"], "edit")
        path.write_bytes(path.read_bytes().replace(b"Actual source", b"Wrong! source"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("校验值不匹配" in item["message"] for item in report["errors"]))

    def test_new_deleted_and_renamed_records_are_observed_on_each_call(self):
        first = self.record("first")
        self.assertEqual(MEMORY.resume_memory(self.root)["record_count"], 1)
        second = self.record("second", time="2026-01-02T12:00:00Z")
        self.assertEqual(MEMORY.resume_memory(self.root)["record_count"], 2)
        first.unlink()
        report = MEMORY.resume_memory(self.root)
        self.assertEqual([item["metadata"]["event_id"] for item in report["records"]], ["second"])
        renamed = second.with_name("renamed.md")
        second.rename(renamed)
        report = MEMORY.resume_memory(self.root)
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["record_count"], 0)
        self.assertTrue(any("文件名和事件 ID" in item["message"] for item in report["errors"]))

    def test_copied_project_uses_its_own_headers_and_rejects_foreign_identity(self):
        path = self.record()
        MEMORY.resume_memory(self.root)
        clone = self.directory / "clone"
        shutil.copytree(self.root, clone)
        clone_path = clone / path.relative_to(self.root)
        self.rewrite(clone_path, actor="clone")
        report = MEMORY.resume_memory(clone)
        self.assertEqual(report["records"][0]["metadata"]["actor"], "clone")
        self.assertEqual(MEMORY.resume_memory(self.root)["records"][0]["metadata"]["actor"], "test")
        self.rewrite(clone_path, project_id="00000000-0000-0000-0000-000000000000")
        report = MEMORY.resume_memory(clone)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("另一项目" in item["message"] for item in report["errors"]))

    def test_invalid_unselected_header_is_still_reported(self):
        self.record("latest", time="2026-01-02T12:00:00Z")
        old = self.record("old")
        self.rewrite(old, schema_version=True)
        report = MEMORY.resume_memory(self.root, limit=1)
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["records"][0]["metadata"]["event_id"], "latest")
        self.assertTrue(any(item["path"] == old.relative_to(self.root).as_posix() for item in report["errors"]))

    def test_new_file_during_scan_marks_result_incomplete_and_next_call_includes_it(self):
        path = self.record()
        original = MEMORY._index_header
        inserted = False

        def insert_then_read(*args):
            nonlocal inserted
            if not inserted:
                inserted = True
                self.record("inserted")
            return original(*args)

        with patch.object(MEMORY, "_index_header", side_effect=insert_then_read):
            report = MEMORY.resume_memory(self.root)
        self.assertFalse(report["scan_complete"])
        self.assertTrue(any(item.get("code") == "concurrent_journal_change" for item in report["errors"]))
        self.assertEqual(MEMORY.resume_memory(self.root)["record_count"], 2)
        self.assertTrue(path.exists())

    def test_new_date_during_scan_marks_result_incomplete(self):
        self.record()
        original = MEMORY._index_header
        inserted = False

        def insert_then_read(*args):
            nonlocal inserted
            if not inserted:
                inserted = True
                self.record("inserted", time="2026-01-02T12:00:00Z")
            return original(*args)

        with patch.object(MEMORY, "_index_header", side_effect=insert_then_read):
            report = MEMORY.resume_memory(self.root)
        self.assertFalse(report["scan_complete"])
        self.assertTrue(any(item["path"] == MEMORY.JOURNAL and item.get("code") == "concurrent_journal_change"
                            for item in report["errors"]))

    def test_directory_replacement_during_scan_is_rejected(self):
        path = self.record()
        original = MEMORY._index_header
        replaced = False

        def replace_then_read(*args):
            nonlocal replaced
            if not replaced:
                replaced = True
                moved = self.directory / "original-day"
                path.parent.rename(moved)
                shutil.copytree(moved, path.parent)
            return original(*args)

        with patch.object(MEMORY, "_index_header", side_effect=replace_then_read):
            report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["records"], [])
        self.assertFalse(report["scan_complete"])
        self.assertTrue(any("日志目录在扫描期间改变" in item["message"] for item in report["errors"]))

    def test_file_replacement_between_check_and_open_is_rejected_before_read(self):
        path = self.record()
        original_open = Path.open
        replaced = False

        def replace_then_open(target, *args, **kwargs):
            nonlocal replaced
            if target == path and args == ("rb",) and not replaced:
                replaced = True
                old = target.with_suffix(".old")
                target.rename(old)
                with original_open(old, "rb") as source:
                    data = source.read()
                with original_open(target, "wb") as destination:
                    destination.write(data)
            return original_open(target, *args, **kwargs)

        with patch.object(Path, "open", new=replace_then_open):
            report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("打开前被替换或修改" in item["message"] for item in report["errors"]))

    def test_parent_link_swap_before_file_stat_never_enters_header_index(self):
        path = self.record()
        outside = self.directory / "outside-day"
        shutil.copytree(path.parent, outside)
        probe = self.directory / "link-probe"
        try:
            probe.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symbolic links unavailable: {exc}")
        probe.unlink()
        original_lstat = Path.lstat
        replaced = False

        def replace_then_stat(target, *args, **kwargs):
            nonlocal replaced
            if target == path and not replaced:
                replaced = True
                path.parent.rename(self.directory / "original-day")
                path.parent.symlink_to(outside, target_is_directory=True)
            return original_lstat(target, *args, **kwargs)

        with patch.object(Path, "lstat", new=replace_then_stat):
            report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["scanned_header_count"], 0)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("日志目录在扫描期间改变" in item["message"] for item in report["errors"]))

    def test_unexpected_files_invalid_dates_and_duplicates_keep_diagnostics(self):
        path = self.record()
        (path.parent / ".tmp-write").write_bytes(b"unfinished")
        (path.parent / "extra.txt").write_bytes(b"unrelated")
        invalid_day = path.parent.parent / "2026-02-30"
        invalid_day.mkdir()
        duplicate = path.parent.parent / "2026-01-02" / path.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(path.read_bytes())
        self.rewrite(duplicate, time="2026-01-02T12:00:00Z")
        before = self.snapshot()
        report = MEMORY.resume_memory(self.root)
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["record_count"], 1)
        self.assertEqual({item["code"] for item in report["warnings"]}, {"incomplete_write", "unexpected_file"})
        self.assertTrue(any("重复事件 ID" in item["message"] for item in report["errors"]))
        self.assertTrue(any(item["path"].endswith("2026-02-30") for item in report["errors"]))
        self.assertEqual(self.snapshot(), before)

    def test_links_to_outside_sources_are_rejected(self):
        path = self.record()
        outside = self.directory / "outside.md"
        path.rename(outside)
        try:
            path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symbolic links unavailable: {exc}")
        report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("符号链接" in item["message"] for item in report["errors"]))
        path.unlink()
        path.parent.rmdir()
        outside_day = self.directory / "outside-day"
        outside_day.mkdir()
        shutil.copy2(outside, outside_day / path.name)
        path.parent.symlink_to(outside_day, target_is_directory=True)
        report = MEMORY.resume_memory(self.root)
        self.assertEqual(report["records"], [])
        self.assertTrue(any("符号链接" in item["message"] for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
