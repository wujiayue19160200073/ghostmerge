import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ghostmerge import report  # noqa: E402

NOISY = """\
....F
======================================================================
ERROR: test_x (unittest.loader._FailedTest.test_x)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_x
Traceback (most recent call last):
  File "C:\\Users\\me\\AppData\\Local\\Temp\\ghostmerge-ab12cd\\tests\\test_x.py", line 3, in <module>
    ~~~~~~~~~~^^^^^^
    from reports import build_report
  File "C:\\Users\\me\\AppData\\Local\\Temp\\ghostmerge-ab12cd\\reports.py", line 1, in <module>
    from users import get_user
ImportError: cannot import name 'get_user' from 'users'
----------------------------------------------------------------------
Ran 1 test in 0.001s
FAILED (errors=1)
"""


class ExcerptTests(unittest.TestCase):
    def test_shows_the_root_cause_line(self):
        rows = report._failure_excerpt(NOISY)
        self.assertTrue(any("cannot import name 'get_user'" in row for row in rows), rows)

    def test_scrubs_temp_worktree_paths(self):
        rows = report._failure_excerpt(NOISY)
        joined = "\n".join(rows)
        self.assertNotIn("ghostmerge-ab12cd", joined)

    def test_strips_traceback_decoration(self):
        rows = report._failure_excerpt(NOISY)
        self.assertFalse(rows[0].strip().startswith("~"), rows)
        self.assertNotIn("----", rows[-1])

    def test_empty_output_is_safe(self):
        self.assertEqual(report._failure_excerpt(""), [])
        self.assertEqual(report._failure_excerpt("   \n\n"), [])


class ScrubTests(unittest.TestCase):
    def test_both_path_styles(self):
        self.assertEqual(report._scrub("  /tmp/ghostmerge-x9/a.py"), "  a.py")
        self.assertEqual(report._scrub(r"  C:\Temp\ghostmerge-x9\a.py"), "  a.py")


class StatusTests(unittest.TestCase):
    def test_counts_are_per_status(self):
        from ghostmerge.trial import Trial
        trials = [
            Trial("a", "b", "ghost"),
            Trial("a", "c", "pass"),
            Trial("b", "c", "pass"),
        ]
        tally = report.counts(trials)
        self.assertEqual(tally["ghost"], 1)
        self.assertEqual(tally["pass"], 2)


if __name__ == "__main__":
    unittest.main()
