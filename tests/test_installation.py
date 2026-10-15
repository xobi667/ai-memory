"""Exercise a directory installation containing only the skill's runtime files."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "SKILL.md",
    "LICENSE",
    "NOTICE.md",
    "agents/openai.yaml",
    "references/adoption.md",
    "references/hooks.md",
    "references/interview.md",
    "references/lifecycle.md",
    "references/maintenance.md",
    "references/documents.md",
    "references/workflow.md",
    "references/health.md",
    "scripts/ai_memory.py",
    "scripts/memory_admin.py",
    "scripts/memory_hook.py",
    "scripts/memory_store.py",
    "scripts/memory_common.py",
    "scripts/memory_archive.py",
    "scripts/memory_documents.py",
    "scripts/memory_workflow.py",
    "scripts/memory_health.py",
    "scripts/memory_status.py",
)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.base = Path(self.workspace.name)
        self.installed = self.base / "skills" / "ai-memory"
        for relative in RUNTIME_FILES:
            target = self.installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        self.entry = self.installed / "scripts" / "ai_memory.py"

    def test_installed_skill_initializes_project_without_repository(self):
        project = self.base / "project"
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(self.entry), "init", "--root", str(project),
             "--name", "Installation smoke test", "--kind", "generic", "--brief", "Verify installation"],
            cwd=self.base, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((project / "AGENTS.md").is_file())
        for command in ("check", "inspect", "audit"):
            checked = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(self.entry), command, "--root", str(project)],
                cwd=self.base, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_installed_skill_exercises_dispatchers_and_roundtrip_organization(self):
        root = self.base / "中文项目"

        def cli(command, *arguments):
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(self.entry), command,
                                     "--root", str(root), *arguments], cwd=self.base,
                                    capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        def request(name, value):
            path = self.base / name
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            return str(path)

        cli("init", "--name", "安装验收", "--brief", "独立目录中的模块可运行")
        scope = request("scope.json", {"goal": "安装后交接", "detail": "small", "modules": ["docs"],
                                      "confirmation_summary": "已确认此测试任务"})
        cli("scope", "--task", "one", "--file", scope, "--confirmed")
        cli("bind", "--task", "one", "--client", "codex", "--session", "local-test")
        event = request("event.json", {"event_id": "checkpoint", "actor": "installation-test", "changed": "安装验收",
                            "verification": {"status": "not_run", "summary": "业务未验证"}, "next": "确认整理结果"})
        cli("record", "--task", "one", "--file", event)
        self.assertEqual(cli("resume", "--client", "codex", "--session", "local-test")["records"][0]["metadata"]["event_id"], "checkpoint")
        cli("handoff", "--task", "one")
        before_status = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        status = cli("status", "--client", "codex", "--session", "local-test", "--json")
        self.assertEqual(status["task"]["task_id"], "one")
        self.assertEqual(status["business_validation"], "not_performed")
        self.assertEqual(before_status, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()})
        (root / "散落.md").write_text("# 设计\n\n保留实际设计原因。\n", encoding="utf-8")
        original = (root / "散落.md").read_bytes()
        manifest = request("organize.json", {"schema_version": 1, "documents": [{"source": "散落.md",
            "purpose": "设计原因", "role": "design", "state": "current", "target": "docs/设计/设计原因.md",
            "reason": "已阅读，按现有docs目录归类", "uncertainty": None}]})
        cli("inspect", "--documents")
        cli("propose-organize", "--id", "organize", "--file", manifest, "--reason", "整理用途")
        cli("apply-review", "--id", "organize", "--confirmed", "--confirmation", "批准此测试提案")
        self.assertEqual((root / "docs/设计/设计原因.md").read_bytes(), original)
        cli("check")
        cli("rollback-review", "--id", "organize", "--confirmed", "--confirmation", "回滚此测试批次")
        self.assertEqual((root / "散落.md").read_bytes(), original)
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        review = request("review.json", {"schema_version": 1, "review_id": "verified-doc",
            "summary": "核对设计与实现", "task_id": "one",
            "documents": [{"path": "散落.md", "dependencies": ["app.py"]}]})
        cli("review-docs", "--file", review)
        self.assertEqual(cli("health", "--task", "one")["total_counts"]["changes"], 0)
        (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertTrue(cli("health", "--task", "one")["needs_attention"])
        self.assertIn("document_freshness", cli("handoff", "--task", "one")["gaps"])
        cli("propose", "--id", "archive", "--kind", "journal-archive", "--task", "one", "--reason", "追溯测试")
        cli("apply-review", "--id", "archive", "--confirmed", "--confirmation", "批准所列事件")
        self.assertEqual(cli("resume", "--task", "one")["records"], [])
        self.assertEqual(len(cli("resume", "--task", "one", "--include-archived")["records"]), 1)


if __name__ == "__main__":
    unittest.main()
