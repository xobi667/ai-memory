"""Separate local Git clones do not share writer locks or scope revision CAS."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_store as store


@unittest.skipUnless(shutil.which("git"), "Git unavailable")
class CrossCloneTests(unittest.TestCase):
    def git(self, root, *arguments, success=True):
        result = subprocess.run(["git", "-c", "user.name=Memory Test", "-c", "user.email=memory@example.invalid",
                                 "-c", "commit.gpgsign=false", "-C", str(root), *arguments],
                                capture_output=True, encoding="utf-8")
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def commit(self, root):
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "fixture checkpoint")

    def test_independent_events_merge_but_parallel_scope_changes_need_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            seed, first, second = (base / name for name in ("seed", "first", "second"))
            seed.mkdir()
            self.git(seed, "init", "-b", "main")
            scope = {"goal": "共同目标", "detail": "small", "modules": ["core"], "confirmation_summary": "用户确认"}
            store.configure_scope(seed, "task", scope, True)
            self.commit(seed)
            self.git(base, "clone", str(seed), str(first))
            self.git(base, "clone", str(seed), str(second))
            for root, event in ((first, "actor-a"), (second, "actor-b")):
                store.record_event(root, "task", {"event_id": event, "actor": event, "changed": "各自保存发现",
                    "verification": {"status": "not_run", "summary": "尚未核验"}, "next": "合并后核验"})
                self.commit(root)
            self.git(second, "fetch", str(first), "main")
            self.git(second, "merge", "FETCH_HEAD", "--no-edit")
            report = store.audit_store(second)
            self.assertEqual(report["record_count"], 2)
            self.assertEqual(report["errors"], [])
            for root, goal in ((first, "克隆A独立修改"), (second, "克隆B独立修改")):
                store.configure_scope(root, "task", {**scope, "goal": goal}, True, expected_revision=1)
                self.commit(root)
            self.git(second, "fetch", str(first), "main")
            merge = self.git(second, "merge", "FETCH_HEAD", "--no-edit", success=False)
            self.assertNotEqual(merge.returncode, 0)
            conflicts = self.git(second, "diff", "--name-only", "--diff-filter=U").stdout
            self.assertIn(".ai-memory/scopes/task.json", conflicts)
            self.assertTrue(store.audit_store(second)["errors"])
            for event in ("actor-a", "actor-b"):
                self.assertEqual(len(list((second / store.JOURNAL).glob(f"*/{event}.md"))), 1)


if __name__ == "__main__":
    unittest.main()
