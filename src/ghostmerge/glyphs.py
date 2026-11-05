"""Characters the current output encoding can actually represent.

A tool that finishes the expensive part and then dies while printing the result
is worse than one that prints an uglier result. Windows redirects stdout to
cp1252 whenever it is not a terminal — a pipe, a CI job, `> report.txt` — and
cp1252 has no `✓`, `✗`, `│` or `─`. Writing one raises UnicodeEncodeError and
kills the process after all the work is done. Under `LC_ALL=C`, which is common
in containers, even `·` and `—` fail.

So every symbol is looked up rather than written literally: ask the stream's
encoding, fall back to an ASCII form that keeps the layout (all replacements are
one column wide where the original was). The fallback is deliberately boring —
`x`, `+`, `|`, `-` — because the alternative is a traceback.
"""

from __future__ import annotations

import sys
from typing import Dict

_UNICODE: Dict[str, str] = {
    "tick": "✓",
    "cross": "✗",
    "bar": "│",
    "rule": "─",
    "times": "×",
    "dot": "·",
    "ellipsis": "…",
    "dash": "—",
    "bang": "!",
}

# One column wide wherever the Unicode form is, so aligned output stays aligned.
_ASCII: Dict[str, str] = {
    "tick": "+",
    "cross": "x",
    "bar": "|",
    "rule": "-",
    "times": "x",
    "dot": ".",
    "ellipsis": "...",
    "dash": "-",
    "bang": "!",
}

_cache: Dict[str, str] = {}


def _encoding() -> str:
    return getattr(sys.stdout, "encoding", None) or "ascii"


def _can_encode(encoding: str, char: str) -> bool:
    try:
        char.encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def sym(name: str) -> str:
    """The best form of `name` that stdout can actually encode."""
    if name not in _cache:
        unicode_form = _UNICODE[name]
        _cache[name] = unicode_form if _can_encode(_encoding(), unicode_form) else _ASCII[name]
    return _cache[name]


def reset() -> None:
    """Re-detect after stdout was replaced (tests, redirects, capture)."""
    _cache.clear()
