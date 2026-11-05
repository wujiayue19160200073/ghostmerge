"""Build a throwaway repository that demonstrates the bug.

`ghostmerge demo` exists because "try it on your own repo" is a big ask. The
user has to pick a base branch, guess at test commands, and hope their project
has two branches worth merging — and if anything goes wrong they blame the tool.

So the tool ships the failure it detects. Four small files, two branches, one
rename and one new caller. Both branches are green on their own. The merge is
textually clean. The merged code raises ImportError. That is the entire thesis
of the project in a repo you can delete afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

USERS = '''\
"""User lookup. Deliberately tiny."""

_USERS = {"u1": {"id": "u1", "email": "ada@example.com"}}


def get_user(user_id):
    return _USERS.get(user_id)
'''

INVOICES = '''\
from users import get_user


def create_invoice(user_id, amount):
    user = get_user(user_id)
    if user is None:
        raise ValueError("unknown user")
    return {"user": user["email"], "amount": amount}
'''

TEST_USERS = '''\
import unittest

from users import get_user


class TestUsers(unittest.TestCase):
    def test_finds_existing_user(self):
        self.assertEqual(get_user("u1")["email"], "ada@example.com")

    def test_missing_user_is_none(self):
        self.assertIsNone(get_user("nobody"))
'''

TEST_INVOICES = '''\
import unittest

from invoices import create_invoice


class TestInvoices(unittest.TestCase):
    def test_invoice_for_known_user(self):
        self.assertEqual(create_invoice("u1", 10)["amount"], 10)

    def test_unknown_user_raises(self):
        with self.assertRaises(ValueError):
            create_invoice("nobody", 10)
'''

# Branch A: a refactor. Renames the function and updates every caller *on its
# own branch*, which is why its tests pass. The rename is correct and complete
# as far as branch A can see.
AUTH_USERS = USERS.replace("def get_user(user_id):", "def fetch_user(user_id):")
AUTH_INVOICES = INVOICES.replace("from users import get_user", "from users import fetch_user") \
                         .replace("get_user(user_id)", "fetch_user(user_id)")
AUTH_TEST_USERS = TEST_USERS.replace("import get_user", "import fetch_user") \
                            .replace("get_user(", "fetch_user(")

# Branch B: a new feature that starts using the old name. Nothing on branch B
# knows about the rename, so nothing on branch B can fail.
REPORTS = '''\
from users import get_user


def build_report(user_ids, amount):
    rows = []
    for user_id in user_ids:
        user = get_user(user_id)
        if user is not None:
            rows.append({"email": user["email"], "amount": amount})
    return rows
'''

TEST_REPORTS = '''\
import unittest

from reports import build_report


class TestReports(unittest.TestCase):
    def test_skips_unknown_users(self):
        rows = build_report(["u1", "nobody"], 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 5)
'''

# Branch C: unrelated, and it stays unrelated. Exists so the demo output shows
# a pair that passes, not only a failure. A tool that only ever prints red is a
# tool people assume is broken.
PAGINATION = '''\
def paginate(items, page_size, page=1):
    start = (page - 1) * page_size
    return items[start:start + page_size]
'''

TEST_PAGINATION = '''\
import unittest

from paging import paginate


class TestPagination(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate([1, 2, 3, 4], 2), [1, 2])

    def test_second_page(self):
        self.assertEqual(paginate([1, 2, 3, 4], 2, 2), [3, 4])

    def test_past_the_end_is_empty(self):
        self.assertEqual(paginate([1, 2], 2, 9), [])
'''


@dataclass
class DemoRepo:
    path: str
    base: str
    test_command: str
    branches: list[str]


def _git(cwd: str, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not build the demo repo: git {' '.join(args)}\n{proc.stderr}")


def _write(root: str, name: str, content: str) -> None:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    # newline="" keeps the file byte-identical on Windows, so the diffs the demo
    # produces look the same as on Linux.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _commit(cwd: str, message: str) -> None:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)


def build_demo_repo(root: str | None = None) -> DemoRepo:
    root = root or tempfile.mkdtemp(prefix="ghostmerge-demo-")
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)

    _git(root, "init", "-q", "-b", "main")
    # Local identity: the machine running the demo may have no global git user,
    # and a demo that dies on "please tell me who you are" is a lost user.
    _git(root, "config", "user.name", "ghostmerge demo")
    _git(root, "config", "user.email", "demo@localhost")
    _git(root, "config", "commit.gpgsign", "false")

    # `unittest discover -t .` refuses a start directory that isn't importable,
    # and a namespace package doesn't count. One empty file, one fewer
    # confusing traceback for anyone who runs the demo command.
    _write(root, "tests/__init__.py", "")
    _write(root, "users.py", USERS)
    _write(root, "invoices.py", INVOICES)
    _write(root, "tests/test_users.py", TEST_USERS)
    _write(root, "tests/test_invoices.py", TEST_INVOICES)
    _commit(root, "base: users, invoices and their tests")

    _git(root, "checkout", "-q", "-b", "agent/auth-refactor")
    _write(root, "users.py", AUTH_USERS)
    _write(root, "invoices.py", AUTH_INVOICES)
    _write(root, "tests/test_users.py", AUTH_TEST_USERS)
    _commit(root, "agent: rename get_user to fetch_user")

    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "agent/reports")
    _write(root, "reports.py", REPORTS)
    _write(root, "tests/test_reports.py", TEST_REPORTS)
    _commit(root, "agent: add usage report")

    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "agent/pagination")
    _write(root, "paging.py", PAGINATION)
    _write(root, "tests/test_pagination.py", TEST_PAGINATION)
    _commit(root, "agent: add pagination helper")

    _git(root, "checkout", "-q", "main")

    command = f'"{sys.executable}" -m unittest discover -s tests -t . -q'
    return DemoRepo(
        path=root,
        base="main",
        test_command=command,
        branches=["agent/auth-refactor", "agent/reports", "agent/pagination"],
    )
