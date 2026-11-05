"""Run one CI step and republish its failure as a check annotation.

Why this exists: workflow job logs need an authenticated API call, so anyone
without write access to the repository — a contributor, or the maintainer
reading a red X from a phone — cannot see *why* a job failed. Annotations are
public. GitHub's own `::error::` workflow command is the only channel that
survives that restriction.

Two details that are easy to get wrong and are the whole point of this file:

- `%` must be escaped to `%25` *before* newlines become `%0A`. GitHub decodes
  `%0A` on the way in; a literal `%` in test output would otherwise be eaten or
  corrupt the escape sequence that follows it.
- The tail is capped. An annotation is not a log viewer, and a 200 KB
  annotation gets truncated at an arbitrary byte, usually mid-traceback.

Usage:  python tools/ci_step.py "<label>" <command> [args...]
"""

from __future__ import annotations

import subprocess
import sys

MAX_CHARS = 3000


def annotate(label: str, text: str) -> None:
    tail = "\n".join(text.splitlines()[-40:])[-MAX_CHARS:]
    body = tail.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title={label} failed::{body}", flush=True)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: ci_step.py <label> <command> [args...]", file=sys.stderr)
        return 2
    label, cmd = argv[1], argv[2:]
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        annotate(label, f"could not start {cmd[0]}: {exc}")
        return 127

    output = (proc.stdout or "") + (proc.stderr or "")
    print(output, flush=True)
    if proc.returncode != 0:
        annotate(label, output or f"exited {proc.returncode} with no output")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
