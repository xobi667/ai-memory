"""Freshness findings are explainable, isolated, bounded and never semantic proof."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import memory_health as health
from memory_store import configure_scope
from memory_workflow import FIELDS


class HealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory_health_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, relative, content):
        return self.write(relative, json.dumps(content, ensure_ascii=False))

    def task(self, task_id="work", plan=None):
        return configure_scope(self.root, task_id, {"goal": "核对文档和实现", "detail": "standard" if plan else "small",
            "modules": ["project"], "record_fields": list(FIELDS), "plan": plan,
            "confirmation_summary": "范围已确认"}, confirmed=True)["scope"]

    def manifest(self, review_id="review1", documents=None, task_id=None):
        result = {"schema_version": 1, "review_id": review_id, "summary": "已读取文档、实现和测试，登记核对依据。",
                  "documents": documents if documents is not None else [{"path": "文档/说明.md", "dependencies": ["src/app.py"]}]}
        if task_id is not None:
            result["task_id"] = task_id
        return result

    def prepared(self):
        self.task()
        self.write("文档/说明.md", "# 用途\n由 src/app.py 提供。\n")
        self.write("src/app.py", "value = 1\n")
        return health.review_documents(self.root, self.manifest())

    def snapshot(self, review_id="review1"):
        path = self.root / f".ai-memory/reviews/{review_id}/evidence.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def file_state(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_new_project_health_creates_no_files_and_exposes_missing_coverage(self):
        self.write("README.md", "Existing project\n")
        before = self.file_state()
        result = health.health(self.root)
        self.assertFalse(result["registered"])
        self.assertFalse(result["coverage"]["review_snapshots"])
        self.assertEqual(result["business_validation"], "not_performed")
        self.assertTrue(result["semantic_review_required"])
        self.assertEqual(before, self.file_state())
        self.assertFalse((self.root / ".ai-memory").exists())

    def test_snapshot_records_hashes_without_modifying_source_or_task(self):
        self.task()
        self.write("文档/说明.md", "# 说明\n")
        self.write("src/app.py", "value = 1\n")
        before = self.file_state()
        result = health.review_documents(self.root, self.manifest())
        for relative, content in before.items():
            self.assertEqual(content, (self.root / relative).read_bytes())
        _, snapshot = self.snapshot()
        self.assertEqual(snapshot["documents"][0]["dependencies"][0]["sha256"],
                         hashlib.sha256((self.root / "src/app.py").read_bytes()).hexdigest())
        self.assertFalse(result["source_modified"])
        self.assertFalse(result["claim_verified_by_script"])
        before = self.file_state()
        check = health.health(self.root)
        self.assertFalse(check["needs_attention"])
        self.assertEqual(check["checked_counts"]["dependencies"], 1)
        self.assertEqual(before, self.file_state())

    def test_dependency_change_is_review_clue_not_confirmed_semantic_conflict(self):
        self.prepared()
        self.write("src/app.py", "value = 2\n")
        result = health.health(self.root)
        self.assertEqual([item["code"] for item in result["changes"]], ["dependency_changed"])
        self.assertEqual(result["changes"][0]["document"], "文档/说明.md")
        self.assertEqual(result["conflicts"], [])
        self.assertTrue(result["needs_attention"])
        self.assertTrue(result["changes"][0]["semantic_review_required"])

    def test_document_change_and_missing_dependency_are_distinct(self):
        self.prepared()
        self.write("文档/说明.md", "# New text\n")
        (self.root / "src/app.py").unlink()
        result = health.health(self.root)
        self.assertEqual({item["code"] for item in result["changes"]}, {"review_document_changed", "dependency_missing"})
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["errors"], [])

    def test_missing_reviewed_document_preserves_evidence_and_reports_path(self):
        self.prepared()
        (self.root / "文档/说明.md").unlink()
        before = self.file_state()
        result = health.health(self.root)
        self.assertEqual(result["changes"][0]["code"], "review_document_missing")
        self.assertEqual(before, self.file_state())

    def test_catalog_reuses_existing_classification_hash_without_refreshing_it(self):
        scope = self.task()
        self.write("文档/说明.md", "new content\n")
        path = self.write_json(health.CATALOG, {"schema_version": 1, "project_id": scope["project_id"], "documents": [
            {"source": "文档/说明.md", "sha256": "0" * 64}, {"source": "文档/丢失.md", "sha256": "1" * 64}]})
        before = path.read_bytes()
        result = health.health(self.root)
        self.assertEqual({item["code"] for item in result["changes"]}, {"catalog_document_changed", "catalog_document_missing"})
        self.assertEqual(result["checked_counts"]["catalog_documents"], 2)
        self.assertEqual(before, path.read_bytes())

    def test_foreign_catalog_cannot_be_treated_as_local_baseline(self):
        self.task()
        self.write("文档/说明.md", "local content")
        self.write_json(health.CATALOG, {"schema_version": 1, "project_id": str(uuid.uuid4()), "documents": [
            {"source": "文档/说明.md", "sha256": "0" * 64}]})
        result = health.health(self.root)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["errors"][0]["code"], "invalid_catalog")
        self.assertFalse(result["scan_complete"])

    def test_new_review_does_not_silently_resolve_divergent_heads(self):
        self.prepared()
        self.write("src/app.py", "value = 2\n")
        health.review_documents(self.root, self.manifest("review2"))
        result = health.health(self.root)
        self.assertEqual(result["conflicts"][0]["code"], "parallel_review_heads")
        self.assertEqual(result["conflicts"][0]["review_ids"], ["review1", "review2"])
        self.assertEqual(result["checked_counts"]["review_documents"], 2)

    def test_explicit_supersedes_resolves_only_selected_document(self):
        self.task()
        self.write("文档/说明.md", "one")
        self.write("文档/另一份.md", "two")
        self.write("src/app.py", "value = 1")
        documents = [{"path": path, "dependencies": ["src/app.py"]} for path in ("文档/说明.md", "文档/另一份.md")]
        health.review_documents(self.root, self.manifest(documents=documents))
        self.write("src/app.py", "value = 2")
        health.review_documents(self.root, self.manifest("review2", [
            {"path": "文档/说明.md", "dependencies": ["src/app.py"], "supersedes": ["review1"]}]))
        result = health.health(self.root)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["document"], "文档/另一份.md")
        self.assertEqual(result["conflicts"], [])

    def test_resolving_multiple_heads_requires_explicit_refs_and_keeps_history(self):
        self.prepared()
        self.write("src/app.py", "value = 2")
        health.review_documents(self.root, self.manifest("review2"))
        before = {name: self.snapshot(name)[0].read_bytes() for name in ("review1", "review2")}
        health.review_documents(self.root, self.manifest("review3", [
            {"path": "文档/说明.md", "dependencies": ["src/app.py"], "supersedes": ["review1", "review2"]}]))
        result = health.health(self.root)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["checked_counts"]["review_documents"], 1)
        for name, content in before.items():
            self.assertEqual(self.snapshot(name)[0].read_bytes(), content)

    def test_cross_task_supersedes_is_rejected_and_task_filter_includes_project_only(self):
        self.prepared()
        self.task("other")
        health.review_documents(self.root, self.manifest("other1", task_id="other"))
        with self.assertRaisesRegex(ValueError, "同一任务"):
            health.review_documents(self.root, self.manifest("bad", [
                {"path": "文档/说明.md", "dependencies": [], "supersedes": ["other1"]}], task_id="work"))
        health.review_documents(self.root, self.manifest("work1", task_id="work"))
        self.write("src/app.py", "value = 3")
        result = health.health(self.root, "work")
        self.assertEqual({item["review_id"] for item in result["changes"]}, {"review1", "work1"})
        self.assertEqual(result["checked_counts"]["task_states"], 1)
        self.assertEqual(result["conflicts"], [])

    def test_missing_related_task_does_not_contaminate_selected_task(self):
        self.prepared()
        self.task("other")
        health.review_documents(self.root, self.manifest("other1", task_id="other"))
        (self.root / ".ai-memory/scopes/other.json").unlink()
        result = health.health(self.root, "work")
        self.assertEqual(result["errors"], [])
        self.assertTrue(health.health(self.root)["errors"])

    def test_review_rejects_existing_proposal_namespace_without_overwrite(self):
        self.task()
        self.write("文档/说明.md", "content")
        self.write("src/app.py", "value=1")
        proposal = self.write(".ai-memory/reviews/review1/proposal.json", "existing proposal")
        before = self.file_state()
        with self.assertRaisesRegex(ValueError, "ID 已存在"):
            health.review_documents(self.root, self.manifest())
        self.assertEqual(proposal.read_text(encoding="utf-8"), "existing proposal")
        self.assertEqual(before, self.file_state())

    def test_orphan_proposal_seal_also_reserves_review_id(self):
        self.task()
        self.write("文档/说明.md", "content")
        self.write("src/app.py", "value=1")
        self.write(".ai-memory/review-seals/review1.json", "orphan seal")
        before = self.file_state()
        with self.assertRaisesRegex(ValueError, "ID 已存在"):
            health.review_documents(self.root, self.manifest())
        self.assertEqual(before, self.file_state())

    def test_snapshot_foreign_identity_does_not_read_referenced_files(self):
        self.prepared()
        path, snapshot = self.snapshot()
        snapshot["project_id"] = str(uuid.uuid4())
        snapshot["documents"][0]["dependencies"][0]["path"] = "../outside.txt"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        result = health.health(self.root)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["errors"][0]["code"], "invalid_review")
        self.assertEqual(result["checked_counts"]["dependencies"], 0)

    def test_invalid_snapshot_isolated_from_other_valid_snapshot(self):
        self.prepared()
        health.review_documents(self.root, self.manifest("review2"))
        self.snapshot()[0].write_text("not json", encoding="utf-8")
        self.write("src/app.py", "value = 2")
        result = health.health(self.root)
        self.assertEqual(result["errors"][0]["code"], "invalid_review")
        self.assertEqual(result["changes"][0]["review_id"], "review2")
        self.assertFalse(result["scan_complete"])

    def test_bool_float_versions_and_false_claim_flag_are_rejected(self):
        self.prepared()
        path, snapshot = self.snapshot()
        for field, values in (("schema_version", (True, 1.0)), ("claim_verified_by_script", (True, "false")),
                              ("summary", (None, ""))):
            for value in values:
                with self.subTest(field=field, value=value):
                    altered = dict(snapshot, **{field: value})
                    path.write_text(json.dumps(altered), encoding="utf-8")
                    self.assertEqual(health.health(self.root)["errors"][0]["code"], "invalid_review")
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        for value in (True, 1.0):
            with self.assertRaises(ValueError):
                health.review_documents(self.root, dict(self.manifest("new"), schema_version=value))

    def test_outside_nested_project_internal_and_sensitive_dependencies_rejected(self):
        self.task()
        self.write("文档/说明.md", "content")
        self.write("nested/.git/config", "project boundary")
        self.write("nested/app.py", "value = 1")
        self.write(".env", "PRIVATE=not-read")
        for index, path in enumerate(("../outside.txt", "nested/app.py", ".env", ".ai-memory/project.json")):
            with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                health.review_documents(self.root, self.manifest(f"review{index}", [
                    {"path": "文档/说明.md", "dependencies": [path]}]))
        self.assertFalse((self.root / health.REVIEW_DIRECTORY).exists())

    def test_symlink_or_junction_is_not_followed(self):
        self.task()
        self.write("文档/说明.md", "content")
        target = self.write("src/app.py", "content")
        link = self.root / "linked.py"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Host does not allow unprivileged file symlinks")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            health.review_documents(self.root, self.manifest(documents=[{"path": "文档/说明.md", "dependencies": ["linked.py"]}]))

    def test_missing_identity_with_state_never_reads_unowned_reviews(self):
        self.write_json(".ai-memory/reviews/unowned/evidence.json", {"documents": []})
        before = self.file_state()
        result = health.health(self.root)
        self.assertFalse(result["registered"])
        self.assertEqual(result["errors"][0]["code"], "invalid_project_or_task")
        self.assertEqual(before, self.file_state())

    def test_new_review_without_project_registration_does_not_initialize(self):
        self.write("文档/说明.md", "text")
        with self.assertRaises(FileNotFoundError):
            health.review_documents(self.root, self.manifest(documents=[{"path": "文档/说明.md"}]))
        self.assertFalse((self.root / ".ai-memory").exists())

    def test_redundant_states_are_conflict_but_multiple_active_tasks_are_not(self):
        self.task()
        self.task("other")
        result = health.health(self.root)
        self.assertEqual(result["conflicts"], [])
        self.write("文档/任务.md", "<!-- ai-memory-task-status: active -->\n<!-- ai-memory-task-status: completed -->\n")
        self.task("planned", "文档/任务.md")
        result = health.health(self.root)
        self.assertEqual(result["conflicts"][0]["code"], "task_state_conflict")
        self.assertEqual(result["conflicts"][0]["task_id"], "planned")

    def test_cycle_and_missing_review_relation_are_explicit_conflicts(self):
        self.prepared()
        health.review_documents(self.root, self.manifest("review2"))
        for name, target in (("review1", "review2"), ("review2", "review1")):
            path, snapshot = self.snapshot(name)
            snapshot["documents"][0]["supersedes"] = [target]
            path.write_text(json.dumps(snapshot), encoding="utf-8")
        result = health.health(self.root)
        self.assertEqual({item["code"] for item in result["conflicts"]}, {"review_cycle"})
        self.assertEqual(result["checked_counts"]["review_documents"], 2)
        path, snapshot = self.snapshot("review1")
        snapshot["documents"][0]["supersedes"] = ["missing"]
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertIn("missing_review_target", {item["code"] for item in health.health(self.root)["conflicts"]})

    def test_graph_does_not_mark_ancestor_of_cycle_as_cyclic(self):
        def snapshot(name, targets):
            return {"review_id": name, "documents": [{"path": "doc.md", "sha256": "0" * 64,
                    "dependencies": [], "supersedes": targets}]}
        _, conflicts = health._review_graph([snapshot("a", ["b"]), snapshot("b", ["c"]), snapshot("c", ["b"])])
        self.assertEqual({item["review_id"] for item in conflicts if item["code"] == "review_cycle"}, {"b", "c"})

    def test_cross_branch_cycle_cannot_retire_valid_review(self):
        graph = {"a": ["b", "c"], "b": ["c", "d"], "c": ["a"], "d": []}
        snapshots = [{"review_id": name, "documents": [{"path": "doc.md", "sha256": "0" * 64,
                      "dependencies": [], "supersedes": targets}]} for name, targets in graph.items()]
        heads, conflicts = health._review_graph(snapshots)
        self.assertEqual({item["review_id"] for item in conflicts if item["code"] == "review_cycle"}, {"a", "b", "c"})
        self.assertEqual({node[0] for node in heads}, {"a", "b", "c", "d"})

    def test_bounded_report_preserves_totals_and_attention(self):
        scope = self.task()
        entries = [{"source": f"文档/长名称-{index}.md", "sha256": "0" * 64} for index in range(100)]
        self.write_json(health.CATALOG, {"schema_version": 1, "project_id": scope["project_id"], "documents": entries})
        result = health.health(self.root, max_bytes=2048)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")), 2048)
        self.assertEqual(result["total_counts"]["changes"], 100)
        self.assertTrue(result["needs_attention"])
        self.assertTrue(result["output_truncated"])
        self.assertEqual(len(result["changes"]) + result["omitted_counts"]["changes"], 100)
        self.assertTrue(result["scan_complete"])

    def test_bad_budget_and_manifest_are_rejected(self):
        for budget in (True, 2048.0, 2047, 1048577):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                health.health(self.root, max_bytes=budget)
        self.task()
        for manifest in ({}, [], dict(self.manifest(), unknown=True), dict(self.manifest(), summary="")):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                health.review_documents(self.root, manifest)

    def test_cli_reports_changes_without_treating_them_as_execution_error(self):
        self.prepared()
        self.write("src/app.py", "changed")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "memory_health.py"),
            "health", "--root", str(self.root)], capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["needs_attention"])
        self.snapshot()[0].write_text("corrupt", encoding="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "memory_health.py"),
            "health", "--root", str(self.root)], capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_organize_move_review_and_rollback_keep_traceable_document_identity(self):
        import memory_documents as documents
        scope = self.task()
        self.write("旧说明.md", "# 说明\n[依赖](support.md)\n")
        self.write("support.md", "# 证据\n")
        old = self.root / "旧说明.md"
        self.write_json(health.CATALOG, {"schema_version": 1, "project_id": scope["project_id"], "documents": [
            {"source": "旧说明.md", "sha256": hashlib.sha256(old.read_bytes()).hexdigest(), "original_paths": [],
             "purpose": "说明", "role": "design", "state": "current", "reason": "已读", "classified": True}]})
        health.review_documents(self.root, self.manifest(documents=[{"path": "旧说明.md", "dependencies": ["support.md"]}]))
        manifest = self.write_json(".ai-memory/分类.json", {"schema_version": 1, "documents": [
            {"source": "旧说明.md", "target": "文档/设计/说明.md", "purpose": "说明", "role": "design",
             "state": "current", "reason": "已阅读", "uncertainty": None}]})
        proposal = documents.propose_organize(self.root, "move", manifest, "整理已核对的说明")
        self.assertEqual(len(proposal["moves"]), 1)
        documents.apply_organize(self.root, "move", True, "执行这批整理")
        moved = health.health(self.root)
        change = next(item for item in moved["changes"] if item["code"] == "review_document_changed")
        self.assertEqual(change["path"], "文档/设计/说明.md")
        self.assertEqual(change["reviewed_path"], "旧说明.md")
        self.assertFalse(any(item["code"].endswith("_missing") for item in moved["changes"]))
        health.review_documents(self.root, self.manifest("review2", [
            {"path": "文档/设计/说明.md", "dependencies": ["support.md"], "supersedes": ["review1"]}]))
        reviewed = health.health(self.root)
        self.assertEqual(reviewed["conflicts"], [])
        self.assertEqual(reviewed["changes"], [])
        self.assertEqual(reviewed["checked_counts"]["review_documents"], 1)
        documents.rollback_review(self.root, "move", True, "恢复整理前的说明")
        rolled_back = health.health(self.root)
        change = next(item for item in rolled_back["changes"] if item["code"] == "review_document_changed")
        self.assertEqual(change["path"], "旧说明.md")
        self.assertEqual(change["reviewed_path"], "文档/设计/说明.md")
        self.assertEqual(rolled_back["conflicts"], [])
        health.review_documents(self.root, self.manifest("review3", [
            {"path": "旧说明.md", "dependencies": ["support.md"], "supersedes": ["review2"]}]))
        self.assertFalse(health.health(self.root)["needs_attention"])

    def test_first_organize_rollback_uses_sealed_provenance_after_catalog_disappears(self):
        import memory_documents as documents
        self.task()
        self.write("旧说明.md", "# 说明\n[证据](support.md)\n")
        self.write("support.md", "# 验证依据\n")
        health.review_documents(self.root, self.manifest(documents=[{"path": "旧说明.md", "dependencies": ["support.md"]}]))
        self.assertFalse((self.root / health.CATALOG).exists())
        manifest = self.write_json(".ai-memory/分类.json", {"schema_version": 1, "documents": [
            {"source": "旧说明.md", "target": "文档/设计/说明.md", "purpose": "说明", "role": "design",
             "state": "current", "reason": "已阅读", "uncertainty": None}]})
        documents.propose_organize(self.root, "first-move", manifest, "首次整理")
        documents.apply_organize(self.root, "first-move", True, "执行这批整理")
        health.review_documents(self.root, self.manifest("review2", [
            {"path": "文档/设计/说明.md", "dependencies": ["support.md"], "supersedes": ["review1"]}]))
        self.assertFalse(health.health(self.root)["needs_attention"])
        documents.rollback_review(self.root, "first-move", True, "恢复整理前目录")
        self.assertFalse((self.root / health.CATALOG).exists())
        result = health.health(self.root)
        self.assertEqual(result["changes"][0]["code"], "review_document_changed")
        self.assertEqual(result["changes"][0]["path"], "旧说明.md")
        self.assertEqual(result["conflicts"], [])
        seal = self.root / ".ai-memory/review-seals/first-move.json"
        before_seal = seal.read_bytes()
        altered = json.loads(before_seal)
        altered["proposal_sha256"] = "0" * 64
        seal.write_text(json.dumps(altered), encoding="utf-8")
        tampered = health.health(self.root)
        self.assertEqual(tampered["errors"][0]["code"], "unreadable_evidence")
        seal.write_bytes(before_seal)
        health.review_documents(self.root, self.manifest("review3", [
            {"path": "旧说明.md", "dependencies": ["support.md"], "supersedes": ["review2"]}]))
        self.assertFalse(health.health(self.root)["needs_attention"])

    def test_content_review_clears_staleness_without_mutating_classification(self):
        scope = self.task()
        self.write("文档/说明.md", "old text")
        self.write("src/app.py", "value = 1")
        catalog = self.write_json(health.CATALOG, {"schema_version": 1, "project_id": scope["project_id"], "documents": [
            {"source": "文档/说明.md", "sha256": hashlib.sha256((self.root / "文档/说明.md").read_bytes()).hexdigest()}]})
        health.review_documents(self.root, self.manifest())
        self.write("文档/说明.md", "new text reflecting implementation")
        self.assertEqual({item["code"] for item in health.health(self.root)["changes"]},
                         {"catalog_document_changed", "review_document_changed"})
        before = catalog.read_bytes()
        health.review_documents(self.root, self.manifest("review2", [
            {"path": "文档/说明.md", "dependencies": ["src/app.py"], "supersedes": ["review1"]}]))
        result = health.health(self.root)
        self.assertFalse(result["needs_attention"])
        self.assertEqual(result["changes"], [])
        self.assertIn("classification_review_pending", {item["code"] for item in result["warnings"]})
        self.assertEqual(catalog.read_bytes(), before)
        self.write("src/app.py", "value = 2")
        self.assertEqual([item["code"] for item in health.health(self.root)["changes"]], ["dependency_changed"])

    def test_thousands_of_findings_fit_budget_without_losing_counts(self):
        report = {"changes": [{"path": f"文档/{number}.md", "code": "dependency_changed"} for number in range(6000)],
                  "conflicts": [{"code": "parallel_review_heads"}], "errors": [{"code": "invalid_review"}],
                  "warnings": [{"code": "review_without_dependencies"}]}
        result = health._bounded(report, 2048)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")), 2048)
        self.assertEqual(result["total_counts"]["changes"], 6000)
        self.assertEqual(result["omitted_counts"]["changes"] + len(result["changes"]), 6000)
        self.assertTrue(result["needs_attention"])
        self.assertEqual(result["errors"], [{"code": "invalid_review"}])
        self.assertEqual(result["conflicts"], [{"code": "parallel_review_heads"}])


if __name__ == "__main__":
    unittest.main()
