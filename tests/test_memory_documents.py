from __future__ import annotations

import json
import os
from pathlib import Path
import posixpath
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_documents as docs


PROJECT_ID = "70a6c80a-79ac-45c9-a0cd-092afc286512"


class DocumentOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文项目"
        self.root.mkdir()
        self.write(".ai-memory/project.json", json.dumps({"schema_version": 1, "project_id": PROJECT_ID}))
        self.manifest = Path(self.temporary.name) / "分类清单.json"

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def read(self, relative):
        return (self.root / relative).read_text(encoding="utf-8-sig")

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def entry(self, source, target=None, **kwargs):
        return {"source": source, "purpose": "记录离线同步接口设计", "role": "design", "state": "current",
                "target": target, "reason": "已阅读正文，属于同步模块设计", "uncertainty": None, **kwargs}

    def propose(self, entries, review_id="organize-one"):
        self.manifest.write_text(json.dumps({"schema_version": 1, "documents": entries}, ensure_ascii=False), encoding="utf-8")
        return docs.propose_organize(self.root, review_id, self.manifest, "按用途整理现有文档")

    def apply(self, review_id="organize-one"):
        return docs.apply_organize(self.root, review_id, True, "用户已批准这批整理方案")

    def rollback(self, review_id="organize-one"):
        return docs.rollback_review(self.root, review_id, True, "用户要求恢复整理前原文")

    def test_inventory_read_only_excludes_secrets_dependencies_and_subprojects(self):
        self.write("笔记.md", "# 笔记\n正文")
        self.write("资料.pdf", b"%PDF-not-parsed")
        for path in ("node_modules/a.md", "cache/.env", "secrets/private.md", ".env.local", ".git/config",
                     ".ai-memory/private.md", "child/.git/config", "child/readme.md"):
            self.write(path, "应跳过")
        before = self.snapshot()
        result = docs.inspect_documents(self.root)
        self.assertEqual(before, self.snapshot())
        self.assertEqual({item["path"] for item in result["documents"]}, {"笔记.md", "资料.pdf"})
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["unreviewed_count"], 2)
        self.assertTrue(any(item["path"] == "child" for item in result["skipped"]))

    def test_document_metadata_schema_version_requires_integer(self):
        self.write("a.md", "# Note\n")
        layout = {"schema_version": 1, "document_root": "docs", "index": "docs/index.md", "roles": {}}
        for invalid in (True, 1.0):
            with self.subTest(version=invalid):
                self.write(docs.LAYOUT, json.dumps({**layout, "schema_version": invalid}))
                with self.assertRaisesRegex(ValueError, "schema_version"):
                    docs.load_layout(self.root)
                self.write(docs.LAYOUT, json.dumps(layout))
                self.write(docs.CATALOG, json.dumps({"schema_version": invalid, "documents": []}))
                with self.assertRaisesRegex(ValueError, "清单格式"):
                    docs.inspect_documents(self.root)
                self.write(docs.CATALOG, json.dumps({"schema_version": 1, "documents": []}))
                self.manifest.write_text(json.dumps({"schema_version": invalid,
                                                    "documents": [self.entry("a.md")]}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "manifest"):
                    docs.propose_organize(self.root, "invalid-version", self.manifest, "reviewed")
                self.assertFalse((self.root / ".ai-memory/reviews/invalid-version").exists())

    def test_new_unknown_uses_chinese_fallback_and_preserves_original_name(self):
        self.write("随手记.md", "# 尚未确定用途\n原文必须完整保留。\n")
        result = self.propose([self.entry("随手记.md", role="unknown", uncertainty="缺少上下文")])
        self.assertTrue((self.root / "随手记.md").exists())
        self.assertFalse((self.root / "文档").exists())
        self.assertEqual(result["moves"], [{"source": "随手记.md", "target": "文档/待分类/随手记.md"}])
        self.apply()
        self.assertFalse((self.root / "随手记.md").exists())
        self.assertIn("原文必须完整保留", self.read("文档/待分类/随手记.md"))
        self.assertTrue((self.root / "文档/文档索引.md").exists())
        catalog = json.loads(self.read(docs.CATALOG))
        self.assertEqual(catalog["documents"][0]["original_paths"], ["随手记.md"])

    def test_existing_layout_and_manual_index_survive_with_link_rewrite(self):
        self.write("说明/目录.md", "# 手工维护\r\n\r\n这段保留。\r\n")
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "说明", "index": "说明/目录.md", "roles": {}}))
        self.write("旧 设计.md", "# 设计\n![图片](<图片 文件.png>)\n[规格][spec]\n\n[spec]: <规格.md?mode=1#约束> \"标题\"\n")
        self.write("规格.md", "# 规格\n[设计](<旧 设计.md#标题>)\n")
        self.write("图片 文件.png", b"binary")
        self.write("README.md", "# 说明\n[设计](<旧 设计.md?draft=1#标题>)\n[远程](https://example.test/旧%20设计.md)\n")
        self.propose([self.entry("旧 设计.md", "说明/设计/同步.md")])
        self.apply()
        self.assertIn("../../%E5%9B%BE%E7%89%87%20%E6%96%87%E4%BB%B6.png", self.read("说明/设计/同步.md"))
        self.assertIn("../../%E8%A7%84%E6%A0%BC.md?mode=1#约束", self.read("说明/设计/同步.md"))
        self.assertIn("%E8%AF%B4%E6%98%8E/%E8%AE%BE%E8%AE%A1/%E5%90%8C%E6%AD%A5.md?draft=1#标题", self.read("README.md"))
        self.assertIn("https://example.test/旧%20设计.md", self.read("README.md"))
        self.assertTrue((self.root / "图片 文件.png").exists())
        self.assertTrue((self.root / "说明/目录.md").read_bytes().startswith("# 手工维护\r\n\r\n这段保留。\r\n".encode()))
        self.assertTrue(docs.check_documents(self.root)["document_checks_passed"])

    def test_two_way_references_between_moved_docs_and_parentheses(self):
        self.write("one.md", "[Two](two(v2).md#h)\n")
        self.write("two(v2).md", "[One][x]\n\n[x]: one.md \"title\"\n")
        self.propose([self.entry("one.md", "文档/甲/一.md"), self.entry("two(v2).md", "文档/乙/二.md")])
        self.apply()
        self.assertIn("../%E4%B9%99/%E4%BA%8C.md#h", self.read("文档/甲/一.md"))
        self.assertIn("../%E7%94%B2/%E4%B8%80.md \"title\"", self.read("文档/乙/二.md"))

    @unittest.skipUnless(os.name == "nt", "Windows path matching")
    def test_windows_reference_case_survives_real_move(self):
        self.write("note.md", "# Note\n")
        self.write("caller.md", "[Note](NOTE.MD#section)\n")
        self.assertTrue((self.root / "NOTE.MD").is_file())
        result = self.propose([self.entry("note.md", "docs/note.md")])
        self.assertFalse(result["blocked"])
        self.apply()
        self.assertEqual(self.read("caller.md"), "[Note](docs/note.md#section)\n")
        self.assertTrue(docs.check_documents(self.root)["document_checks_passed"])

    @unittest.skipUnless(os.name == "nt", "Windows path matching")
    def test_windows_unsupported_reference_case_blocks_real_move(self):
        self.write("note.md", "# Note\n")
        for number, reference in enumerate(("`NOTE.MD`", '<a href="NOTE.MD">Note</a>')):
            with self.subTest(reference=reference):
                self.write("caller.md", reference)
                result = self.propose([self.entry("note.md", "docs/note.md")], f"case-ref-{number}")
                self.assertFalse(result["moves"])
                self.assertTrue(result["blocked"])
                self.apply(f"case-ref-{number}")
                self.assertTrue((self.root / "note.md").is_file())
                self.assertEqual(self.read("caller.md"), reference)

    @unittest.skipUnless(os.name == "nt", "Windows path matching")
    def test_windows_mapped_entry_case_remains_protected(self):
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "docs",
                                           "index": "docs/index.md", "roles": {"status": "CURRENT.md"}}))
        self.write("current.md", "# Current\n")
        result = self.propose([self.entry("current.md", "docs/current.md")])
        self.assertFalse(result["moves"])
        self.assertTrue(result["blocked"])
        self.apply()
        self.assertTrue((self.root / "CURRENT.md").is_file())
        self.assertTrue(docs.check_documents(self.root)["document_checks_passed"])

    @unittest.skipUnless(os.name == "nt", "Windows path matching")
    def test_windows_index_cannot_alias_a_role_with_different_case(self):
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "docs",
                                           "index": "docs/index.md", "roles": {"status": "docs/INDEX.md"}}))
        with self.assertRaisesRegex(ValueError, "不能同时"):
            docs.load_layout(self.root)

    def test_posix_reference_case_distinctions_are_preserved(self):
        # Keep this policy test independent of the host's filesystem case rules.
        with patch.object(docs.os.path, "normcase", side_effect=posixpath.normcase):
            text = "[moved](note.md) [different file](NOTE.md)"
            self.assertEqual(docs._rewrite(text, "caller.md", "caller.md", {"note.md": "docs/note.md"}),
                             "[moved](docs/note.md) [different file](NOTE.md)")
            self.assertFalse(docs._mentions("`NOTE.md`", "note.md"))

    def test_outside_project_relative_link_keeps_source_in_place(self):
        outside = self.root.parent / "external.md"
        outside.write_bytes(b"outside document\r\n")
        self.write("a.md", "[External](../external.md#section)\n")
        result = self.propose([self.entry("a.md", "docs/a.md")])
        self.assertFalse(result["moves"])
        self.assertIn("项目外相对路径", result["blocked"][0]["reason"])
        self.apply()
        self.assertEqual(self.read("a.md"), "[External](../external.md#section)\n")
        self.assertFalse((self.root / "docs/a.md").exists())
        self.assertEqual(outside.read_bytes(), b"outside document\r\n")

    def test_project_root_relative_link_survives_real_move(self):
        self.write("a.md", "[Root](./?view=all#top)\n")
        result = self.propose([self.entry("a.md", "docs/deep/a.md")])
        self.assertFalse(result["blocked"])
        self.apply()
        self.assertEqual(self.read("docs/deep/a.md"), "[Root](../..?view=all#top)\n")
        self.assertTrue(docs.check_documents(self.root)["document_checks_passed"])

    def test_unsupported_html_wiki_code_references_keep_target_in_place(self):
        for number, reference in enumerate(("<a href='note.md'>说明</a>", "[[note]]", "`note.md`",
                                             "```md\n[note](note.md)\n```", "    [note](note.md)")):
            with self.subTest(reference=reference):
                self.write("note.md", "# note\n")
                self.write("caller.md", reference + "\n")
                result = self.propose([self.entry("note.md", "文档/note.md")], f"case-{number}")
                self.assertEqual(result["moves"], [])
                self.assertEqual(result["blocked"][0]["source"], "note.md")

    def test_code_configuration_reference_keeps_source_in_place(self):
        self.write("note.md", "# note")
        self.write("config.json", '{"help":"note.md"}')
        result = self.propose([self.entry("note.md", "文档/note.md")])
        self.assertIn("代码或配置", result["blocked"][0]["reason"])
        self.apply()
        self.assertTrue((self.root / "note.md").exists())

    def test_unrewritable_outgoing_html_or_wiki_preserves_source_location(self):
        for number, body in enumerate(("<img src='image.png'>", "[[other]]", "`../config.json`")):
            self.write("note.md", body)
            result = self.propose([self.entry("note.md", "文档/note.md")], f"outgoing-{number}")
            self.assertEqual(result["moves"], [])
            self.assertTrue(result["blocked"])

    def test_root_relative_site_links_are_reported_and_preserved(self):
        self.write("note.md", "# note")
        self.write("README.md", "[note](/note.md)")
        result = self.propose([self.entry("note.md", "文档/note.md")])
        self.assertIn("站点根路径", result["blocked"][0]["reason"])

    def test_manifest_inside_project_is_review_input_not_false_reference(self):
        self.write("note.md", "# note")
        self.manifest = self.root / "分类清单.json"
        result = self.propose([self.entry("note.md", "文档/note.md")])
        self.assertFalse(result["blocked"])
        self.apply()
        self.assertTrue((self.root / "文档/note.md").exists())

    def test_manifest_with_parent_segments_is_excluded_from_reference_scan(self):
        self.write("note.md", "# note")
        (self.root / "input").mkdir()
        self.manifest = self.root / "input" / ".." / "分类清单.json"
        result = self.propose([self.entry("note.md", "文档/note.md")])
        self.assertFalse(result["blocked"])
        self.apply()
        self.assertTrue((self.root / "文档/note.md").exists())
        self.rollback()
        self.assertEqual(self.read("note.md"), "# note")

    def test_manifest_through_project_root_alias_is_excluded_from_reference_scan(self):
        self.write("note.md", "# note")
        alias = Path(self.temporary.name) / "project-alias"
        try:
            alias.symlink_to(self.root.resolve(), target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        self.root = alias
        self.manifest = alias / "分类清单.json"
        result = self.propose([self.entry("note.md", "文档/note.md")])
        self.assertFalse(result["blocked"])
        self.apply()
        self.assertTrue((alias / "文档/note.md").exists())
        self.rollback()
        self.assertEqual(self.read("note.md"), "# note")

    def test_protected_entries_and_managed_logs_never_move(self):
        names = ["AGENTS.md", "sub/AGENTS.md", "README.md", "SKILL.md", "task_plan.md",
                 ".planning/STATE.md", "openspec/spec.md", "docs/memory/journal/2026-10-15/entry.md"]
        for name in names:
            self.write(name, "# 内容")
        result = self.propose([self.entry(name, f"文档/{number}.md") for number, name in enumerate(names)])
        self.assertEqual(len(result["blocked"]), len(names))
        self.assertEqual(result["moves"], [])
        self.apply()
        self.assertTrue(all((self.root / name).exists() for name in names))

    def test_scope_plan_and_custom_roles_are_protected(self):
        self.write("流程/当前工作.md", "# 当前工作")
        self.write(".ai-memory/scopes/work.json", json.dumps({"plan": "流程/当前工作.md"}))
        self.write("说明/项目规则.md", "# 规则")
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "说明", "roles": {"rules": "说明/项目规则.md"}}))
        result = self.propose([self.entry("流程/当前工作.md", "说明/移动工作.md"), self.entry("说明/项目规则.md", "说明/移动规则.md")])
        self.assertEqual(len(result["blocked"]), 2)

    def test_immutable_log_incoming_reference_blocks_move(self):
        log = "docs/memory/journal/2026-10-15/entry.md"
        self.write("design.md", "# 设计")
        original = "# evidence\n[设计](../../../../design.md)\n"
        self.write(log, original)
        result = self.propose([self.entry("design.md", "文档/design.md")])
        self.assertIn("不可变日志", result["blocked"][0]["reason"])
        self.apply()
        self.assertEqual(self.read(log), original)

    def test_non_markdown_is_registered_but_move_is_rejected(self):
        self.write("报告.pdf", b"%PDF-body")
        with self.assertRaisesRegex(ValueError, "仅登记"):
            self.propose([self.entry("报告.pdf", "文档/报告.pdf")])
        self.propose([self.entry("报告.pdf", purpose="外部报告", role="evidence")])
        self.apply()
        self.assertEqual((self.root / "报告.pdf").read_bytes(), b"%PDF-body")

    def test_partial_classification_and_incremental_hashes_are_honest(self):
        self.write("已读.md", "# 原文")
        self.write("未读.md", "# 未读")
        result = self.propose([self.entry("已读.md")])
        self.assertEqual(result["pending"], ["未读.md"])
        self.apply()
        result = docs.inspect_documents(self.root)
        items = {item["path"]: item for item in result["documents"]}
        self.assertEqual(items["已读.md"]["change"], "unchanged")
        self.assertTrue(items["已读.md"]["classification"]["classified"])
        self.assertIn("未读.md", result["pending"])
        self.write("已读.md", "# 内容改变")
        items = {item["path"]: item for item in docs.inspect_documents(self.root)["documents"]}
        self.assertEqual(items["已读.md"]["change"], "changed")
        self.assertIsNone(items["已读.md"]["classification"])

    def test_collision_and_unsafe_paths_leave_originals_unchanged(self):
        self.write("a.md", "A")
        self.write("b.md", "B")
        for target in ("b.md", "../escape.md", "C:/escape.md", "docs/con.md", "docs//bad.md", ".ai-memory/bad.md",
                       "docs/memory/journal/x/bad.md", "a.md "):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.propose([self.entry("a.md", target)])
        self.assertEqual(self.read("a.md"), "A")
        self.assertEqual(self.read("b.md"), "B")

    def test_target_in_nested_project_rejected(self):
        self.write("a.md", "A")
        self.write("child/.ai-memory/project.json", "{}")
        with self.assertRaisesRegex(ValueError, "子项目"):
            self.propose([self.entry("a.md", "child/a.md")])

    def test_expected_classification_hash_rejects_stale_ai_input(self):
        self.write("a.md", "changed")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.propose([self.entry("a.md", sha256="0" * 64)])

    def test_apply_and_rollback_require_confirmation(self):
        self.write("a.md", "A")
        self.propose([self.entry("a.md", "文档/a.md")])
        for func in (docs.apply_organize, docs.rollback_review):
            with self.assertRaises(ValueError):
                func(self.root, "organize-one", False, "yes")
            with self.assertRaises(ValueError):
                func(self.root, "organize-one", True, "")

    def test_apply_rollback_preserve_exact_bytes_and_are_idempotent(self):
        raw = b"\xef\xbb\xbf# original\r\nbody\r\n"
        self.write("a.md", raw)
        self.propose([self.entry("a.md", "文档/a.md")])
        self.apply()
        self.assertEqual((self.root / "文档/a.md").read_bytes(), raw)
        self.assertTrue(self.apply()["already_applied"])
        self.rollback()
        self.assertEqual((self.root / "a.md").read_bytes(), raw)
        self.assertFalse((self.root / "文档/a.md").exists())
        self.assertFalse((self.root / docs.CATALOG).exists())
        self.assertTrue(self.rollback()["already_rolled_back"])
        with self.assertRaisesRegex(ValueError, "回滚"):
            self.apply()

    def test_original_or_reference_drift_rejected_before_any_mutation(self):
        self.write("a.md", "A")
        self.write("README.md", "[a](a.md)")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.write("README.md", "[a](a.md) changed")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.apply()
        self.assertTrue((self.root / "a.md").exists())
        self.assertFalse((self.root / "文档/a.md").exists())

    def test_new_reference_file_requires_fresh_review(self):
        self.write("a.md", "A")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.write("new.md", "[a](a.md)")
        with self.assertRaisesRegex(ValueError, "集合已变化"):
            self.apply()

    def test_changed_stage_or_proposal_refused(self):
        for number, artifact in enumerate(("proposal.json", "after/00000.bin", "before/00002.bin")):
            self.write("a.md", "A")
            review = f"drift-{number}"
            self.propose([self.entry("a.md", "文档/a.md")], review)
            path = self.root / ".ai-memory/reviews" / review / artifact
            if not path.exists():
                path = next((self.root / ".ai-memory/reviews" / review / "before").glob("*.bin"))
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                self.apply(review)
            self.assertTrue((self.root / "a.md").exists())

    def test_human_edit_blocks_rollback_and_preserves_new_work(self):
        self.write("a.md", "A")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.apply()
        self.write("文档/a.md", "人工新内容")
        with self.assertRaisesRegex(ValueError, "人工改动"):
            self.rollback()
        self.assertEqual(self.read("文档/a.md"), "人工新内容")

    def test_new_incoming_reference_blocks_rollback_before_deleting_target(self):
        self.write("a.md", "A")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.apply()
        self.write("后来新增.md", "[文档](文档/a.md)")
        with self.assertRaisesRegex(ValueError, "集合已变化"):
            self.rollback()
        self.assertTrue((self.root / "文档/a.md").exists())

    def test_changed_unmodified_reference_blocks_rollback(self):
        self.write("a.md", "A")
        self.write("b.md", "B")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.apply()
        self.write("b.md", "[新引用](文档/a.md)")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.rollback()
        self.assertTrue((self.root / "文档/a.md").exists())

    def test_new_reference_decode_error_is_explicit_and_prevents_apply(self):
        self.write("a.md", "A")
        self.write("config.json", "{}")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.write("config.json", b"\xff\xfeinvalid")
        with self.assertRaisesRegex(ValueError, "引用扫描错误"):
            self.apply()

    def test_plan_mapping_change_requires_fresh_review(self):
        self.write("a.md", "A")
        self.propose([self.entry("a.md", "文档/a.md")])
        self.write(".ai-memory/scopes/new.json", json.dumps({"plan": "a.md"}))
        with self.assertRaisesRegex(ValueError, "活动计划映射"):
            self.apply()

    def test_interrupt_after_every_apply_step_can_resume(self):
        self.write("a.md", "[b](b.md)\n")
        self.write("b.md", "B")
        result = self.propose([self.entry("a.md", "文档/a.md")])
        operations = json.loads(Path(result["proposal"]).read_text(encoding="utf-8"))["operations"]
        # Interrupt at each actual write by rolling back and using a fresh proposal.
        for position in range(len(operations)):
            review = f"interrupt-{position}"
            self.propose([self.entry("a.md", "文档/a.md")], review)
            original = docs._mutate
            count = 0

            def fail_after(root, review_id, operation, phase):
                nonlocal count
                original(root, review_id, operation, phase)
                current = count
                count += 1
                if current == position:
                    raise OSError("模拟单文件写入后进程退出")

            with patch.object(docs, "_mutate", side_effect=fail_after), self.assertRaises(OSError):
                self.apply(review)
            self.apply(review)
            self.assertFalse((self.root / "a.md").exists())
            self.rollback(review)
            self.assertEqual(self.read("a.md"), "[b](b.md)\n")

    def test_partial_apply_can_be_rolled_back_and_rollback_interruption_resumes(self):
        self.write("a.md", "A")
        self.propose([self.entry("a.md", "文档/a.md")])
        original = docs._mutate

        def fail_after(*args):
            original(*args)
            raise OSError("interrupted")

        with patch.object(docs, "_mutate", side_effect=fail_after), self.assertRaises(OSError):
            self.apply()
        with patch.object(docs, "_mutate", side_effect=fail_after), self.assertRaises(OSError):
            self.rollback()
        self.rollback()
        self.assertEqual(self.read("a.md"), "A")
        self.assertFalse((self.root / "文档/a.md").exists())

    def test_index_markers_invalid_refuse_proposal(self):
        self.write("文档/文档索引.md", "手工内容\n" + docs.START)
        self.write("a.md", "A")
        with self.assertRaisesRegex(ValueError, "标记"):
            self.propose([self.entry("a.md")])
        self.assertIn("手工内容", self.read("文档/文档索引.md"))

    def test_non_utf8_markdown_is_registered_and_not_moved(self):
        self.write("旧编码.md", b"\xff\xfeinvalid")
        result = self.propose([self.entry("旧编码.md", "文档/旧编码.md")])
        self.assertTrue(result["blocked"])
        self.apply()
        self.assertEqual((self.root / "旧编码.md").read_bytes(), b"\xff\xfeinvalid")

    def test_existing_index_managed_region_updates_only_once(self):
        self.write("a.md", "A")
        self.write("docs/INDEX.md", "# 人工索引\n\n" + docs.START + "\n旧自动内容\n" + docs.END + "\n\n人工尾部\n")
        self.propose([self.entry("a.md")])
        self.apply()
        text = self.read("docs/INDEX.md")
        self.assertEqual(text.count(docs.START), 1)
        self.assertTrue(text.startswith("# 人工索引\n\n"))
        self.assertTrue(text.endswith("\n\n人工尾部\n"))
        self.assertNotIn("旧自动内容", text)

    def test_custom_layout_check_uses_actual_roles_and_links(self):
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "资料", "index": "资料/目录.md",
                                           "roles": {"rules": "约定.md", "status": "当前.md"}}))
        self.write("约定.md", "# 约定")
        self.write("当前.md", "[目录](资料/目录.md)")
        self.write("资料/目录.md", "[约定](../约定.md)")
        result = docs.check_documents(self.root)
        self.assertTrue(result["document_checks_passed"], result)
        self.write("资料/目录.md", "[坏链接](不存在.md)")
        self.assertFalse(docs.check_documents(self.root)["document_checks_passed"])

    def test_custom_layout_budgets_follow_roles_and_keep_fixed_agents_entry(self):
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "文档", "index": "文档/索引.md",
            "roles": {"rules": "文档/规则.md", "status": "文档/当前状态.md"}}, ensure_ascii=False))
        self.write("文档/索引.md", "# 索引\n")
        self.write("文档/规则.md", "规" * 2800)
        self.write("文档/当前状态.md", "状态\n" * 101)
        self.write("AGENTS.md", "规则\n" * 101)
        before = self.snapshot()
        result = docs.check_documents(self.root)
        budgets = {item["path"]: item for item in result["warnings"] if item.get("code") == "entry_budget"}
        self.assertEqual(set(budgets), {"文档/规则.md", "文档/当前状态.md", "AGENTS.md"})
        self.assertGreater(budgets["文档/规则.md"]["bytes"], 8192)
        self.assertEqual(budgets["文档/当前状态.md"]["lines"], 101)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(result["document_checks_passed"])

    def test_mapped_agents_budget_warning_is_not_duplicated(self):
        self.write(docs.LAYOUT, json.dumps({"schema_version": 1, "document_root": "文档", "index": "文档/索引.md",
                                           "roles": {"rules": "AGENTS.md", "status": "AGENTS.md"}}))
        self.write("文档/索引.md", "# 索引\n")
        self.write("AGENTS.md", "规则\n" * 101)
        warnings = [item for item in docs.check_documents(self.root)["warnings"] if item.get("code") == "entry_budget"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["roles"], ["rules", "status"])

    def test_nested_image_link_and_parenthesized_titles_survive_real_move(self):
        self.write("note.md", "# Design\n")
        self.write("image.png", b"image")
        self.write("caller.md", "[![diagram](image.png)](note.md)\n\n"
                   "[inline](note.md (A title))\n\n[reference][ref]\n\n[ref]: note.md (A title)\n")
        result = self.propose([self.entry("note.md", "docs/design/note.md")])
        self.assertFalse(result["blocked"])
        self.apply()
        body = self.read("caller.md")
        self.assertIn("[![diagram](image.png)](docs/design/note.md)", body)
        self.assertIn("[inline](docs/design/note.md (A title))", body)
        self.assertIn("[ref]: docs/design/note.md (A title)", body)
        self.assertTrue(docs.check_documents(self.root)["document_checks_passed"])

    def test_unquoted_html_src_srcset_and_css_urls_keep_source_in_place(self):
        self.write("image.png", b"image")
        for index, body in enumerate(("<img src=image.png>\n", '<source srcset="image.png 1x, other.png 2x">\n',
                                      '<div style="background: url(image.png)">text</div>\n',
                                      '<img title="a > b" src=image.png>\n')):
            with self.subTest(body=body):
                self.write("caller.md", body)
                result = self.propose([self.entry("caller.md", "docs/caller.md")], f"html-{index}")
                self.assertFalse(result["moves"])
                self.assertIn("html", result["blocked"][0]["reason"])
                self.apply(f"html-{index}")
                self.assertEqual(self.read("caller.md"), body)
                self.assertFalse((self.root / "docs/caller.md").exists())

    def test_nonclosing_fence_text_is_code_and_never_rewritten(self):
        self.write("note.md", "# Note\n")
        for index, body in enumerate(("```text\nexample\n```not-a-close\n[note](note.md)\n```\n",
                                      "> ~~~md\n> [note](note.md)\n> ~~~\n",
                                      "- ~~~md\n  [note](note.md)\n  ~~~\n")):
            with self.subTest(body=body):
                self.write("caller.md", body)
                result = self.propose([self.entry("note.md", "docs/note.md")], f"fence-{index}")
                self.assertFalse(result["moves"])
                self.assertIn("code", result["blocked"][0]["reason"])
                self.apply(f"fence-{index}")
                self.assertEqual(self.read("caller.md"), body)
                self.assertTrue((self.root / "note.md").exists())


if __name__ == "__main__":
    unittest.main()
