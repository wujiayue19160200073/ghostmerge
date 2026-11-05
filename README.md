# ghostmerge

**Git says the merge is clean. Your tests say `ImportError`.**

`ghostmerge` finds the merges that git reports as conflict-free but that break when the merged code actually runs. It does this by *running* the merged code — in a throwaway worktree, with your own test command — instead of guessing from the diff.

```
$ ghostmerge check --all --test "pytest -q"

  ghostmerge  3 branches · 3 pairs · base main

  1 ghost merge

  ✗ agent/auth-refactor × agent/reports
    merge  clean    git saw no conflict at all
    tests  FAILED   exit 1

    │   File "tests/test_reports.py", line 3, in <module>
    │     from reports import build_report
    │   File "reports.py", line 1, in <module>
    │     from users import get_user
    │ ImportError: cannot import name 'get_user' from 'users'. Did you mean: 'fetch_user'?

    what broke it
      agent/auth-refactor deleted get_user, renamed to fetch_user  (users.py:6)
        -def get_user(user_id):
      agent/reports still calls it  (reports.py:1)
        +from users import get_user

  ── all pairs ────────────────────────────────────────
  ✓ agent/auth-refactor × agent/pagination  ·  pass   ·  1.2s
  ✗ agent/auth-refactor × agent/reports      ·  GHOST  ·  1.2s
  ✓ agent/pagination × agent/reports         ·  pass   ·  1.5s

  git says 0 conflicts. Your tests say 1.
  Each branch is green on its own; the break exists only after the merge.
```

## The failure this exists for

Branch A renames `get_user` to `fetch_user` and updates every caller it can see. Its tests pass.

Branch B adds a new file that imports `get_user`. Its tests pass too, because on branch B that function still exists.

The two branches touch **different files on different lines**. Git merges them without a single conflict marker. The result compiles, imports, and raises `ImportError` on the first request — in production, three days later, in code neither author has open.

This is not a rare edge case, it is the normal shape of work:

- two agents running in parallel worktrees, each green on its own branch
- a long-lived refactor branch plus a week of normal feature work
- a rename or a schema change that lands while someone else is building against the old name

Your test suite is the only thing that can catch it, and it has never been run on the merged result. Until now.

## Try it in one command

```bash
pipx install git+https://github.com/wujiayue19160200073/ghostmerge
ghostmerge demo
```

`demo` builds a real repository in a temp directory — three branches, one rename, one new caller — and runs the check on it. No configuration, no existing project required. That's the output above.

## Install

```bash
# recommended: isolated, on PATH
pipx install git+https://github.com/wujiayue19160200073/ghostmerge

# or into the current environment
pip install git+https://github.com/wujiayue19160200073/ghostmerge

# or from a clone, if you want to read the code first
git clone https://github.com/wujiayue19160200073/ghostmerge
cd ghostmerge && pip install -e .
```

Not on PyPI yet — the name is reserved for this project, but publishing is a
separate step and I would rather the first command in the README actually work.

Requires **git ≥ 2.38** (`git merge-tree --write-tree`) and **Python ≥ 3.9**. No dependencies, no network, no telemetry.

## Usage

### Checking branches you name

```bash
ghostmerge check feature-a feature-b --test "pytest -q"
```

Every pair among the branches you name gets merged and tested.

### Checking what your agents are doing (the main use case)

```bash
ghostmerge check --worktrees --test "pytest -q"
```

Takes every branch currently checked out in a worktree other than the base, and checks all pairs. Correct for 2–6 parallel agents. Beyond that, see [cost](#cost).

### Checking everything not yet merged

```bash
ghostmerge check --all --test "pytest -q"
```

Every branch ahead of the base. Pairs are `n(n-1)/2`, so be deliberate with a large `n`.

### Before merging, from a hook or CI

```bash
ghostmerge check --all --test "pytest -q" --markdown > ghost.md   # PR comment
ghostmerge check --all --test "pytest -q" --json                  # machine-readable
ghostmerge check --all --test "pytest -q" --no-fail               # advisory only
```

Exit codes: `0` no ghost merges · `1` at least one ghost merge · `2` usage or environment problem.

### Other commands

```bash
ghostmerge doctor    # is git new enough, is there a test command, how many pairs
ghostmerge clean     # remove temp worktrees left by a crashed run
```

Config lives in `.ghostmerge.json` next to the repo root:

```json
{ "base": "main", "test": "pytest -q", "timeout": 600 }
```

## What it actually does

1. `git merge-tree --write-tree` computes the merged tree **in the object database**. No checkout, no index changes, your working directory is never touched.
2. That tree becomes a detached, unreachable commit.
3. It is checked out into a temp worktree **outside your repo** (in the system temp dir), so linters, test discovery and `git status` never wander into it.
4. Your test command runs there.
5. The worktree is removed in a `finally` block — including on Ctrl-C.
6. If the merge was clean and the tests failed, that pair is reported as a **ghost merge**, with the branch that deleted the symbol and the branch still calling it.

Cleanup is verified by tests, not by hope: one test asserts that after a full run `git worktree list` has exactly one entry.

## Why not static analysis

There is a static approach to this problem and it is already taken: parse the diff, extract a symbol footprint, warn when one branch deletes a name another branch recently used. It requires a parser per language and it can only ever say *"this might break, please verify"*.

`ghostmerge` says *"this breaks, here is the traceback"* — because the merged code is right there, and running it costs one worktree.

The trade-off is honest: the static approach is faster and needs no test suite. If you have no tests, it is the better tool. If you have tests, running them is the ground truth.

## Cost

Each pair is one test run. 3 branches = 3 runs; 5 branches = 10 runs; 10 branches = 45.

- Use `--base` narrowly and check the branches that are actually in flight, not every branch in the repo.
- `--timeout` (default 600s) stops a hung suite from blocking the run; a timeout is reported as `timeout`, never as a ghost merge.
- `--jobs N` runs pairs concurrently. It defaults to `1` on purpose: two suites in parallel fight over ports and databases, which is the exact class of failure this tool exists to find. If you raise it, give each suite its own port and database.
- In CI, run it on a schedule or before merge, not on every push.

## What it does not do

- **It does not merge or push anything.** No branch is created, moved, or deleted. Trial commits are unreachable and garbage-collected.
- **It does not replace your CI merge queue.** It answers "do these two branches work together", not "is main green".
- **It does not guess when it cannot prove.** If the test output names no symbol, it says so and stops. There is no "confidence score" invented to fill the space.
- **It does not read your test results.** It runs the command you give it and reads the exit code. If that command is `true`, every merge passes and the tool is useless — that's on the command, not the tool.

## Requirements and platforms

- git ≥ 2.38, Python ≥ 3.9
- Linux, macOS, Windows (tested on all three in CI)
- Zero Python dependencies

## License

MIT.
