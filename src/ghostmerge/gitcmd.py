"""Thin wrapper around git plumbing.

Everything here is read-only with one exception: `commit_tree` writes a commit
object. Nothing in this module checks out, resets, or moves a branch. The whole
premise of ghostmerge is that it can answer "what would this merge do" without
touching anything the user cares about, so the git surface stays deliberately
small.

Why plumbing and not `git merge`:
  `git merge` needs a working tree and mutates index + HEAD. A tool that has to
  stash your work, merge, test, and reset is a tool that loses someone's
  uncommitted changes once and gets uninstalled forever. `git merge-tree
  --write-tree` computes the merged tree in the object database only. Plain
  files in, plain oid out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional, Sequence

# merge-tree --write-tree landed in 2.38. Asking for it on an older git gives a
# cryptic usage error, so we check up front and say something useful instead.
MIN_GIT = (2, 38)


class GitError(RuntimeError):
    """A git invocation failed in a way the user needs to know about."""

    def __init__(self, message: str, *, args: Sequence[str] = (), stderr: str = ""):
        self.args_used = list(args)
        self.stderr = stderr.strip()
        detail = f"\n  git {' '.join(self.args_used)}\n  {self.stderr}" if self.stderr else ""
        super().__init__(message + detail)


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = 120.0,
    check: bool = False,
    env: Optional[dict] = None,
) -> Result:
    """Run git and capture text output.

    Encoding is forced to utf-8 with replacement: git writes UTF-8 paths, but
    Windows consoles default to a codepage that mangles them. A tool that
    crashes on a Chinese filename is not a tool.
    """
    merged_env = None
    if env:
        merged_env = {**os.environ, **env}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=merged_env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitError("git not found; install git 2.38 or newer") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git command timed out after {timeout:.0f}s", args=args) from exc

    result = Result(proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise GitError("git command failed", args=args, stderr=result.err or result.out)
    return result


def git_version() -> tuple[int, ...]:
    out = run(["--version"]).out.strip()
    # "git version 2.55.0.windows.3"
    parts = out.split()
    if len(parts) < 3:
        return (0,)
    numbers = []
    for chunk in parts[2].split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


def require_supported_git() -> None:
    version = git_version()
    if version < MIN_GIT:
        have = ".".join(str(n) for n in version[:3]) or "unknown"
        raise GitError(
            f"git >= 2.38 required (found {have}). ghostmerge relies on"
            " `git merge-tree --write-tree`, added in 2.38; older git can only do a"
            " trivial three-way merge and cannot produce the real merged tree."
        )


def repo_root(cwd: Optional[str] = None) -> str:
    result = run(["rev-parse", "--show-toplevel"], cwd=cwd)
    if not result.ok or not result.out.strip():
        raise GitError("not inside a git repository (or not in any worktree)", args=["rev-parse", "--show-toplevel"], stderr=result.err)
    return result.out.strip()


def is_inside_work_tree(cwd: Optional[str] = None) -> bool:
    result = run(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return result.ok and result.out.strip() == "true"


def rev_parse(ref: str, cwd: Optional[str] = None) -> Optional[str]:
    result = run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd)
    return result.out.strip() if result.ok and result.out.strip() else None


def current_branch(cwd: Optional[str] = None) -> Optional[str]:
    result = run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd)
    return result.out.strip() if result.ok and result.out.strip() else None


def is_ancestor(maybe_ancestor: str, descendant: str, cwd: Optional[str] = None) -> bool:
    """True when `maybe_ancestor` is reachable from `descendant` (nothing to merge)."""
    result = run(["merge-base", "--is-ancestor", maybe_ancestor, descendant], cwd=cwd)
    if result.code not in (0, 1):
        return False
    return result.code == 0


def merge_base(a: str, b: str, cwd: Optional[str] = None) -> Optional[str]:
    result = run(["merge-base", a, b], cwd=cwd)
    return result.out.strip() if result.ok and result.out.strip() else None


def changed_files(base: str, branch: str, cwd: Optional[str] = None) -> list[str]:
    """Files that differ between the merge base and `branch`.

    Three-dot semantics (`base...branch`) is what a PR diff means: changes on
    this branch since it diverged. Using two dots here is a classic bug — it
    reports the other branch's work as your own.
    """
    result = run(["diff", "--name-only", "-z", f"{base}...{branch}"], cwd=cwd)
    if not result.ok:
        return []
    return [p for p in result.out.split("\0") if p.strip()]


def diff_text(base: str, branch: str, cwd: Optional[str] = None, *, context: int = 0) -> str:
    result = run(
        ["diff", "--no-color", f"-U{context}", f"{base}...{branch}"],
        cwd=cwd,
        timeout=180,
    )
    return result.out if result.ok else ""


@dataclass
class MergeProbe:
    """What `git merge-tree --write-tree` concluded, without touching disk."""

    clean: bool
    tree: Optional[str]
    conflicts: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def verdict(self) -> str:
        return "clean" if self.clean else "conflict"


def probe_merge(a: str, b: str, cwd: Optional[str] = None, *, timeout: float = 180.0) -> MergeProbe:
    """Compute the merged tree of `a` and `b` in the object database.

    Exit code is 1 when there are textual conflicts even though a tree is still
    written (with conflict markers inside it). We treat that tree as unusable:
    a file full of `<<<<<<<` cannot be tested meaningfully.
    """
    result = run(
        ["merge-tree", "--write-tree", "--name-only", "--no-messages", a, b],
        cwd=cwd,
        timeout=timeout,
    )
    lines = [line for line in result.out.splitlines() if line.strip()]
    if result.code not in (0, 1):
        raise GitError(
            "git merge-tree failed (the branches may have no common ancestor)",
            args=["merge-tree", "--write-tree", a, b],
            stderr=result.err or result.out,
        )
    tree = lines[0].strip() if lines else None
    conflicts = [line.strip() for line in lines[1:]] if result.code == 1 else []
    return MergeProbe(clean=result.code == 0, tree=tree, conflicts=conflicts, message=result.err.strip())


def commit_tree(tree: str, parents: Sequence[str], message: str, cwd: Optional[str] = None) -> str:
    """Wrap a tree into a throwaway commit so it can be checked out.

    These commits are unreachable and get garbage collected. They are never
    referenced by a branch, never pushed, and never shown to the user's git log.
    """
    args = ["commit-tree", tree]
    for parent in parents:
        args.extend(["-p", parent])
    args.extend(["-m", message])
    env = {
        # Do not inherit the user's identity: these commits are noise, and a
        # signing hook would otherwise stop the merge probe from working.
        "GIT_AUTHOR_NAME": "ghostmerge",
        "GIT_AUTHOR_EMAIL": "ghostmerge@localhost",
        "GIT_COMMITTER_NAME": "ghostmerge",
        "GIT_COMMITTER_EMAIL": "ghostmerge@localhost",
    }
    result = run(args, cwd=cwd, check=True, env=env)
    return result.out.strip()


def add_detached_worktree(path: str, commit: str, cwd: Optional[str] = None) -> None:
    result = run(["worktree", "add", "--detach", "--force", path, commit], cwd=cwd, timeout=300)
    if not result.ok:
        raise GitError("could not create a trial worktree", args=["worktree", "add", path], stderr=result.err or result.out)


def remove_worktree(path: str, cwd: Optional[str] = None) -> None:
    """Best-effort removal. Never raises: cleanup must not mask the real error."""
    run(["worktree", "remove", "--force", path], cwd=cwd, timeout=120)
    if os.path.isdir(path):
        # Dirty temp dir (tests wrote files); the git registration is gone but
        # the folder may remain on Windows where open handles block deletion.
        shutil.rmtree(path, ignore_errors=True)
    run(["worktree", "prune"], cwd=cwd, timeout=60)


def list_worktrees(cwd: Optional[str] = None) -> list[tuple[str, str]]:
    """Return (path, branch) pairs. Detached worktrees report an empty branch."""
    result = run(["worktree", "list", "--porcelain"], cwd=cwd)
    if not result.ok:
        return []
    entries: list[tuple[str, str]] = []
    path = branch = ""
    for line in result.out.splitlines():
        if line.startswith("worktree "):
            if path:
                entries.append((path, branch))
            path, branch = line[len("worktree "):].strip(), ""
        elif line.startswith("branch "):
            branch = line[len("branch "):].strip().replace("refs/heads/", "", 1)
    if path:
        entries.append((path, branch))
    return entries


def branches_ahead_of(base: str, cwd: Optional[str] = None) -> list[str]:
    result = run(
        ["for-each-ref", "--format=%(refname:short)", "--no-merged", base, "refs/heads"],
        cwd=cwd,
    )
    if not result.ok:
        return []
    return [line.strip() for line in result.out.splitlines() if line.strip()]


def short(oid: str) -> str:
    return oid[:8] if oid else ""


def shortstat(base: str, branch: str, cwd: Optional[str] = None) -> str:
    result = run(["diff", "--shortstat", f"{base}...{branch}"], cwd=cwd)
    return result.out.strip() if result.ok else ""


def is_dirty(cwd: Optional[str] = None) -> bool:
    result = run(["status", "--porcelain"], cwd=cwd)
    return bool(result.out.strip()) if result.ok else False


def ensure_git_available() -> None:
    try:
        run(["--version"])
    except GitError:
        print("ghostmerge: git not found; install it first.", file=sys.stderr)
        raise
