"""Real stdin/stdout contract tests for the optional, read-only hook adapters."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from memory_workflow import bind


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_hook.py"


class MemoryHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory_hook_")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "中文项目"
        self.root.mkdir()
        self.project_id = str(uuid.uuid4())

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def register(self, root=None, project_id=None):
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        self.write_json(root / ".ai-memory/project.json", {
            "schema_version": 1, "project_id": project_id or self.project_id,
            "created_at": "2026-10-15T00:00:00Z",
        })

    def scope(self, task="initial-build", root=None, **updates):
        root = root or self.root
        data = {
            "schema_version": 1, "project_id": self.project_id,
            "task_id": task, "revision": 1, "goal": "管理中文网址收藏和待办",
            "detail": "small", "modules": ["bookmarks", "todos"],
            "record_fields": ["changed", "pitfall", "fix", "verification", "next"],
            "plan": None, "mode": "task_then_auto",
            "long_term_auto": False,
            "confirmation": {"caller_confirmed": True, "summary": "仅当前任务", "recorded_at": "2026-10-15T00:00:00Z"},
            "updated_at": "2026-10-15T00:00:00Z",
        }
        data.update(updates)
        self.write_json(root / f".ai-memory/scopes/{task}.json", data)

    def run_hook(self, tool="codex", event="SessionStart", root=None, raw=None, args=(), **extra):
        payload = {"cwd": str(root or self.root), "hook_event_name": event, **extra}
        content = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--tool", tool, *args], input=content,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout.decode("utf-8")), result

    @staticmethod
    def context(response):
        return response.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_startup_resume_and_compact_restore_for_both_tools(self):
        self.register()
        self.scope(detail="standard", plan="docs/tasks/initial-build/plan.md")
        (self.root / "DEV.md").write_text("DO NOT LOAD BODY", encoding="utf-8")
        plan = self.root / "docs/tasks/initial-build/plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("<!-- ai-memory-task-status: active -->\nDO NOT LOAD PLAN BODY", encoding="utf-8")
        for tool in ("claude", "codex"):
            for source in ("startup", "resume", "compact"):
                with self.subTest(tool=tool, source=source):
                    response, result = self.run_hook(tool=tool, source=source)
                    self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "SessionStart")
                    context = self.context(response)
                    self.assertIn("管理中文网址收藏和待办", context)
                    self.assertIn("docs/tasks/initial-build/plan.md", context)
                    self.assertIn("DEV.md", context)
                    self.assertIn("仍需核对当前任务", context)
                    self.assertNotIn("DO NOT LOAD", context)
                    self.assertEqual(result.stderr, b"")

    def test_prompt_is_short_reminder_not_full_scope_or_chat(self):
        self.register()
        self.scope(goal="PRIVATE_GOAL_BODY")
        for tool in ("claude", "codex"):
            response, _ = self.run_hook(tool=tool, event="UserPromptSubmit", prompt="PRIVATE_PROMPT")
            text = self.context(response)
            self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
            for field in ("changed", "pitfall", "fix", "verification", "next"):
                self.assertIn(field, text)
            self.assertIn(".ai-memory/scopes/initial-build.json", text)
            self.assertNotIn("PRIVATE_GOAL_BODY", text)
            self.assertNotIn("PRIVATE_PROMPT", text)

    def test_compaction_output_uses_only_supported_fields(self):
        self.register()
        self.scope()
        for event in ("PreCompact", "PostCompact"):
            response, _ = self.run_hook(tool="claude", event=event)
            self.assertEqual(response, {})
            response, _ = self.run_hook(tool="codex", event=event)
            self.assertEqual(set(response), {"systemMessage"})
            self.assertNotIn("hookSpecificOutput", response)

    def test_stop_never_blocks_or_continues_and_reentry_is_quiet(self):
        self.register()
        self.scope()
        for tool in ("claude", "codex"):
            response, _ = self.run_hook(tool=tool, event="Stop", last_assistant_message="PRIVATE_ASSISTANT")
            self.assertEqual(set(response), {"systemMessage"})
            self.assertNotIn("PRIVATE_ASSISTANT", json.dumps(response))
            response, _ = self.run_hook(tool=tool, event="Stop", stop_hook_active=True)
            self.assertEqual(response, {})

    def test_unregistered_projects_are_silent_for_every_event(self):
        for tool in ("claude", "codex"):
            for event in ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop"):
                response, result = self.run_hook(tool=tool, event=event)
                self.assertEqual(response, {})
                self.assertEqual(result.stderr, b"")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_input_fails_open_without_echoing_input(self):
        for raw in (b'{PRIVATE_SECRET', b'[]', b'null', b'\xff', b'a' * (1024 * 1024 + 1)):
            response, result = self.run_hook(raw=raw)
            self.assertEqual(response, {})
            self.assertTrue(result.stderr)
            self.assertNotIn(b"PRIVATE_SECRET", result.stderr)
        response, result = self.run_hook(args=("--max-bytes", "1"))
        self.assertEqual(response, {})
        self.assertTrue(result.stderr)

    def test_unknown_event_is_quiet(self):
        self.register()
        response, result = self.run_hook(event="PostToolUse")
        self.assertEqual(response, {})
        self.assertEqual(result.stderr, b"")

    def test_oversized_registration_fails_open(self):
        self.register()
        marker = self.root / ".ai-memory/project.json"
        marker.write_text('{"PRIVATE_METADATA": "' + "x" * (33 * 1024) + '"}', encoding="utf-8")
        response, result = self.run_hook()
        self.assertEqual(response, {})
        self.assertTrue(result.stderr)
        self.assertNotIn(b"PRIVATE_METADATA", result.stdout + result.stderr)

    def test_finds_nearest_project_from_child_directory(self):
        self.register()
        self.scope(goal="PARENT_PROJECT_DATA")
        nested = self.root / "packages/nested"
        self.register(nested, str(uuid.uuid4()))
        child = nested / "src"
        child.mkdir()
        response, _ = self.run_hook(root=child)
        text = self.context(response)
        self.assertIn(str(nested).replace("\\", "\\\\"), text)
        self.assertNotIn("PARENT_PROJECT_DATA", text)

    def test_git_directory_and_worktree_file_stop_parent_discovery(self):
        self.register()
        self.scope(goal="OUTSIDE_REPOSITORY")
        for kind in ("directory", "file"):
            nested = self.root / kind
            nested.mkdir()
            git = nested / ".git"
            git.mkdir() if kind == "directory" else git.write_text("gitdir: elsewhere", encoding="utf-8")
            response, result = self.run_hook(root=nested)
            self.assertEqual(response, {})
            self.assertEqual(result.stderr, b"")

    def test_malformed_nearest_registration_does_not_fall_back(self):
        self.register()
        self.scope(goal="PARENT_PRIVATE")
        nested = self.root / "nested"
        (nested / ".ai-memory").mkdir(parents=True)
        (nested / ".ai-memory/project.json").write_text("broken", encoding="utf-8")
        response, result = self.run_hook(root=nested)
        self.assertEqual(response, {})
        self.assertNotIn(b"PARENT_PRIVATE", result.stdout + result.stderr)
        self.assertTrue(result.stderr)
        for version in (True, 1.0):
            self.write_json(nested / ".ai-memory/project.json", {
                "schema_version": version, "project_id": self.project_id,
                "created_at": "2026-10-15T00:00:00Z",
            })
            with self.subTest(version=version):
                response, result = self.run_hook(root=nested)
                self.assertEqual(response, {})
                self.assertTrue(result.stderr)
                self.assertNotIn(b"PARENT_PRIVATE", result.stdout + result.stderr)

    def test_scope_identity_confirmation_and_history_are_filtered(self):
        self.register()
        self.scope()
        self.scope(task="foreign", project_id=str(uuid.uuid4()), goal="FOREIGN_SECRET")
        self.scope(task="unconfirmed", confirmation={"caller_confirmed": False}, goal="UNCONFIRMED_SECRET")
        self.scope(task="truthy", confirmation={"caller_confirmed": "true"}, goal="TRUTHY_SECRET")
        self.write_json(self.root / ".ai-memory/scopes/history/private.json", {"goal": "HISTORY_SECRET"})
        response, _ = self.run_hook()
        text = self.context(response)
        self.assertIn("范围扫描受限", text)
        self.assertNotIn("管理中文网址收藏和待办", text)
        for forbidden in ("FOREIGN_SECRET", "UNCONFIRMED_SECRET", "TRUTHY_SECRET", "HISTORY_SECRET"):
            self.assertNotIn(forbidden, text)
        response, _ = self.run_hook(args=("--task", "initial-build"))
        self.assertIn("管理中文网址收藏和待办", self.context(response))

    def test_multiple_scopes_offer_entries_without_selecting_a_task(self):
        self.register()
        self.scope(task="a-old-task", goal="OLD_TASK_GOAL")
        self.scope(task="z-current-task", goal="CURRENT_TASK_GOAL")
        for tool in ("claude", "codex"):
            for event in ("SessionStart", "UserPromptSubmit"):
                response, _ = self.run_hook(tool=tool, event=event)
                text = self.context(response)
                self.assertIn("存在多个已登记范围", text)
                self.assertIn("resume --task <TASK_ID>", text)
                self.assertNotIn("OLD_TASK_GOAL", text)
                self.assertNotIn("CURRENT_TASK_GOAL", text)
                self.assertNotIn("a-old-task.json", text)
                self.assertNotIn("z-current-task.json", text)

    def test_explicit_task_restores_only_requested_scope_for_both_tools(self):
        self.register()
        self.scope(task="a-old-task", goal="OLD_TASK_GOAL")
        self.scope(task="z-current-task", goal="CURRENT_TASK_GOAL")
        for tool in ("claude", "codex"):
            response, _ = self.run_hook(tool=tool, args=("--task", "z-current-task"))
            text = self.context(response)
            self.assertIn("CURRENT_TASK_GOAL", text)
            self.assertIn("z-current-task.json", text)
            self.assertIn("仍需核对当前任务", text)
            self.assertNotIn("OLD_TASK_GOAL", text)
            self.assertNotIn("a-old-task.json", text)

    def test_explicit_task_bypasses_scan_limit(self):
        self.register()
        for index in range(128):
            self.scope(task=f"old-{index:03}", goal="OLD_TASK_GOAL")
        self.scope(task="z-current", goal="DIRECT_SCOPE_GOAL")
        response, result = self.run_hook(args=("--task", "z-current"))
        self.assertIn("DIRECT_SCOPE_GOAL", self.context(response))
        self.assertNotIn("OLD_TASK_GOAL", self.context(response))
        self.assertEqual(result.stderr, b"")
        response, result = self.run_hook()
        self.assertIn("范围扫描受限", self.context(response))
        self.assertIn("resume --task <TASK_ID>", self.context(response))
        self.assertNotIn("DIRECT_SCOPE_GOAL", self.context(response))
        self.assertNotIn("OLD_TASK_GOAL", self.context(response))
        self.assertTrue(result.stderr)

    def test_incomplete_scan_does_not_treat_one_valid_scope_as_unique(self):
        self.register()
        self.scope(goal="NOT_PROVEN_UNIQUE")
        directory = self.root / ".ai-memory/scopes"
        for index in range(128):
            (directory / f"unrelated-{index:03}.txt").write_text("", encoding="utf-8")
        response, _ = self.run_hook()
        self.assertIn("范围扫描受限", self.context(response))
        self.assertNotIn("NOT_PROVEN_UNIQUE", self.context(response))

    def test_unavailable_selected_task_never_falls_back(self):
        self.register()
        self.scope(goal="OTHER_TASK_GOAL")
        self.scope(task="foreign", project_id=str(uuid.uuid4()), goal="FOREIGN_GOAL")
        self.scope(task="unconfirmed", confirmation={"caller_confirmed": False}, goal="UNCONFIRMED_GOAL")
        self.scope(task="mismatch", task_id="different", goal="MISMATCH_GOAL")
        (self.root / ".ai-memory/scopes/broken.json").write_text("broken", encoding="utf-8")
        for task in ("missing", "../initial-build", "", "foreign", "unconfirmed", "mismatch", "broken"):
            with self.subTest(task=task):
                response, result = self.run_hook(args=("--task", task))
                text = self.context(response)
                self.assertIn("指定任务范围不可用", text)
                self.assertIn("未选择其他任务", text)
                self.assertIn("resume --task <TASK_ID>", text)
                self.assertIn(".ai-memory/project.json", text)
                self.assertNotIn("_GOAL", text)
                self.assertTrue(result.stderr)

    def test_explicit_task_without_scope_directory_still_provides_entries(self):
        self.register()
        response, _ = self.run_hook(args=("--task", "missing"))
        self.assertIn("指定任务范围不可用", self.context(response))
        self.assertIn(".ai-memory/project.json", self.context(response))

    def test_scope_path_traversal_and_nested_projects_are_not_exposed(self):
        self.register()
        outside = self.base / "outside-secret.md"
        outside.write_text("SECRET_BODY", encoding="utf-8")
        self.scope(plan="../outside-secret.md")
        response, _ = self.run_hook()
        self.assertNotIn("outside-secret.md", self.context(response))
        nested = self.root / "other"
        self.register(nested, str(uuid.uuid4()))
        (nested / "plan.md").write_text("SECRET_BODY", encoding="utf-8")
        self.scope(plan="other/plan.md")
        response, _ = self.run_hook()
        self.assertNotIn("other/plan.md", self.context(response))

    def test_does_not_read_transcripts_or_journal_bodies_and_does_not_write(self):
        self.register()
        self.scope()
        private = self.base / "transcript.txt"
        private.write_text("TRANSCRIPT_SECRET", encoding="utf-8")
        journal = self.root / "docs/memory/journal/2026-10-15/event.md"
        journal.parent.mkdir(parents=True)
        journal.write_text("JOURNAL_SECRET", encoding="utf-8")
        before = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        response, result = self.run_hook(transcript_path=str(private), last_assistant_message="MESSAGE_SECRET")
        after = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        for value in (b"TRANSCRIPT_SECRET", b"JOURNAL_SECRET", b"MESSAGE_SECRET"):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_entire_json_output_obeys_utf8_byte_budget(self):
        self.register()
        for index in range(60):
            self.scope(task=f"task-{index:02}", goal="多字节内容" * 800)
        for tool in ("claude", "codex"):
            for event in ("SessionStart", "UserPromptSubmit", "Stop"):
                for budget in (768, 1500, 4096):
                    with self.subTest(tool=tool, event=event, budget=budget):
                        response, result = self.run_hook(tool=tool, event=event, args=("--max-bytes", str(budget), "--task", "task-59"))
                        effective_budget = budget if event == "SessionStart" else min(budget, 1536)
                        self.assertLessEqual(len(result.stdout), effective_budget)
                        self.assertTrue(response)

    def test_minimum_budget_truncates_an_oversized_session_context(self):
        self.register()
        self.scope(goal="多字节内容" * 800)
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                # First establish that this context exceeds the minimum budget.
                # Short Stop reminders can fit at 768 bytes on POSIX paths.
                _, full = self.run_hook(tool=tool, args=("--max-bytes", "8192"))
                self.assertGreater(len(full.stdout), 768)
                response, bounded = self.run_hook(tool=tool, args=("--max-bytes", "768"))
                self.assertLessEqual(len(bounded.stdout), 768)
                self.assertIn("输出受限", self.context(response))

    def symlink(self, target, link, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_linked_project_metadata_is_rejected(self):
        outside = self.base / "outside"
        self.register(outside)
        self.scope(root=outside, goal="EXTERNAL_METADATA")
        self.symlink(outside / ".ai-memory", self.root / ".ai-memory", directory=True)
        response, result = self.run_hook()
        self.assertEqual(response, {})
        self.assertNotIn(b"EXTERNAL_METADATA", result.stdout + result.stderr)
        self.assertTrue(result.stderr)

    def test_linked_cwd_does_not_follow_another_project(self):
        outside = self.base / "outside"
        self.register(outside)
        link = self.root / "alias"
        self.symlink(outside, link, directory=True)
        response, result = self.run_hook(root=link)
        self.assertEqual(response, {})
        self.assertTrue(result.stderr)

    def test_linked_scope_is_not_read(self):
        self.register()
        directory = self.root / ".ai-memory/scopes"
        directory.mkdir()
        outside = self.base / "outside"
        self.register(outside)
        self.scope(root=outside, goal="LINKED_SCOPE_SECRET")
        self.symlink(outside / ".ai-memory/scopes/initial-build.json", directory / "initial-build.json")
        response, _ = self.run_hook()
        self.assertNotIn("LINKED_SCOPE_SECRET", self.context(response))
        self.assertIn("范围扫描受限", self.context(response))
        response, _ = self.run_hook(args=("--task", "initial-build"))
        self.assertNotIn("LINKED_SCOPE_SECRET", self.context(response))
        self.assertIn("指定任务范围不可用", self.context(response))

    def test_session_binding_restores_exact_task_and_displays_real_session_id(self):
        self.register()
        self.scope(task="first", goal="FIRST_GOAL")
        self.scope(task="second", goal="SECOND_GOAL")
        bind(self.root, "first", "codex", "session-a")
        bind(self.root, "second", "codex", "session-b")
        response, _ = self.run_hook(session_id="session-a")
        context = self.context(response)
        self.assertIn("FIRST_GOAL", context)
        self.assertNotIn("SECOND_GOAL", context)
        self.assertIn('session_id="session-a"', context)
        self.assertIn("bind --root", context)
        response, _ = self.run_hook(session_id="session-b")
        self.assertIn("SECOND_GOAL", self.context(response))
        response, _ = self.run_hook(session_id="session-a", args=("--task", "second"))
        self.assertIn("SECOND_GOAL", self.context(response))

    def test_invalid_binding_does_not_fall_back_to_unique_active_task(self):
        self.register()
        self.scope(task="old", goal="OLD_GOAL")
        self.scope(task="current", goal="CURRENT_GOAL")
        bind(self.root, "old", "claude", "session")
        (self.root / ".ai-memory/scopes/old.json").unlink()
        response, _ = self.run_hook(tool="claude", session_id="session")
        self.assertIn("当前会话绑定无效", self.context(response))
        self.assertNotIn("CURRENT_GOAL", self.context(response))

    def test_active_selection_and_custom_layout_are_used(self):
        self.register()
        self.scope(task="done", task_status="completed", goal="DONE_GOAL")
        self.scope(task="current", goal="CURRENT_GOAL")
        self.write_json(self.root / ".ai-memory/layout.json", {"schema_version": 1, "document_root": "文档",
            "index": "文档/索引.md", "roles": {"status": "文档/当前状态.md"}})
        (self.root / "文档").mkdir()
        (self.root / "文档/索引.md").write_text("PRIVATE_INDEX_BODY", encoding="utf-8")
        (self.root / "文档/当前状态.md").write_text("PRIVATE_STATUS_BODY", encoding="utf-8")
        response, _ = self.run_hook()
        context = self.context(response)
        self.assertIn("CURRENT_GOAL", context)
        self.assertNotIn("DONE_GOAL", context)
        self.assertIn("文档/当前状态.md", context)
        self.assertIn("文档/索引.md", context)
        self.assertNotIn("PRIVATE_", context)
        self.assertIn("未提供可靠 session_id", context)

    def test_unknown_plan_state_prevents_automatic_selection(self):
        self.register()
        self.scope(plan="missing-plan.md", goal="UNKNOWN_GOAL")
        response, _ = self.run_hook()
        self.assertIn("状态未知", self.context(response))
        self.assertNotIn("UNKNOWN_GOAL", self.context(response))
        response, _ = self.run_hook(args=("--task", "initial-build"))
        self.assertIn("UNKNOWN_GOAL", self.context(response))

    def test_oversized_selected_scope_has_a_readable_entrypoint(self):
        self.register()
        self.scope(goal="x" * (32 * 1024))
        response, _ = self.run_hook(args=("--task", "initial-build"))
        self.assertIn(".ai-memory/scopes/initial-build.json", self.context(response))
        self.assertIn("32 KiB", self.context(response))


if __name__ == "__main__":
    unittest.main()
