"""Point at the branch that actually broke things.

A red test is not an answer, it is a question. "ImportError: cannot import name
'get_user'" leaves you to work out which of two branches is at fault, and the
answer is usually one grep away — but you have to know to grep for it.

This module is deliberately a heuristic and says so in the output. It reads the
failure, pulls out the identifiers the runtime complained about, and checks
which branch deleted them. When it finds a delete on exactly one side and a use
on the other, that is not a guess, it is the trace of the break. When it does
not, it admits it instead of inventing a culprit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import gitcmd

# Runtime and compiler messages worth mining. Ordered roughly by how diagnostic
# they are: an ImportError names the symbol and the module, a bare AssertionError
# names nothing and we should not pretend otherwise.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("import", re.compile(r"ImportError: cannot import name ['\"]([\w.]+)['\"](?: from ['\"]([\w.]+)['\"])?")),
    ("module", re.compile(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]")),
    ("attr", re.compile(r"AttributeError: (?:module|type object) ['\"][\w.]+['\"] has no attribute ['\"]([\w]+)['\"]")),
    ("attr", re.compile(r"AttributeError: ['\"][\w.<>]+['\"] object has no attribute ['\"]([\w]+)['\"]")),
    ("name", re.compile(r"NameError: name ['\"]([\w]+)['\"] is not defined")),
    # JS / TS
    ("ts-export", re.compile(r"has no exported member ['\"]([\w]+)['\"]")),
    ("ts-symbol", re.compile(r"Cannot find (?:name|module) ['\"]([\w.]+)['\"]")),
    ("js-type", re.compile(r"TypeError: (?:\w+\.)*([A-Za-z_$][\w$]*) is not a function")),
    # JVM / Go
    ("jvm", re.compile(r"cannot find symbol[\s\S]{0,80}?symbol:\s+\w+\s+([\w]+)")),
    ("go", re.compile(r"undefined: ([\w.]+)")),
]


@dataclass
class Symbol:
    name: str
    kind: str


@dataclass
class Evidence:
    branch: str
    file: str
    line: int
    text: str


@dataclass
class Clue:
    symbol: str
    kind: str
    cause: Optional[Evidence] = None
    renamed_to: Optional[str] = None
    callers: list[Evidence] = field(default_factory=list)
    confidence: str = "guess"  # "high" when a delete on one side meets a use on the other

    @property
    def explained(self) -> bool:
        return self.cause is not None


def extract_symbols(output: str, limit: int = 6) -> list[Symbol]:
    """Pull the identifiers a failure is complaining about, most specific first."""
    seen: dict[str, Symbol] = {}
    for line in output.splitlines():
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                name = match.group(1).split(".")[-1] if kind == "module" else match.group(1)
                name = name.strip()
                if not name or len(name) < 2:
                    continue
                if name not in seen:
                    seen[name] = Symbol(name, kind)
                if len(seen) >= limit:
                    return list(seen.values())
    return list(seen.values())


def _identifier_pattern(name: str) -> re.Pattern:
    # Word-boundary match, but `$` is a legal identifier char in JS so \b is
    # wrong there; use explicit boundaries instead.
    escaped = re.escape(name)
    return re.compile(rf"(?<![A-Za-z0-9_$]){escaped}(?![A-Za-z0-9_$])")


def _scan(diff: str) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Split a unified diff into (removed, added) line records.

    Tracking two line counters at once because the evidence we print has to be
    clickable: "users.py:4" must be line 4 in that branch, not an offset into
    the diff blob.
    """
    removed: list[tuple[str, int, str]] = []
    added: list[tuple[str, int, str]] = []
    current = ""
    old_no = new_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", line)
            if match:
                old_no, new_no = int(match.group(1)), int(match.group(2))
            continue
        if line.startswith("---") or line.startswith("diff --git") or line.startswith("index "):
            continue
        if line.startswith("-"):
            removed.append((current, old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            added.append((current, new_no, line[1:]))
            new_no += 1
        else:
            old_no += 1
            new_no += 1
    return removed, added


def _diff_cache(branches: Iterable[str], base: str, cwd: Optional[str]) -> dict[str, tuple[list, list]]:
    cache = {}
    for branch in branches:
        diff = gitcmd.diff_text(base, branch, cwd=cwd, context=0)
        cache[branch] = _scan(diff)
    return cache


def attribute(
    output: str,
    branches: list[str],
    base: str,
    cwd: Optional[str] = None,
) -> list[Clue]:
    """Map failure identifiers to the branch that removed them."""
    symbols = extract_symbols(output)
    if not symbols:
        return []

    diffs = _diff_cache(branches, base, cwd)
    clues: list[Clue] = []

    for symbol in symbols:
        pattern = _identifier_pattern(symbol.name)
        clue = Clue(symbol=symbol.name, kind=symbol.kind)

        deletions: list[Evidence] = []
        insertions: list[Evidence] = []
        for branch, (removed, added) in diffs.items():
            for file, line_no, text in removed:
                if pattern.search(text):
                    deletions.append(Evidence(branch, file, line_no, text.strip()))
            for file, line_no, text in added:
                if pattern.search(text):
                    insertions.append(Evidence(branch, file, line_no, text.strip()))

        # The break is: one branch deleted the name, another branch (or the same
        # one, before the merge) still refers to it. Prefer a deletion whose
        # branch has no matching insertion — that is a removal, not a rename.
        if deletions:
            deletions.sort(key=lambda e: _deletion_weight(e.text), reverse=True)
            clue.cause = deletions[0]
            # One caller line per branch. A branch that uses the missing name in
            # four places is still one branch to fix; repeating it four times
            # buries the second culprit.
            for evidence in insertions:
                if evidence.branch == clue.cause.branch:
                    continue
                if any(c.branch == evidence.branch for c in clue.callers):
                    continue
                clue.callers.append(evidence)
                if len(clue.callers) >= 2:
                    break
            if clue.callers:
                clue.confidence = "high"

            # Rename: the same branch deleted the old name and introduced a new
            # one. The new name cannot be found by searching for the old one —
            # `def fetch_user` does not contain `get_user` — so the pool is every
            # declaration the branch added.
            _removed_rows, added_rows = diffs[clue.cause.branch]
            declarations = [
                text for _file, _line, text in added_rows
                if _looks_like_declaration(text)
            ]
            clue.renamed_to = _guess_rename(symbol.name, "\n".join(declarations))

        clues.append(clue)

    # Explainable clues first: the user should not have to scroll past "maybe".
    clues.sort(key=lambda c: (c.confidence != "high", not c.explained))
    return clues


def _deletion_weight(text: str) -> int:
    """Rank deletions by how much they look like a declaration.

    `def get_user(` being deleted is the interesting line. `get_user(old_id)`
    appearing inside a deleted block is a side effect of that block going away.
    """
    stripped = text.strip()
    weight = 0
    for marker in ("def ", "class ", "function ", "export ", "interface ", "type ", "const ", "pub fn ", "struct "):
        if stripped.startswith(marker) or f" {marker}" in f" {stripped}":
            weight += 3
    if "(" in stripped and "=" not in stripped.split("(")[0]:
        weight += 1
    return weight


def _looks_like_declaration(text: str) -> bool:
    stripped = text.strip()
    return any(
        stripped.startswith(marker)
        for marker in ("def ", "class ", "function ", "async def ", "export ", "interface ",
                       "type ", "const ", "let ", "func ", "pub fn ", "struct ", "enum ")
    )


def _guess_rename(old: str, addition_text: str) -> Optional[str]:
    """Find the identifier the old name most likely became.

    Shape matters more than substring. `get_user` and `fetch_user` share the
    trailing word; `user_id` merely contains "user" and is a parameter, not a
    rename target. Matching on the last word first is what keeps this from
    confidently reporting the wrong name.
    """
    if not addition_text:
        return None
    tokens = {
        token for token in re.findall(r"[A-Za-z_$][\w$]{2,}", addition_text)
        if token != old
    }
    if not tokens:
        return None

    if "_" in old:
        tail = old.rsplit("_", 1)[-1]
        candidates = [t for t in tokens if "_" in t and t.rsplit("_", 1)[-1].lower() == tail.lower()]
    else:
        tail_match = re.search(r"[A-Z][a-z0-9]*$", old)
        tail = tail_match.group(0) if tail_match else old[-4:]
        candidates = [t for t in tokens if t.lower().endswith(tail.lower())]

    if not candidates:
        # No same-shape candidate: fall back to containing the old tail, but
        # only as a last resort, and say nothing if still ambiguous.
        loose = [t for t in tokens if tail.lower() in t.lower()]
        if len(loose) != 1:
            return None
        return loose[0]

    candidates.sort(key=lambda t: (abs(len(t) - len(old)), len(t)))
    return candidates[0]
