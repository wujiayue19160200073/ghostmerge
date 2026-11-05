import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ghostmerge import blame, demo  # noqa: E402

PYTHON_FAILURE = """\
ERROR: tests.test_reports (unittest.loader._FailedTest.tests.test_reports)
----------------------------------------------------------------------
ImportError: Failed to import test module: tests.test_reports
Traceback (most recent call last):
  File "tests/test_reports.py", line 3, in <module>
    from reports import build_report
  File "reports.py", line 1, in <module>
    from users import get_user
ImportError: cannot import name 'get_user' from 'users'
"""

TS_FAILURE = """\
src/reports.ts:1:10 - error TS2305: Module '"./users"' has no exported member 'getUser'.
"""

GO_FAILURE = """\
./reports.go:12:2: undefined: GetUser
"""

JS_FAILURE = """\
TypeError: getDefault is not a function
    at buildReport (reports.js:4:12)
"""


class SymbolExtractionTests(unittest.TestCase):
    def test_python_import_error(self):
        names = [s.name for s in blame.extract_symbols(PYTHON_FAILURE)]
        self.assertIn("get_user", names)

    def test_typescript_missing_export(self):
        names = [s.name for s in blame.extract_symbols(TS_FAILURE)]
        self.assertIn("getUser", names)

    def test_go_undefined(self):
        names = [s.name for s in blame.extract_symbols(GO_FAILURE)]
        self.assertIn("GetUser", names)

    def test_javascript_missing_function(self):
        names = [s.name for s in blame.extract_symbols(JS_FAILURE)]
        self.assertIn("getDefault", names)

    def test_plain_assertion_failure_yields_nothing_to_blame(self):
        # An AssertionError names no symbol. Inventing one here would produce
        # confident, wrong attributions.
        names = blame.extract_symbols("AssertionError: 3 != 4")
        self.assertEqual(names, [])


class AttributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = demo.build_demo_repo()

    def test_names_the_branch_that_deleted_the_symbol(self):
        clues = blame.attribute(
            PYTHON_FAILURE,
            ["agent/auth-refactor", "agent/reports"],
            "main",
            cwd=self.repo.path,
        )
        self.assertTrue(clues)
        top = clues[0]
        self.assertEqual(top.symbol, "get_user")
        self.assertIsNotNone(top.cause)
        self.assertEqual(top.cause.branch, "agent/auth-refactor")
        self.assertIn("users.py", top.cause.file)

    def test_identifies_the_branch_still_calling_it(self):
        clues = blame.attribute(
            PYTHON_FAILURE,
            ["agent/auth-refactor", "agent/reports"],
            "main",
            cwd=self.repo.path,
        )
        top = clues[0]
        self.assertEqual([c.branch for c in top.callers], ["agent/reports"])
        self.assertEqual(top.confidence, "high")

    def test_guesses_the_new_name_after_a_rename(self):
        clues = blame.attribute(
            PYTHON_FAILURE,
            ["agent/auth-refactor", "agent/reports"],
            "main",
            cwd=self.repo.path,
        )
        self.assertEqual(clues[0].renamed_to, "fetch_user")

    def test_unexplainable_failure_returns_no_confident_clue(self):
        clues = blame.attribute("AssertionError: 3 != 4", ["agent/reports"], "main", cwd=self.repo.path)
        self.assertEqual(clues, [])


class DeletionWeightTests(unittest.TestCase):
    def test_declaration_outranks_usage(self):
        declaration = blame._deletion_weight("def get_user(user_id):")
        usage = blame._deletion_weight("    user = get_user(user_id)")
        self.assertGreater(declaration, usage)


if __name__ == "__main__":
    unittest.main()
