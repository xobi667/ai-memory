from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_archive as archive
import memory_store as store
import memory_admin as admin


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store.configure_scope(self.root, "task", {"goal": "交接", "detail": "small", "modules": ["docs"],
                              "confirmation_summary": "用户确认本任务记录"}, True)
        self.first = self.add("first")

    def add(self, event):
        return store.record_event(self.root, "task", {"event_id": event, "actor": "test", "changed": "记录",
                    "verification": {"status": "not_run", "summary": "业务未测试"}, "next": "核验实现"})

    def propose(self, identifier="archive", kind="journal-archive"):
        return archive.propose_archive(self.root, identifier, kind, "减少默认恢复历史", task="task")

    def apply(self, identifier="archive"):
        return admin.apply_review(self.root, identifier, True, "已审阅此事件集合并确认")

    def test_snapshot_does_not_archive_future_events_or_change_original_bytes(self):
        path = self.root / self.first["path"]
        original = path.read_bytes()
        self.propose()
        self.add("later")
        self.apply()
        manifest = archive.load_manifest(self.root, admin.identity(self.root))
        self.assertEqual(set(manifest["events"]), {"first"})
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.apply()["already_applied"])
        self.propose("undo", "journal-unarchive")
        self.apply("undo")
        self.assertEqual(archive.load_manifest(self.root, admin.identity(self.root))["events"], {})

    def test_drift_and_missing_confirmation_leave_manifest_absent(self):
        self.propose()
        with self.assertRaises(ValueError):
            archive.apply_archive(self.root, "archive", False, "")
        path = self.root / self.first["path"]
        path.write_bytes(path.read_bytes() + b"drift")
        with self.assertRaisesRegex(ValueError, "原文"):
            self.apply()
        self.assertFalse((self.root / archive.MANIFEST).exists())

    def test_stale_manifest_cannot_clobber_another_review(self):
        self.propose("a")
        self.propose("b")
        self.apply("a")
        before = (self.root / archive.MANIFEST).read_bytes()
        with self.assertRaisesRegex(ValueError, "清单已变化"):
            self.apply("b")
        self.assertEqual((self.root / archive.MANIFEST).read_bytes(), before)

    def test_interruption_after_manifest_write_finishes_receipt_on_retry(self):
        self.propose()
        original = archive.create_exact
        def fail(root, path, data):
            if path.name == "audit.json":
                raise OSError("simulated interruption")
            return original(root, path, data)
        with patch.object(archive, "create_exact", side_effect=fail), self.assertRaises(OSError):
            self.apply()
        before = (self.root / archive.MANIFEST).read_bytes()
        self.apply()
        self.assertEqual((self.root / archive.MANIFEST).read_bytes(), before)
        self.assertTrue((self.root / ".ai-memory/reviews/archive/audit.json").is_file())

    def test_proposal_tampering_and_unknown_events_fail(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            archive.propose_archive(self.root, "missing", "journal-archive", "test", event_ids=["missing"])
        self.propose()
        path = self.root / ".ai-memory/reviews/archive/after.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "快照"):
            self.apply()

    def test_manifest_schema_rejects_boolean_and_float_without_writes(self):
        path = self.root / archive.MANIFEST
        for version in (True, 1.0):
            content = json.dumps({"schema_version": version, "project_id": admin.identity(self.root),
                                  "revision": 1, "events": {}}).encode("utf-8")
            path.write_bytes(content)
            with self.subTest(version=version), self.assertRaises(ValueError):
                archive.load_manifest(self.root, admin.identity(self.root))
            self.assertTrue(store.audit_store(self.root)["errors"])
            self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
