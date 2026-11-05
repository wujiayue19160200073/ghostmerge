import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ghostmerge import glyphs, report  # noqa: E402
from ghostmerge.trial import Plan, Trial  # noqa: E402

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


class EncodingFallbackTests(unittest.TestCase):
    """The report must not die on a console that cannot encode its symbols.

    Windows hands every redirected stdout cp1252 — a pipe, a CI job, `> out.txt` —
    and cp1252 has no ✓, ✗, │ or ─. Writing one raises UnicodeEncodeError after
    the tests have already run, so the tool does all the work and then crashes
    while reporting it. `LC_ALL=C` on Linux is the same story for · and —.

    CI found this on windows-latest; this is the cheaper local version.
    """

    def _render(self, encoding: str) -> str:
        plan = Plan(base="main", branches=["a", "b"], pairs=[("a", "b")])
        trials = [Trial("a", "b", "ghost", output="ImportError: cannot import name 'x'")]
        sink = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict", newline="")
        try:
            with contextlib.redirect_stdout(sink):
                glyphs.reset()
                return report.render_terminal(plan, trials, {})
        finally:
            glyphs.reset()

    def test_ascii_console_never_gets_a_character_it_cannot_encode(self):
        text = self._render("ascii")
        text.encode("ascii")  # the assertion that used to fail on the real bug

    def test_cp1252_console_never_gets_an_unencodable_character(self):
        text = self._render("cp1252")
        text.encode("cp1252")

    def test_utf8_console_keeps_the_nicer_symbols(self):
        text = self._render("utf-8")
        self.assertIn(glyphs._UNICODE["cross"], text)

    def test_row_markers_stay_one_column_wide(self):
        # These start an aligned row. Widening them breaks the table; the
        # ellipsis is mid-sentence and deliberately exempt.
        for name in ("tick", "cross", "bar", "rule", "bang", "dot"):
            self.assertEqual(
                len(glyphs._UNICODE[name]), len(glyphs._ASCII[name]),
                f"{name} changes width when it degrades to ASCII",
            )


class MarkdownTests(unittest.TestCase):
    def test_markdown_is_a_document_not_a_log(self):
        plan = Plan(base="main", branches=["a", "b", "c"], pairs=[("a", "b")])
        trials = [Trial("a", "b", "pass"), Trial("a", "c", "conflict")]
        body = report.render_markdown(plan, trials, {})
        self.assertTrue(body.startswith("## ghostmerge:"), body[:60])
        self.assertNotIn("\r", body)


if __name__ == "__main__":
    unittest.main()
