"""用临时项目检验可移植初始化、幂等、文档检查与路径边界。"""

import importlib.util
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ai_memory.py"
SPEC = importlib.util.spec_from_file_location("ai_memory", SCRIPT)
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="项目记忆测试_")
        self.addCleanup(self.workspace.cleanup)
        self.base = Path(self.workspace.name)
        self.root = self.base / "中文项目"

    def run_cli(self, *arguments, expected=0):
        process = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", check=False,
        )
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def init(self, kind="generic", *extra, expected=0):
        return self.run_cli("init", "--root", str(self.root), "--name", "中文示例", "--kind", kind,
                            "--brief", "供多个 AI 恢复上下文的临时测试项目。", *extra, expected=expected)

    def test_empty_web_initialization_and_read_only_check(self):
        result = self.init("web")
        self.assertEqual(len(result["created"]), 6)
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("运行环境", readme)
        self.assertIn("部署入口", readme)
        self.assertNotIn("版本码", readme)
        self.assertNotIn("签名配置", readme)
        self.assertEqual((self.root / "CLAUDE.md").read_bytes(), b"@AGENTS.md\n")
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = self.run_cli("check", "--root", str(self.root))
        self.assertTrue(report["document_checks_passed"])
        self.assertEqual(report["business_validation"], "not_performed")
        self.assertIn("不代表", report["note"])
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_empty_android_only_records_paths_without_values(self):
        self.init("android", "--tools", "codex", "cursor")
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("版本名 / 版本码的配置来源路径：待确认", readme)
        self.assertIn("签名配置所在路径：待确认", readme)
        self.assertNotIn("部署入口", readme)
        self.assertNotIn("2.9", readme)
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.run_cli("check", "--root", str(self.root))

    def test_data_and_content_do_not_inherit_application_version_rules(self):
        for kind, applicable in (("data", "数据处理工具"), ("content", "编辑、生成和导出工具")):
            with self.subTest(kind=kind):
                self.root = self.base / kind
                self.init(kind)
                readme = (self.root / "README.md").read_text(encoding="utf-8")
                agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
                self.assertIn(applicable, readme)
                self.assertNotIn("当前版本", readme)
                self.assertNotIn("升版", readme + agents)
                self.assertNotIn("版本码", readme + agents)
                self.assertNotIn("构建、测试、运行与发布", (self.root / "DEV.md").read_text(encoding="utf-8"))
                self.run_cli("check", "--root", str(self.root))

    def test_repeat_preserves_every_existing_byte(self):
        self.init("web")
        (self.root / "DEV.md").write_bytes("# 用户已有状态\r\n保留换行与内容。\r\n".encode("utf-8"))
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.init("android")
        self.assertEqual(result["created"], [])
        self.assertEqual(set(result["preserved"]), set(before) - {".ai-memory/store.lock"})
        for relative, content in before.items():
            self.assertEqual((self.root / relative).read_bytes(), content)

    def test_missing_files_only_are_created(self):
        self.root.mkdir()
        original = b"Existing instructions\x00\xff"
        (self.root / "AGENTS.md").write_bytes(original)
        report = self.init()
        self.assertIn("AGENTS.md", report["preserved"])
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original)
        report = self.run_cli("check", "--root", str(self.root), expected=1)
        self.assertTrue(any(item["file"] == "AGENTS.md" for item in report["errors"]))

    def test_task_preserves_entries_and_documents_registration(self):
        self.init()
        dev = (self.root / "DEV.md").read_bytes()
        index = (self.root / "docs" / "INDEX.md").read_bytes()
        report = self.run_cli("task", "--root", str(self.root), "--id", "fix-123", "--goal", "验证中文路径恢复。")
        self.assertEqual(len(report["created"]), 3)
        self.assertEqual(report["registration_required"], [])
        self.assertEqual(report["registered_entries"], ["DEV.md", "docs/INDEX.md"])
        self.assertTrue((self.root / "DEV.md").read_bytes().startswith(dev))
        self.assertTrue((self.root / "docs" / "INDEX.md").read_bytes().startswith(index))
        plan = self.root / "docs" / "tasks" / "fix-123" / "plan.md"
        self.assertIn("验证中文路径恢复。", plan.read_text(encoding="utf-8"))
        self.assertIn("任务当前事实的主要来源", plan.read_text(encoding="utf-8"))
        progress = plan.with_name("progress.md").read_text(encoding="utf-8")
        self.assertIn("交接快照（UTC：", progress)
        self.assertIn("不覆盖当前 plan.md", progress)
        saved = plan.read_bytes()
        entries = [(self.root / name).read_bytes() for name in ("DEV.md", "docs/INDEX.md")]
        self.run_cli("task", "--root", str(self.root), "--id", "fix-123", "--goal", "另一个目标", expected=2)
        report = self.run_cli("task", "--root", str(self.root), "--id", "fix-123", "--goal", "验证中文路径恢复。")
        self.assertEqual(report["created"], [])
        self.assertEqual(plan.read_bytes(), saved)
        self.assertEqual([(self.root / name).read_bytes() for name in ("DEV.md", "docs/INDEX.md")], entries)
        self.run_cli("check", "--root", str(self.root))

    def test_failed_publication_leaves_no_partial_document(self):
        self.root.mkdir()
        destination = self.root / "README.md"
        with patch.object(MEMORY.os, "link", side_effect=OSError("模拟发布失败")):
            with self.assertRaises(OSError):
                MEMORY.atomic_create_text(self.root, destination, "完整内容")
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        MEMORY.atomic_create_text(self.root, destination, "完整内容")
        self.assertEqual(destination.read_text(encoding="utf-8"), "完整内容")

    def test_task_goal_is_rechecked_after_acquiring_shared_lock(self):
        self.init()
        import memory_common
        @contextmanager
        def competing_writer(root):
            MEMORY.create_missing(root, MEMORY.task_documents("race", "另一调用先建立的目标"))
            yield
        output = io.StringIO()
        with patch.object(memory_common, "project_lock", competing_writer), redirect_stdout(output):
            code = MEMORY.main(["task", "--root", str(self.root), "--id", "race", "--goal", "本调用的不同目标"])
        self.assertEqual(code, 2)
        self.assertIn("目标不同", json.loads(output.getvalue())["error"])
        self.assertIn("另一调用先建立的目标", (self.root / "docs/tasks/race/plan.md").read_text(encoding="utf-8"))

    def test_task_registration_preserves_custom_layout_and_broken_markers(self):
        self.root.mkdir()
        (self.root / ".ai-memory").mkdir()
        (self.root / "资料").mkdir()
        (self.root / "资料/进度.md").write_bytes("人工进度\r\n".encode("utf-8"))
        (self.root / "资料/索引.md").write_text("人工索引\n<!-- ai-memory:tasks:start -->\n", encoding="utf-8")
        (self.root / ".ai-memory/layout.json").write_text(json.dumps({"schema_version": 1,
            "document_root": "资料", "index": "资料/索引.md", "roles": {"status": "资料/进度.md"}}), encoding="utf-8")
        result = self.run_cli("task", "--root", str(self.root), "--id", "custom", "--goal", "自定义布局")
        self.assertEqual(result["registration_required"], ["资料/索引.md"])
        self.assertTrue((self.root / "资料/进度.md").read_bytes().startswith("人工进度\r\n".encode("utf-8")))
        self.assertFalse((self.root / "DEV.md").exists())
        self.assertFalse((self.root / "docs/INDEX.md").exists())
        plan = (self.root / "docs/tasks/custom/plan.md").read_text(encoding="utf-8")
        self.assertIn("../../../%E8%B5%84%E6%96%99/%E8%BF%9B%E5%BA%A6.md", plan)
        self.assertNotIn("](../../../DEV.md)", plan)

    def test_direct_creation_accepts_project_root_alias(self):
        self.root.mkdir()
        alias = self.base / "项目根目录别名"
        self.make_symlink(self.root, alias, directory=True)
        self.assertNotEqual(alias, alias.resolve())
        destination = alias / "docs" / "说明.md"
        MEMORY.atomic_create_text(alias, destination, "别名路径中的完整内容")
        self.assertEqual((self.root / "docs" / "说明.md").read_text(encoding="utf-8"), "别名路径中的完整内容")
        result = MEMORY.create_missing(alias, {"README.md": "根目录别名可用于直接 API", "docs/说明.md": "不能覆盖"})
        self.assertEqual(result, {"created": ["README.md"], "preserved": ["docs/说明.md"]})
        self.assertEqual(destination.read_text(encoding="utf-8"), "别名路径中的完整内容")

    def test_project_root_alias_still_rejects_child_links_and_parent_escape(self):
        self.root.mkdir()
        alias = self.base / "项目根目录别名"
        self.make_symlink(self.root, alias, directory=True)
        actual = self.root / "真实子目录"
        actual.mkdir()
        self.make_symlink(actual, self.root / "链接子目录", directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接或 junction"):
            MEMORY.atomic_create_text(alias, alias / "链接子目录" / "禁止写入.md", "不可写")
        with self.assertRaisesRegex(ValueError, "越出项目根目录"):
            MEMORY.atomic_create_text(alias, alias / ".." / "越界.md", "不可写")
        self.assertEqual(list(actual.iterdir()), [])
        self.assertFalse((self.base / "越界.md").exists())

    def test_partial_task_is_not_reported_as_complete_structure(self):
        self.init()
        task = self.root / "docs" / "tasks" / "interrupted"
        task.mkdir(parents=True)
        (task / "findings.md").write_text("已有发现但计划未生成。", encoding="utf-8")
        report = self.run_cli("check", "--root", str(self.root), expected=1)
        self.assertTrue(any("plan.md" in item["file"] for item in report["errors"]))

    def test_malicious_task_ids_are_rejected_without_creation(self):
        for identifier in ("../escape", "..", "/absolute", "a/b", "a\\b", "C:escape", "CON", "nul", "safe.", ""):
            with self.subTest(identifier=identifier):
                self.run_cli("task", "--root", str(self.root), "--id", identifier, "--goal", "测试", expected=2)
                self.assertFalse(self.root.exists())

    def test_task_registration_warnings_clear_with_real_entry_links(self):
        self.init()
        for identifier in ("current-task", "previous-task"):
            MEMORY.create_missing(self.root, MEMORY.task_documents(identifier, "验证尚未登记的现有任务可被入口发现。"))
        report = self.run_cli("check", "--root", str(self.root))
        codes = [item.get("code") for item in report["warnings"]]
        self.assertEqual(codes.count("unregistered_task"), 2)
        self.assertEqual(codes.count("no_active_task_link"), 1)
        self.assertIn("标准新项目布局", report["scope"])
        index = self.root / "docs" / "INDEX.md"
        with index.open("a", encoding="utf-8") as stream:
            stream.write("\n- [当前任务](tasks/current-task/plan.md)\n- [历史任务](tasks/previous-task/plan.md)\n")
        report = self.run_cli("check", "--root", str(self.root))
        self.assertEqual([item.get("code") for item in report["warnings"]], ["no_active_task_link"])
        with (self.root / "DEV.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- [当前任务](docs/tasks/current-task/plan.md)\n")
        report = self.run_cli("check", "--root", str(self.root))
        self.assertEqual(report["warnings"], [])

    def make_symlink(self, destination, link, directory=False):
        try:
            os.symlink(destination, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"平台不允许创建测试符号链接：{exc}")

    def test_docs_symlink_escape_refused_before_any_write(self):
        self.root.mkdir()
        outside = self.base / "外部目录"
        outside.mkdir()
        self.make_symlink(outside, self.root / "docs", directory=True)
        self.init(expected=2)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_target_file_symlink_escape_refused(self):
        self.root.mkdir()
        outside = self.base / "外部.txt"
        outside.write_bytes(b"do not touch")
        self.make_symlink(outside, self.root / "README.md")
        self.init(expected=2)
        self.assertEqual(outside.read_bytes(), b"do not touch")
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_task_directory_symlink_escape_refused(self):
        self.init()
        outside = self.base / "外部任务"
        outside.mkdir()
        self.make_symlink(outside, self.root / "docs" / "tasks", directory=True)
        self.run_cli("task", "--root", str(self.root), "--id", "safe", "--goal", "测试", expected=2)
        self.assertEqual(list(outside.iterdir()), [])
        report = self.run_cli("check", "--root", str(self.root), expected=1)
        self.assertTrue(report["errors"])

    def test_missing_project_check_does_not_create_or_claim_success(self):
        report = self.run_cli("check", "--root", str(self.root), expected=1)
        self.assertFalse(self.root.exists())
        self.assertFalse(report["document_checks_passed"])
        self.assertEqual(report["business_validation"], "not_performed")
        self.assertEqual(len(report["errors"]), len(MEMORY.REQUIRED))

    def test_broken_links_are_errors_but_size_limits_only_warnings(self):
        self.init()
        agents = self.root / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8") + "\n" + "说明" * 2000, encoding="utf-8")
        dev = self.root / "DEV.md"
        dev.write_text("当前状态\n" * 110, encoding="utf-8")
        report = self.run_cli("check", "--root", str(self.root))
        self.assertEqual(len(report["warnings"]), 2)
        dev.write_text("[不存在的证据](docs/missing.md)\n[远程](https://example.invalid)\n", encoding="utf-8")
        report = self.run_cli("check", "--root", str(self.root), expected=1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["link"], "docs/missing.md")

    def test_markdown_links_support_chinese_and_ignore_code(self):
        self.init()
        evidence = self.root / "docs" / "中文 证据.txt"
        evidence.write_text("测试证据", encoding="utf-8")
        (self.root / "DEV.md").write_text(
            "[证据](<docs/中文 证据.txt>)\n[跳转](#当前状态)\n"
            "`[示例](missing.md)`\n```md\n[示例](missing.md)\n```\n"
            "[引用][evidence]\n[evidence]: docs/中文%20证据.txt\n", encoding="utf-8",
        )
        self.run_cli("check", "--root", str(self.root))


if __name__ == "__main__":
    unittest.main()
