"""Command line entry point.

Three verbs, and the only one that matters is `check`. Everything else is there
to make `check` trustworthy: `demo` proves it works, `clean` removes what a
crashed run left behind, `doctor` explains why it cannot run.

Exit codes are a contract for CI:
  0  no ghost merges
  1  at least one ghost merge (clean merge, failing tests)
  2  usage or environment problem (bad ref, no test command, old git)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import __version__, blame, gitcmd, report
from . import trial as trial_mod
from .demo import build_demo_repo

EXIT_OK = 0
EXIT_GHOST = 1
EXIT_USAGE = 2

CONFIG_NAME = ".ghostmerge.json"

AUTO_TESTS = [
    ("pytest.ini", "python -m pytest -q"),
    ("pyproject.toml", "python -m pytest -q"),
    ("tox.ini", "python -m pytest -q"),
    ("package.json", "npm test --silent"),
    ("go.mod", "go test ./..."),
    ("Cargo.toml", "cargo test --quiet"),
    ("pom.xml", "mvn -q test"),
    ("build.gradle", "./gradlew test -q"),
]


def _load_config(root: str) -> dict:
    path = os.path.join(root, CONFIG_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ghostmerge: ignoring unreadable {CONFIG_NAME} ({exc})", file=sys.stderr)
        return {}


def _guess_base(root: str) -> str:
    for candidate in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if gitcmd.rev_parse(candidate, cwd=root):
            if candidate == "origin/HEAD":
                real = gitcmd.run(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=root)
                if real.ok and real.out.strip():
                    return real.out.strip()
            return candidate
    branch = gitcmd.current_branch(cwd=root)
    if branch:
        return branch
    raise gitcmd.GitError("cannot find a base branch; pass --base")


def _guess_test(root: str) -> Optional[str]:
    for marker, command in AUTO_TESTS:
        if os.path.exists(os.path.join(root, marker)):
            return command
    return None


def _worktree_branches(root: str, base: str) -> list[str]:
    """Branches checked out in other worktrees.

    A detached worktree has no branch to name, and pairing it would mean
    inventing a label that appears nowhere in the user's git. Better to say so:
    the agent tooling that creates these worktrees (worktrunk, `claude
    --worktree`, Codex) checks out a branch anyway.
    """
    branches: list[str] = []
    detached = 0
    for path, branch in gitcmd.list_worktrees(cwd=root):
        if branch:
            if branch != base:
                branches.append(branch)
        elif path.rstrip("\\/") != root.rstrip("\\/"):
            detached += 1
    if detached:
        print(
            f"ghostmerge: {detached} worktree(s) are on a detached HEAD and have no branch name; skipped.\n"
            "  Give each one a branch with `git -C <worktree> switch -c <name>`,"
            " or name the branches explicitly: ghostmerge check A B",
            file=sys.stderr,
        )
    return branches


def _select_branches(root: str, base: str, args) -> list[str]:
    if args.worktrees:
        branches = _worktree_branches(root, base)
    elif args.all:
        branches = gitcmd.branches_ahead_of(base, cwd=root)
    else:
        branches = list(args.branches)

    if not args.branches and not args.worktrees and not args.all:
        # Default to the parallel-agent case: whatever the other worktrees hold.
        from_worktrees = _worktree_branches(root, base)
        if len(from_worktrees) >= 2:
            print(
                "ghostmerge: no branches given; using the branches in other worktrees"
                f" ({len(from_worktrees)}). Pass --all to check every unmerged branch.",
                file=sys.stderr,
            )
            branches = from_worktrees
        else:
            branches = gitcmd.branches_ahead_of(base, cwd=root)

    unique: list[str] = []
    seen: set[str] = set()
    for branch in branches:
        if branch == base or branch in seen:
            continue
        if not gitcmd.rev_parse(branch, cwd=root):
            print(f"ghostmerge: skipping unknown branch {branch}", file=sys.stderr)
            continue
        seen.add(branch)
        unique.append(branch)
    return unique


def _print_plan(plan: trial_mod.Plan, test_command: str) -> None:
    print(f"  base      {plan.base}")
    print(f"  branches  {', '.join(plan.branches)}")
    print(f"  pairs     {len(plan.pairs)}")
    print(f"  test      {test_command}")
    if plan.skipped:
        print(f"  skipped   {len(plan.skipped)} pair(s), fast-forward")


def cmd_check(args) -> int:
    root = gitcmd.repo_root()
    gitcmd.require_supported_git()

    config = _load_config(root)
    base = args.base or config.get("base") or _guess_base(root)
    if not gitcmd.rev_parse(base, cwd=root):
        print(f"ghostmerge: base branch {base} does not exist", file=sys.stderr)
        return EXIT_USAGE

    branches = _select_branches(root, base, args)
    if len(branches) < 2:
        print(
            "ghostmerge: need at least two branches to pair up.\n"
            "  Run `ghostmerge demo` to see what this looks like, or pass --all to check"
            " every unmerged branch.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    test_command = args.test or config.get("test") or _guess_test(root)
    if not test_command:
        print(
            "ghostmerge: no test command. Pass one explicitly:\n"
            '  ghostmerge check --test "python -m pytest -q"\n'
            f'  or put it in {CONFIG_NAME}: {{"test": "pytest -q"}}',
            file=sys.stderr,
        )
        return EXIT_USAGE

    plan = trial_mod.build_plan(base, branches, cwd=root)
    if not plan.pairs:
        print("ghostmerge: no pairs to check (all fast-forwards).", file=sys.stderr)
        return EXIT_OK

    timeout = args.timeout or config.get("timeout") or 600

    if args.dry_run:
        print("\n  ghostmerge --dry-run\n")
        _print_plan(plan, test_command)
        print("\n  No tests were run. Drop --dry-run to start checking.\n")
        return EXIT_OK

    # stdout is a contract in --json mode: one JSON document, nothing else.
    # A friendly header line here would break every consumer that pipes it into
    # `jq` or json.load.
    if not args.quiet and not args.json:
        print(f"\n  ghostmerge {__version__} · base {base} · {len(plan.pairs)} pairs")
        print(f"  test: {test_command}")
        print("  Every merge runs in a throwaway worktree; your working directory is untouched.\n")

    def progress(index: int, total: int, a: str, b: str) -> None:
        if args.quiet or not sys.stdout.isatty():
            # A \r progress bar piped into a file becomes a single unreadable
            # line of every pair name jammed together. Only animate a terminal.
            return
        sys.stdout.write(f"\r  [{index}/{total}] {a} × {b}".ljust(70))
        sys.stdout.flush()

    trials = trial_mod.run_plan(
        plan,
        test_command=test_command,
        cwd=root,
        timeout=timeout,
        jobs=args.jobs,
        progress=None if (args.quiet or args.json) else progress,
    )
    if not args.quiet and not args.json and sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 72 + "\r")

    clue_map = {}
    for item in trials:
        if item.is_ghost:
            clue_map[f"{item.a}×{item.b}"] = blame.attribute(item.output, [item.a, item.b], base, cwd=root)

    if args.json:
        print(report.render_json(plan, trials, clue_map))
    elif args.markdown:
        print(report.render_markdown(plan, trials, clue_map))
    else:
        print(report.render_terminal(
            plan, trials, clue_map,
            color=None if args.color == "auto" else args.color == "always",
            show_passed=not args.no_pass,
            detail_limit=args.detail_limit,
        ))

    ghosts = [t for t in trials if t.is_ghost]
    if ghosts and args.fail_on_ghost is not False:
        return EXIT_GHOST
    return EXIT_OK


def cmd_demo(args) -> int:
    print("\n  Building a demo repository (4 files, 3 branches, 1 real ghost merge)…\n")
    repo = build_demo_repo()
    print(f"  repo: {repo.path}")
    for branch in repo.branches:
        print(f"    branch {branch}")
    print("\n  Each branch passes its own tests. Press Enter to run the check (Ctrl-C to cancel)…")
    if not args.yes and sys.stdin.isatty():
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return EXIT_OK

    cwd = os.getcwd()
    try:
        os.chdir(repo.path)
        args.base = repo.base
        args.branches = []
        args.all = True
        args.worktrees = False
        args.test = repo.test_command
        args.timeout = 60
        args.jobs = 1
        args.quiet = False
        args.json = False
        args.markdown = False
        args.color = "auto"
        args.no_pass = False
        args.detail_limit = 5
        args.dry_run = False
        args.fail_on_ghost = False
        code = cmd_check(args)
    finally:
        os.chdir(cwd)

    print(f"  The demo repo is still at {repo.path} — go look, or delete it.\n")
    return EXIT_OK if code in (EXIT_OK, EXIT_GHOST) else code


def cmd_clean(args) -> int:
    root = gitcmd.repo_root()
    removed = trial_mod.cleanup_stale(cwd=root)
    print(f"  Removed {removed} leftover trial worktree(s).")
    return EXIT_OK


def cmd_doctor(args) -> int:
    print("\n  ghostmerge doctor\n")
    try:
        version = gitcmd.git_version()
        found = ".".join(str(n) for n in version[:3])
        ok = version >= gitcmd.MIN_GIT
        print(f"  git          {found}  {'ok' if ok else 'needs >= 2.38'}")
        if not ok:
            return EXIT_USAGE
    except gitcmd.GitError as exc:
        print(f"  git          missing  {exc}")
        return EXIT_USAGE

    try:
        root = gitcmd.repo_root()
    except gitcmd.GitError:
        print("  repository   current directory is not inside a git repository")
        return EXIT_USAGE

    print(f"  repository   {root}")
    base = _guess_base(root)
    print(f"  base         {base}")
    test_command = _guess_test(root) or "(not detected; pass --test)"
    print(f"  test         {test_command}")
    ahead = gitcmd.branches_ahead_of(base, cwd=root)
    print(f"  unmerged     {len(ahead)} branch(es)" + (f": {', '.join(ahead[:6])}" if ahead else ""))
    worktrees = gitcmd.list_worktrees(cwd=root)
    print(f"  worktrees    {len(worktrees)}")
    pairs = len(ahead) * (len(ahead) - 1) // 2
    print(f"  pairs        {pairs} in --all mode")
    print(f"  working tree {'has uncommitted changes (does not affect the check)' if gitcmd.is_dirty(cwd=root) else 'clean'}")
    print()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghostmerge",
        description="Find the merges git calls clean and your tests call broken.",
    )
    parser.add_argument("--version", action="version", version=f"ghostmerge {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="actually merge every pair of branches and run tests")
    check.add_argument("branches", nargs="*", help="branches to check (default: those in other worktrees)")
    check.add_argument("--base", help="base branch; auto-detected as main/master when possible")
    check.add_argument("--test", help="test command to run inside the trial worktree")
    check.add_argument("--worktrees", action="store_true", help="use branches from all worktrees")
    check.add_argument("--all", action="store_true", help="use every branch not yet merged into base")
    check.add_argument("--timeout", type=float, help="per-test timeout in seconds (default 600)")
    check.add_argument("--jobs", type=int, default=1,
                       help="how many pairs to run at once (default 1: parallel suites fight over ports)")
    check.add_argument("--json", action="store_true", help="machine-readable output for CI")
    check.add_argument("--markdown", action="store_true", help="Markdown output for a PR comment")
    check.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    check.add_argument("--no-pass", action="store_true", help="hide pairs that passed")
    check.add_argument("--detail-limit", type=int, default=5, help="how many ghost merges to expand")
    check.add_argument("--dry-run", action="store_true", help="show the plan, run nothing")
    check.add_argument("--quiet", action="store_true")
    check.add_argument("--no-fail", dest="fail_on_ghost", action="store_false",
                       help="exit 0 even when ghost merges are found")
    check.set_defaults(func=cmd_check, fail_on_ghost=True)

    demo = sub.add_parser("demo", help="reproduce a ghost merge in a throwaway repo")
    demo.add_argument("--yes", "-y", action="store_true", help="do not wait for confirmation")
    demo.set_defaults(func=cmd_demo)

    clean = sub.add_parser("clean", help="remove trial worktrees left by a crashed run")
    clean.set_defaults(func=cmd_clean)

    doctor = sub.add_parser("doctor", help="check git version, base branch and test command")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        return int(args.func(args))
    except gitcmd.GitError as exc:
        print(f"\nghostmerge: {exc}\n", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\nInterrupted. Leftover worktrees can be removed with `ghostmerge clean`.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
