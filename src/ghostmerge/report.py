"""Output. Terminal for humans, Markdown for PR comments, JSON for CI.

Rule followed here: never print a number without the thing it counts next to it.
"1 ghost merge" is decoration; "git says 0 conflicts, your tests say 1" is the argument.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

from . import trial as trial_mod
from .blame import Clue
from .trial import Plan, Trial

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


class Paint:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "".join(codes) + str(text) + RESET


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Windows 10+ terminals handle ANSI once VT processing is on; Python
        # turns it on for us when the console supports it.
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") or "ANSICON" in os.environ
                    or os.environ.get("ConEmuANSI") == "ON" or True)
    return True


STATUS_STYLE = {
    trial_mod.GHOST: ("✗", RED, "GHOST"),
    trial_mod.PASS: ("✓", GREEN, "pass"),
    trial_mod.CONFLICT: ("—", DIM, "conflict"),
    trial_mod.TIMEOUT: ("!", YELLOW, "timeout"),
    trial_mod.ERROR: ("!", YELLOW, "error"),
}


def counts(trials: list[Trial]) -> dict[str, int]:
    tally = {status: 0 for status in (trial_mod.GHOST, trial_mod.PASS, trial_mod.CONFLICT,
                                      trial_mod.TIMEOUT, trial_mod.ERROR)}
    for item in trials:
        tally[item.status] = tally.get(item.status, 0) + 1
    return tally


def _headline(tally: dict[str, int]) -> str:
    ghosts = tally.get(trial_mod.GHOST, 0)
    if ghosts:
        return f"{ghosts} ghost merge{'s' if ghosts != 1 else ''}"
    if tally.get(trial_mod.CONFLICT) and not tally.get(trial_mod.PASS):
        return "no ghost merges (only conflicts git already reports)"
    return "no ghost merges"


def _status_line(item: Trial, paint: Paint) -> str:
    icon, color, label = STATUS_STYLE.get(item.status, ("?", DIM, item.status))
    seconds = f"{item.duration:.1f}s" if item.duration else ""
    parts = [f"  {paint(icon, color, BOLD)} {item.a} {paint('×', DIM)} {item.b}",
             paint(label, color)]
    if seconds:
        parts.append(paint(seconds, DIM))
    line = "  ·  ".join(parts)
    if item.status == trial_mod.CONFLICT and item.note:
        line += paint("  (git reports this one itself)", DIM)
    elif item.status == trial_mod.TIMEOUT:
        line += paint("  tests timed out, undecided", DIM)
    elif item.status == trial_mod.ERROR and item.note:
        line += paint(f"  {item.note.splitlines()[0][:100]}", DIM)
    return line


def _ghost_block(item: Trial, clues: list[Clue], paint: Paint) -> list[str]:
    lines: list[str] = []
    lines.append("")
    lines.append(f"  {paint('✗', RED, BOLD)} {paint(item.a, BOLD)} {paint('×', DIM)} {paint(item.b, BOLD)}")
    code = item.test_code if item.test_code is not None else "?"
    lines.append(f"    {paint('merge', DIM)}  clean    git saw no conflict at all")
    lines.append(f"    {paint('tests', DIM)}  {paint('FAILED', RED)}   exit {code}")

    failure = _failure_excerpt(item.output)
    if failure:
        lines.append("")
        for row in failure:
            lines.append(f"    {paint('│', RED)} {row}")

    explained = [c for c in clues if c.explained]
    if explained:
        lines.append("")
        lines.append(paint("    what broke it", DIM))
        for clue in explained[:2]:
            cause = clue.cause
            assert cause is not None
            rename = f", renamed to {clue.renamed_to}" if clue.renamed_to else ""
            lines.append(
                f"      {paint(cause.branch, CYAN)} deleted {paint(clue.symbol, BOLD)}{rename}"
                f"  {paint(chr(40) + cause.file + ':' + str(cause.line) + chr(41), DIM)}"
            )
            lines.append(f"        {paint('-' + cause.text.strip()[:88], DIM)}")
            for caller in clue.callers[:1]:
                lines.append(
                    f"      {paint(caller.branch, CYAN)} still calls it"
                    f"  {paint(chr(40) + caller.file + ':' + str(caller.line) + chr(41), DIM)}"
                )
                lines.append(f"        {paint('+' + caller.text.strip()[:88], DIM)}")
    else:
        lines.append("")
        lines.append(paint("    Could not attribute this automatically: the failure names no symbol.", DIM))
        lines.append(paint("    Read the output above, then `git log -S<symbol>` to see which side moved it.", DIM))
    return lines


# Temp worktree paths are long, differ on every run, and mean nothing to the
# reader. Scrubbing them makes output diffable and screenshots readable.
_TEMP_PATH = re.compile(r"(?:[A-Za-z]:)?[^\s\"'()]*ghostmerge-[0-9A-Za-z_]+[\\/]")
_NOISE = re.compile(r"^[\s~^|+=-]*$")


def _scrub(text: str) -> str:
    return _TEMP_PATH.sub("", text)


def _failure_excerpt(output: str, limit: int = 10) -> list[str]:
    """Show the line that explains the failure, not the pipeline around it.

    Test runners are generous with noise: unittest prints eight frames of its own
    loader before admitting `cannot import name 'get_user'`. Picking the first
    line that matches /Error/ therefore shows the loader, which explains nothing.

    So: find the *last* strong diagnostic line (the root cause is printed after
    its traceback), show a little context above it, and stop. If there is no
    strong match, fall back to the tail.
    """
    rows = [_scrub(row.rstrip()) for row in output.splitlines() if row.strip()]
    if not rows:
        return []

    strong = (
        "cannot import name", "No module named", "is not defined", "has no attribute",
        "has no exported member", "Cannot find name", "Cannot find module",
        "cannot find symbol", "undefined:", "AssertionError", "panic:", "SyntaxError",
        "Traceback (most recent call last)",
    )
    hits = [i for i, row in enumerate(rows) if any(marker in row for marker in strong)]
    if hits:
        # The last strong hit is the root cause; earlier ones are frames.
        index = hits[-1]
        window = rows[max(0, index - 5):index + 2][:limit]
    else:
        weak = [i for i, row in enumerate(rows) if "Error" in row or "FAILED" in row or "error:" in row]
        if weak:
            index = weak[-1]
            window = rows[max(0, index - 4):index + 3][:limit]
        else:
            window = rows[-limit:]

    # Trim decoration at the seams of the *window*, not of the whole output:
    # scrubbing leaves caret runs above and unittest leaves a rule below.
    while window and _NOISE.match(window[0]):
        window.pop(0)
    while window and _NOISE.match(window[-1]):
        window.pop()
    return window


def render_terminal(
    plan: Plan,
    trials: list[Trial],
    clues: dict[str, list[Clue]],
    *,
    color: Optional[bool] = None,
    show_passed: bool = True,
    detail_limit: int = 5,
) -> str:
    paint = Paint(supports_color() if color is None else color)
    tally = counts(trials)
    out: list[str] = []

    out.append("")
    out.append(
        f"  {paint('ghostmerge', BOLD)}  {len(plan.branches)} branches · {len(plan.pairs)} pairs"
        f" · base {paint(plan.base, CYAN)}"
    )
    out.append("")
    out.append(f"  {_headline(tally)}")

    ghosts = [t for t in trials if t.is_ghost]
    shown = 0
    for item in ghosts:
        if shown >= detail_limit:
            break
        out.extend(_ghost_block(item, clues.get(f"{item.a}×{item.b}", []), paint))
        shown += 1
    if len(ghosts) > shown:
        out.append("")
        out.append(paint(f"  {len(ghosts) - shown} more not shown; raise --detail-limit", DIM))

    out.append("")
    out.append(paint("  ── all pairs " + "─" * 40, DIM))
    for item in trials:
        if not show_passed and item.status in (trial_mod.PASS,):
            continue
        out.append(_status_line(item, paint))

    if plan.skipped:
        out.append("")
        out.append(paint(f"  skipped {len(plan.skipped)} pair(s): fast-forward, nothing to merge", DIM))

    out.append("")
    if ghosts:
        out.append(
            "  "
            + paint(f"git says 0 conflicts. Your tests say {len(ghosts)}.", BOLD)
            + paint("  Each branch is green on its own; the break exists only after the merge.", DIM)
        )
    elif tally.get(trial_mod.PASS):
        out.append("  " + paint("Every real merge passed its tests.", GREEN))
    else:
        out.append("  " + paint("No pairs to check.", DIM))
    out.append("")
    return "\n".join(out)


def render_markdown(plan: Plan, trials: list[Trial], clues: dict[str, list[Clue]]) -> str:
    tally = counts(trials)
    ghosts = [t for t in trials if t.is_ghost]
    out: list[str] = []

    if ghosts:
        out.append(f"## ghostmerge: {len(ghosts)} ghost merge{'s' if len(ghosts) != 1 else ''}")
        out.append("")
        out.append(
            "These branch pairs **merge cleanly** (0 conflicts) but their merged code fails the "
            "test suite. Each branch passes on its own."
        )
    else:
        out.append("## ghostmerge: no ghost merges")
        out.append("")
        out.append(f"Checked {len(plan.pairs)} pair(s); every real merge passed its tests.")

    out.append("")
    out.append("| Branch A | Branch B | merge | tests |")
    out.append("| --- | --- | --- | --- |")
    for item in trials:
        merge = "clean" if item.status in (trial_mod.GHOST, trial_mod.PASS) else "conflict"
        if item.status == trial_mod.GHOST:
            tests = "**FAILED**"
        elif item.status == trial_mod.PASS:
            tests = "passed"
        elif item.status == trial_mod.CONFLICT:
            tests = "— (not run)"
        elif item.status == trial_mod.TIMEOUT:
            tests = "timeout"
        else:
            tests = "error"
        out.append(f"| `{item.a}` | `{item.b}` | {merge} | {tests} |")

    for item in ghosts:
        out.append("")
        out.append(f"### `{item.a}` × `{item.b}`")
        out.append("")
        rows = _failure_excerpt(item.output, limit=15)
        if rows:
            out.append("```")
            out.extend(rows)
            out.append("```")
        for clue in clues.get(f"{item.a}×{item.b}", []):
            if not clue.explained:
                continue
            cause = clue.cause
            assert cause is not None
            rename = f" (renamed to `{clue.renamed_to}`)" if clue.renamed_to else ""
            out.append("")
            out.append(
                f"- `{cause.branch}` deleted `{clue.symbol}` at `{cause.file}:{cause.line}`{rename}"
            )
            for caller in clue.callers[:1]:
                out.append(f"- `{caller.branch}` still calls it at `{caller.file}:{caller.line}`")

    out.append("")
    out.append(f"<sub>base `{plan.base}` · {len(plan.branches)} branches · failed "
               f"{tally.get(trial_mod.GHOST, 0)}</sub>")
    return "\n".join(out)


def render_json(plan: Plan, trials: list[Trial], clues: dict[str, list[Clue]]) -> str:
    payload = {
        "base": plan.base,
        "branches": plan.branches,
        "pairs": len(plan.pairs),
        "ghosts": sum(1 for t in trials if t.is_ghost),
        "skipped": [{"a": a, "b": b, "reason": reason} for a, b, reason in plan.skipped],
        "trials": [
            {
                "a": t.a,
                "b": t.b,
                "status": t.status,
                "merge_clean": bool(t.probe and t.probe.clean),
                "conflicts": list(t.probe.conflicts) if t.probe else [],
                "test_exit": t.test_code,
                "duration": round(t.duration, 3),
                "note": t.note,
                "clues": [
                    {
                        "symbol": c.symbol,
                        "confidence": c.confidence,
                        "cause_branch": c.cause.branch if c.cause else None,
                        "cause_file": f"{c.cause.file}:{c.cause.line}" if c.cause else None,
                        "renamed_to": c.renamed_to,
                        "callers": [f"{e.branch}:{e.file}:{e.line}" for e in c.callers],
                    }
                    for c in clues.get(f"{t.a}×{t.b}", [])
                ],
            }
            for t in trials
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
