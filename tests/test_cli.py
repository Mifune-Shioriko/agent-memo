import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_memo.cli import main



class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "global" / "projects.json"
        self.run_cli("init")

    def run_cli(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(["--root", str(self.root), "--registry", str(self.registry), *args])
        self.assertEqual(result, code, err.getvalue())
        return out.getvalue()

    def write(self, name, text):
        path = self.root / ".memory" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_init_preserves_notes_and_no_activation(self):
        self.write("summary.md", "# Actual work\n")
        self.run_cli("init")
        self.assertIn("Actual work", self.run_cli("context"))
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".memory" / "enabled").exists())

    def test_search_refreshes_edits_deletions_and_has_source_lines(self):
        self.write("topic.md", "# Intro\n\n## Auth\nUse rotating tokens.\n")
        result = self.run_cli("search", "rotating tokens")
        self.assertIn(".memory/topic.md:3", result)
        self.write("topic.md", "# Auth\nUse passkeys instead.\n")
        self.assertIn("No matching", self.run_cli("search", "rotating"))
        self.assertIn("passkeys", self.run_cli("search", "passkeys"))
        (self.root / ".memory/topic.md").unlink()
        self.assertIn("No matching", self.run_cli("search", "passkeys"))

    def test_chinese_and_literal_queries(self):
        self.write("decisions.md", "# 数据库\n项目认证采用短期令牌，不记录密码。\n")
        self.assertIn("短期令牌", self.run_cli("search", "认证"))
        self.assertIn("短期令牌", self.run_cli("search", "认证 令牌"))
        self.run_cli("search", '" OR * : - (')

    def test_rebuild_after_cache_removed(self):
        self.write("lessons.md", "# Cache\nRebuildable evidence.\n")
        self.run_cli("context")
        (self.root / ".memory/index.sqlite").unlink()
        self.assertIn("evidence", self.run_cli("search", "evidence"))
        self.run_cli("reindex")
        self.assertIn("evidence", self.run_cli("search", "evidence"))

    def test_check_and_bounded_output(self):
        self.write("summary.md", "x\n" * 101)
        self.assertIn("summary.md", self.run_cli("check", code=1))
        self.assertLessEqual(len(self.run_cli("context", "--max-chars", "30").rstrip("\n")), 30)
        self.assertLessEqual(len(self.run_cli("search", "x", "--max-chars", "8").rstrip("\n")), 8)

    def test_no_parent_project_discovery(self):
        child = self.root / "child"
        child.mkdir()
        self.root = child
        self.run_cli("context", code=2)
        self.assertFalse((child / ".memory").exists())

    def test_symlinks_not_read_or_written(self):
        outside = self.root / "outside.md"
        outside.write_text("secretvalue", encoding="utf-8")
        (self.root / ".memory/link.md").symlink_to(outside)
        self.assertIn("No matching", self.run_cli("search", "secretvalue"))
        (self.root / ".memory/index.sqlite").unlink()
        (self.root / ".memory/index.sqlite").symlink_to(outside)
        self.run_cli("context", code=2)
        self.assertEqual(outside.read_text(), "secretvalue")

    def test_skill_install_contains_explicit_only_policy(self):
        destination = self.root / "skills"
        self.run_cli("install-skill", "--dest", str(destination))
        skill = destination / "agent-memo"
        self.assertIn("allow_implicit_invocation: false", (skill / "agents/openai.yaml").read_text())
        self.assertTrue((skill / "SKILL.md").is_file())
        self.run_cli("install-skill", "--dest", str(destination), code=2)

    def test_fenced_headings_do_not_split_sections(self):
        self.write("topic.md", "# Shell\n```bash\n# comment\necho marker\n```\n")
        self.assertIn("topic.md:1", self.run_cli("search", "marker"))

    def test_index_is_fts5(self):
        self.run_cli("context")
        with sqlite3.connect(self.root / ".memory/index.sqlite") as db:
            self.assertIn("fts5", db.execute("SELECT sql FROM sqlite_master WHERE name='chunks'").fetchone()[0])

    def test_registry_contains_only_locations_and_deduplicates_roots(self):
        self.write("summary.md", "Private project details")
        self.run_cli("context")
        self.run_cli("check")
        self.run_cli("init")
        data = json.loads(self.registry.read_text())
        self.assertEqual(data, {"version": 1, "projects": [
            {"root": str(self.root), "memory_dir": str(self.root / ".memory")}]})
        self.root = self.root / ".." / self.root.name
        self.run_cli("context")
        self.assertEqual(json.loads(self.registry.read_text()), data)

    def test_existing_memory_registers_on_use(self):
        self.registry.unlink()
        self.run_cli("context")
        self.assertEqual(json.loads(self.registry.read_text())["projects"][0]["root"], str(self.root))
        self.registry.unlink()
        self.root = self.root / "unrelated"
        self.run_cli("context", code=2)
        self.assertFalse(self.registry.exists())

    def test_list_missing_and_explicit_prune(self):
        directory = self.root / ".memory"
        directory.rename(self.root / "moved-memory")
        before = self.registry.read_bytes()
        result = json.loads(self.run_cli("projects", "--json"))
        self.assertEqual(result["projects"][0]["status"], "missing")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(directory.exists())
        self.assertEqual(json.loads(self.run_cli("projects", "--prune", "--json"))["projects"], [])

    def test_corrupt_and_future_registry_preserved(self):
        for content in ("broken json", '{"version": 2, "projects": []}',
                        '{"version": 1, "projects": [{}]}'):
            self.registry.write_text(content)
            self.run_cli("init", code=2)
            self.assertEqual(self.registry.read_text(), content)
            self.run_cli("projects", code=2)

    def test_registry_symlink_not_overwritten(self):
        outside = self.root / "outside.json"
        outside.write_text("keep")
        self.registry.unlink()
        self.registry.symlink_to(outside)
        self.run_cli("init", code=2)
        self.assertEqual(outside.read_text(), "keep")

    def test_concurrent_registration_keeps_every_project(self):
        processes = []
        for i in range(8):
            command = [sys.executable, "-m", "agent_memo", "--root", str(self.root / str(i)),
                       "--registry", str(self.registry), "init"]
            processes.append(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              env=dict(os.environ)))
        for process in processes:
            _, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, err.decode())
        self.assertEqual(len(json.loads(self.registry.read_text())["projects"]), 9)


    def test_empty_registry_list_does_not_create_files(self):
        self.registry = self.root / "unused" / "projects.json"
        self.assertEqual(json.loads(self.run_cli("projects", "--json"))["projects"], [])
        self.assertFalse(self.registry.parent.exists())

    def test_profile_isolation_and_registration(self):
        self.run_cli("--profile", "career", "init")
        career_dir = self.root / ".memory.career"
        self.assertTrue(career_dir.is_dir())
        (career_dir / "summary.md").write_text("# Career Goal\n", encoding="utf-8")
        self.assertIn("Career Goal", self.run_cli("--profile", "career", "context"))
        self.assertNotIn("Career Goal", self.run_cli("context"))

        projects = json.loads(self.run_cli("projects", "--json"))["projects"]
        self.assertEqual(len(projects), 2)
        self.assertEqual(projects[0]["root"], str(self.root))
        self.assertIsNone(projects[0].get("profile"))
        self.assertEqual(projects[1]["profile"], "career")

        # Invalid profile names
        self.run_cli("--profile", "../bad", "init", code=2)
        self.run_cli("--profile", "a/b", "init", code=2)

    def test_split_fork_and_extract(self):
        self.write("topic.md", "# Topic\nDetails about backend.\n")
        self.write("keep.md", "# Keep\nStays here.\n")
        self.run_cli("context")

        # Fork mode
        self.run_cli("split", "--to", "forked")
        self.assertTrue((self.root / ".memory.forked" / "topic.md").is_file())
        self.assertTrue((self.root / ".memory.forked" / "keep.md").is_file())
        self.assertIn("backend", self.run_cli("--profile", "forked", "search", "backend"))

        # Extract mode
        self.run_cli("split", "--to", "extracted", "--files", "topic.md")
        self.assertTrue((self.root / ".memory.extracted" / "topic.md").is_file())
        self.assertFalse((self.root / ".memory" / "topic.md").exists())
        self.assertTrue((self.root / ".memory" / "keep.md").exists())
        self.assertIn("backend", self.run_cli("--profile", "extracted", "search", "backend"))
        self.assertIn("No matching", self.run_cli("search", "backend"))

    def test_merge_profiles(self):
        self.run_cli("--profile", "sub", "init")
        sub_dir = self.root / ".memory.sub"
        (sub_dir / "summary.md").write_text("# Sub Goal\n", encoding="utf-8")
        (sub_dir / "decisions.md").write_text("# Decisions\n\n- Decided to use Rust.\n", encoding="utf-8")
        (sub_dir / "lessons.md").write_text("# Lessons\n\n- Profiling was essential.\n", encoding="utf-8")
        (sub_dir / "extra.md").write_text("# Extra\nAdditional note.\n", encoding="utf-8")

        self.run_cli("merge", "--from", "sub", "--to", "default", "--delete-source")
        self.assertFalse(sub_dir.exists())
        self.assertTrue((self.root / ".memory" / "summary-sub.md").is_file())
        self.assertIn("Sub Goal", (self.root / ".memory" / "summary-sub.md").read_text())
        self.assertIn("Decided to use Rust", (self.root / ".memory" / "decisions.md").read_text())
        self.assertIn("Profiling was essential", (self.root / ".memory" / "lessons.md").read_text())
        self.assertTrue((self.root / ".memory" / "extra.md").is_file())
        self.assertIn("Additional note", self.run_cli("search", "Additional"))

        # Self-merge rejection
        self.run_cli("merge", "--from", "default", "--to", "default", code=2)


if __name__ == "__main__":
    unittest.main()
