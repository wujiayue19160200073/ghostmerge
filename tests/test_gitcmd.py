import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ghostmerge import demo, gitcmd  # noqa: E402


class GitPlumbingTests(unittest.TestCase):
    """The probe must answer without touching disk, and must be honest about conflicts."""

    @classmethod
    def setUpClass(cls):
        cls.repo = demo.build_demo_repo()
        cls.root = cls.repo.path

    def test_clean_merge_of_disjoint_branches(self):
        probe = gitcmd.probe_merge("main", "agent/pagination", cwd=self.root)
        self.assertTrue(probe.clean)
        self.assertTrue(probe.tree)
        self.assertEqual(probe.conflicts, [])

    def test_probe_does_not_touch_the_working_tree(self):
        before = gitcmd.run(["status", "--porcelain"], cwd=self.root).out
        head_before = gitcmd.rev_parse("HEAD", cwd=self.root)
        gitcmd.probe_merge("agent/auth-refactor", "agent/reports", cwd=self.root)
        after = gitcmd.run(["status", "--porcelain"], cwd=self.root).out
        self.assertEqual(before, after)
        self.assertEqual(head_before, gitcmd.rev_parse("HEAD", cwd=self.root))

    def test_reports_textual_conflict(self):
        # Two branches writing different content to the same line.
        path = os.path.join(self.root, "conflict.txt")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("base\n")
        gitcmd.run(["add", "-A"], cwd=self.root)
        gitcmd.run(["commit", "-q", "-m", "add conflict file"], cwd=self.root)

        for branch, text in (("clash-a", "a\n"), ("clash-b", "b\n")):
            gitcmd.run(["checkout", "-q", "-b", branch, "main"], cwd=self.root)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            gitcmd.run(["add", "-A"], cwd=self.root)
            gitcmd.run(["commit", "-q", "-m", f"{branch} edits"], cwd=self.root)

        gitcmd.run(["checkout", "-q", "main"], cwd=self.root)
        probe = gitcmd.probe_merge("clash-a", "clash-b", cwd=self.root)
        self.assertFalse(probe.clean)
        self.assertIn("conflict.txt", probe.conflicts)

    def test_changed_files_uses_three_dot_semantics(self):
        # `main...branch` must report only this branch's work. Two dots would
        # also list everything main gained after the split.
        files = gitcmd.changed_files("main", "agent/pagination", cwd=self.root)
        self.assertIn("paging.py", files)
        self.assertNotIn("users.py", files)

    def test_ancestor_detection(self):
        self.assertTrue(gitcmd.is_ancestor("main", "agent/pagination", cwd=self.root))
        self.assertFalse(gitcmd.is_ancestor("agent/pagination", "main", cwd=self.root))

    def test_refuses_old_git_versions(self):
        original = gitcmd.git_version
        gitcmd.git_version = lambda: (2, 30, 0)
        try:
            with self.assertRaises(gitcmd.GitError) as ctx:
                gitcmd.require_supported_git()
            self.assertIn("2.38", str(ctx.exception))
        finally:
            gitcmd.git_version = original

    def test_outside_a_repository_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(gitcmd.GitError):
                gitcmd.repo_root(cwd=empty)


if __name__ == "__main__":
    unittest.main()
