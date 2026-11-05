"""Run the merged code, not just the diff.

The whole point of ghostmerge lives in this file: take a tree that git considers
conflict-free, check it out somewhere disposable, and run the project's own test
command against it. "Clean merge" is a statement about text. A passing test run
is a statement about behaviour. Only the second one protects you.

Isolation rules, in order of how much they cost when broken:
  1. The trial commit is detached, unreachable, and never pushed.
  2. The worktree is created outside the repo (system temp), so no build tool
     wanders into it while globbing `**/*`.
  3. Removal happens in `finally`, and a failed removal never masks a failure.
  4. The user's working tree is never read from, written to, or reset.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from . import gitcmd
from .gitcmd import MergeProbe

PASS = "pass"
GHOST = "ghost"
CONFLICT = "conflict"
TIMEOUT = "timeout"
ERROR = "error"
SKIPPED = "skipped"

# Keep the last N lines of test output. Failures print at the bottom, and the
# top of a pytest run is a wall of dots nobody reads.
OUTPUT_LINES = 40


@dataclass
class Trial:
    a: str
    b: str
    status: str
    probe: Optional[MergeProbe] = None
    test_code: Optional[int] = None
    output: str = ""
    duration: float = 0.0
    commit: str = ""
    note: str = ""

    @property
    def is_ghost(self) -> bool:
        return self.status == GHOST

    @property
    def label(self) -> str:
        return f"{self.a} × {self.b}"

    def tail(self, lines: int = OUTPUT_LINES) -> str:
        rows = [r for r in self.output.splitlines() if r.strip()]
        if len(rows) <= lines:
            return "\n".join(rows)
        return "\n".join(["…"] + rows[-lines:])


@dataclass
class Plan:
    """What we intend to check, before any test runs."""

    base: str
    branches: list[str]
    pairs: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    file_map: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        ghost_free = len(self.pairs)
        return f"{len(self.branches)} branches, {ghost_free} pairs to really merge"


def build_plan(base: str, branches: list[str], cwd: Optional[str] = None) -> Plan:
    """Pair up branches, dropping pairs where a merge is meaningless.

    Skipped for a reason, not for speed:
      - identical refs: nothing to merge.
      - ancestor/descendant: a fast-forward, git cannot produce a conflict, and
        running the tests would just re-run one branch's suite.
    Everything else gets checked, even pairs touching disjoint files: a rename
    in one branch and a fresh import in the other never collide textually, and
    they are exactly the case this tool exists for.
    """
    plan = Plan(base=base, branches=list(branches))

    for branch in branches:
        plan.file_map[branch] = gitcmd.changed_files(base, branch, cwd=cwd)

    for i in range(len(branches)):
        for j in range(i + 1, len(branches)):
            a, b = branches[i], branches[j]
            if a == b:
                plan.skipped.append((a, b, "same branch"))
                continue
            if gitcmd.is_ancestor(a, b, cwd=cwd):
                plan.skipped.append((a, b, f"{a} already contains {b} (or vice versa): fast-forward"))
                continue
            if gitcmd.is_ancestor(b, a, cwd=cwd):
                plan.skipped.append((a, b, f"{b} already contains {a} (or vice versa): fast-forward"))
                continue
            plan.pairs.append((a, b))
    return plan


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the whole process tree.

    `proc.kill()` on Windows kills the shell, not the pytest it spawned, and the
    orphaned child keeps the temp worktree locked so cleanup fails. This is the
    kind of bug that turns a timeout into a mysterious "directory not empty".
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    else:
        try:
            proc.kill()
        except OSError:
            pass


def run_test(command: str, cwd: str, timeout: float) -> tuple[Optional[int], str, bool]:
    """Run the project's own test command inside a disposable worktree."""
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return None, f"could not start the test command: {exc}", False

    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out = ""
        return None, (out or "") + f"\n[ghostmerge] tests timed out after {timeout:.0f}s; process tree killed", True


def run_trial(
    a: str,
    b: str,
    *,
    test_command: str,
    cwd: Optional[str] = None,
    timeout: float = 600.0,
) -> Trial:
    """Merge `a` and `b` in memory, then test the result. Never raises."""
    started = time.monotonic()
    try:
        probe = gitcmd.probe_merge(a, b, cwd=cwd)
    except gitcmd.GitError as exc:
        return Trial(a, b, ERROR, note=str(exc), duration=time.monotonic() - started)

    if not probe.clean:
        # Git already tells the user this. Reporting it as a ghost would make
        # the tool noisy and teach people to ignore it.
        return Trial(
            a, b, CONFLICT,
            probe=probe,
            duration=time.monotonic() - started,
            note=f"{len(probe.conflicts)} file(s) with textual conflicts",
        )

    if not probe.tree:
        return Trial(a, b, ERROR, probe=probe, note="merge-tree produced no tree",
                     duration=time.monotonic() - started)

    worktree_path = ""
    try:
        commit = gitcmd.commit_tree(probe.tree, [a, b], f"ghostmerge trial: {a} + {b}", cwd=cwd)
        worktree_path = tempfile.mkdtemp(prefix="ghostmerge-")
        # mkdtemp created the dir; `git worktree add` refuses a non-empty path.
        os.rmdir(worktree_path)
        gitcmd.add_detached_worktree(worktree_path, commit, cwd=cwd)

        code, output, timed_out = run_test(test_command, worktree_path, timeout)
        duration = time.monotonic() - started

        if timed_out:
            return Trial(a, b, TIMEOUT, probe=probe, output=output, duration=duration, commit=commit)
        if code is None:
            return Trial(a, b, ERROR, probe=probe, output=output, note="test command could not run", duration=duration, commit=commit)
        if code != 0:
            return Trial(a, b, GHOST, probe=probe, test_code=code, output=output, duration=duration, commit=commit)
        return Trial(a, b, PASS, probe=probe, test_code=0, output=output, duration=duration, commit=commit)

    except gitcmd.GitError as exc:
        return Trial(a, b, ERROR, probe=probe, note=str(exc), duration=time.monotonic() - started)
    except OSError as exc:
        return Trial(a, b, ERROR, probe=probe, note=f"trial worktree error: {exc}",
                     duration=time.monotonic() - started)
    finally:
        if worktree_path:
            gitcmd.remove_worktree(worktree_path, cwd=cwd)


def run_plan(
    plan: Plan,
    *,
    test_command: str,
    cwd: Optional[str] = None,
    timeout: float = 600.0,
    jobs: int = 1,
    progress=None,
) -> list[Trial]:
    """Run every pair in the plan, in order, optionally with a few at a time.

    Default is jobs=1 on purpose. Two test suites running at once fight over
    ports and databases — the exact failure this tool is meant to catch. Making
    that the default would be a bad joke.
    """
    results: list[Trial] = []
    if jobs <= 1:
        for index, (a, b) in enumerate(plan.pairs, 1):
            if progress:
                progress(index, len(plan.pairs), a, b)
            results.append(run_trial(a, b, test_command=test_command, cwd=cwd, timeout=timeout))
        return results

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(run_trial, a, b, test_command=test_command, cwd=cwd, timeout=timeout)
            for a, b in plan.pairs
        ]
        for index, future in enumerate(futures, 1):
            trial = future.result()
            if progress:
                progress(index, len(plan.pairs), trial.a, trial.b)
            results.append(trial)
    return results


def cleanup_stale(cwd: Optional[str] = None) -> int:
    """Remove worktrees a previous crashed run left behind.

    A killed process skips `finally`. Without this, the temp registry slowly
    fills with dead entries and `git worktree list` becomes unreadable.

    Safety, because this function deletes directories:
      1. Never the main worktree. A repo can legitimately be named
         `ghostmerge-something`, and a name is not proof of ownership.
      2. Only paths inside the system temp dir.
      3. Only names we generate.
    All three must hold. Two of three is how a cleanup command eats someone's
    checkout.
    """
    root = os.path.realpath(gitcmd.repo_root(cwd=cwd))
    temp_root = os.path.realpath(tempfile.gettempdir())
    removed = 0

    for path, _branch in gitcmd.list_worktrees(cwd=cwd):
        real = os.path.realpath(path)
        if real == root:
            continue
        try:
            inside_temp = os.path.commonpath([real, temp_root]) == temp_root
        except ValueError:
            # Different drives on Windows; commonpath refuses to compare.
            inside_temp = False
        if not inside_temp:
            continue
        if not os.path.basename(real).startswith("ghostmerge-"):
            continue

        gitcmd.remove_worktree(path, cwd=cwd)
        removed += 1
    return removed
