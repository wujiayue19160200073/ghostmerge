import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ghostmerge import demo, gitcmd  # noqa: E402
from ghostmerge import trial as trial_mod  # noqa: E402


def command() -> str:
    return f'"{sys.executable}" -m unittest discover -s tests -t . -q'


class TrialMergeTests(unittest.TestCase):
    """The end-to-end claim: a clean merge that fails its tests is reported as a ghost."""

    @classmethod
    def setUpClass(cls):
        cls.repo = demo.build_demo_repo()
        cls.root = cls.repo.path
        cls.plan = trial_mod.build_plan("main", cls.repo.branches, cwd=cls.root)
        cls.trials = trial_mod.run_plan(
            cls.plan, test_command=command(), cwd=cls.root, timeout=120, jobs=1
        )
        cls.by_pair = {(t.a, t.b): t for t in cls.trials}

    def test_finds_exactly_one_ghost(self):
        ghosts = [t for t in self.trials if t.is_ghost]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual({ghosts[0].a, ghosts[0].b}, {"agent/auth-refactor", "agent/reports"})

    def test_the_ghost_merge_is_textually_clean(self):
        ghost = next(t for t in self.trials if t.is_ghost)
        self.assertIsNotNone(ghost.probe)
        self.assertTrue(ghost.probe.clean)
        self.assertEqual(ghost.probe.conflicts, [])

    def test_unrelated_pairs_pass(self):
        passing = {frozenset((t.a, t.b)) for t in self.trials if t.status == trial_mod.PASS}
        self.assertIn(frozenset({"agent/auth-refactor", "agent/pagination"}), passing)
        self.assertIn(frozenset({"agent/pagination", "agent/reports"}), passing)

    def test_failure_output_names_the_missing_symbol(self):
        ghost = next(t for t in self.trials if t.is_ghost)
        self.assertIn("get_user", ghost.output)

    def test_no_temp_worktree_is_left_behind(self):
        # The most expensive bug this tool could have: crash, leave a worktree
        # registered, and the next run starts on a dirty registry.
        worktrees = gitcmd.list_worktrees(cwd=self.root)
        self.assertEqual(len(worktrees), 1, worktrees)

    def test_trial_worktrees_lived_outside_the_repo(self):
        # A worktree inside the repo gets picked up by test runners, linters and
        # `git status`, and the user's own tooling starts reporting on our
        # scratch directory. The original version of this test walked
        # `git worktree list` *after* cleanup, when only the main worktree was
        # left, and compared raw strings — so it passed for the wrong reason on
        # macOS (/tmp is a symlink to /private/tmp) and Windows (git prints
        # forward slashes), and failed on Linux, where the strings match exactly.
        # Each Trial now records the directory it used, so this asserts the real
        # thing instead of inferring it from what survived.
        root = os.path.realpath(self.root)
        checked = 0
        for item in self.trials:
            if not item.worktree:
                continue
            checked += 1
            resolved = os.path.realpath(item.worktree)
            self.assertNotEqual(resolved, root, "a trial reused the repo itself")
            self.assertFalse(
                resolved.startswith(root + os.sep),
                f"a trial worktree lived inside the repo: {item.worktree}",
            )
        self.assertGreaterEqual(checked, 1, "no trial recorded a worktree path")

    def test_plan_skips_nothing_when_branches_diverged(self):
        self.assertEqual(len(self.plan.pairs), 3)
        self.assertEqual(self.plan.skipped, [])


class ConflictTests(unittest.TestCase):
    """A textual conflict must not be reported as a ghost: git already says it."""

    def test_textual_conflict_is_not_a_ghost(self):
        repo = demo.build_demo_repo()
        root = repo.path
        path = os.path.join(root, "shared.txt")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("base\n")
        gitcmd.run(["add", "-A"], cwd=root)
        gitcmd.run(["commit", "-q", "-m", "shared"], cwd=root)

        for branch, text in (("x", "one\n"), ("y", "two\n")):
            gitcmd.run(["checkout", "-q", "-b", branch, "main"], cwd=root)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            gitcmd.run(["add", "-A"], cwd=root)
            gitcmd.run(["commit", "-q", "-m", branch], cwd=root)
        gitcmd.run(["checkout", "-q", "main"], cwd=root)

        result = trial_mod.run_trial("x", "y", test_command=command(), cwd=root, timeout=60)
        self.assertEqual(result.status, trial_mod.CONFLICT)
        self.assertIsNone(result.test_code)


class TimeoutTests(unittest.TestCase):
    def test_hanging_test_is_reported_as_timeout_not_ghost(self):
        repo = demo.build_demo_repo()
        result = trial_mod.run_trial(
            "agent/auth-refactor", "agent/pagination",
            test_command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
            cwd=repo.path,
            timeout=1.5,
        )
        self.assertEqual(result.status, trial_mod.TIMEOUT)
        self.assertIn("timed out", result.output)


class PlanTests(unittest.TestCase):
    def test_identical_and_ancestor_pairs_are_skipped(self):
        repo = demo.build_demo_repo()
        plan = trial_mod.build_plan(
            "main", ["main", "agent/pagination", "agent/pagination"], cwd=repo.path
        )
        self.assertEqual(plan.pairs, [])
        self.assertTrue(all(reason for _a, _b, reason in plan.skipped))


class CleanupTests(unittest.TestCase):
    """`clean` deletes directories. Its guards are the most important code here."""

    def test_never_removes_the_main_worktree_even_when_the_name_matches(self):
        # Regression: the demo repo lives at %TEMP%/ghostmerge-demo-xxxx, whose
        # name starts with "ghostmerge-". An earlier version treated that as
        # proof of ownership and deleted the repository it was standing in.
        repo = demo.build_demo_repo()
        self.assertIn("ghostmerge-demo-", repo.path)
        removed = trial_mod.cleanup_stale(cwd=repo.path)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.isdir(repo.path))
        self.assertEqual(len(gitcmd.list_worktrees(cwd=repo.path)), 1)

    def test_removes_a_genuine_stale_temp_worktree(self):
        import tempfile

        repo = demo.build_demo_repo()
        root = repo.path
        probe = gitcmd.probe_merge("main", "agent/pagination", cwd=root)
        commit = gitcmd.commit_tree(probe.tree, ["main", "agent/pagination"], "stale", cwd=root)
        path = tempfile.mkdtemp(prefix="ghostmerge-")
        os.rmdir(path)
        gitcmd.add_detached_worktree(path, commit, cwd=root)
        self.assertEqual(len(gitcmd.list_worktrees(cwd=root)), 2)

        removed = trial_mod.cleanup_stale(cwd=root)
        self.assertEqual(removed, 1)
        self.assertEqual(len(gitcmd.list_worktrees(cwd=root)), 1)


if __name__ == "__main__":
    unittest.main()
