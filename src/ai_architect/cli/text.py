"""Explicit Windows-console text sanitization.

Legacy Windows consoles speak cp1252, while model output routinely
contains Unicode punctuation (notably the non-breaking hyphen U+2011
favored by current models, curly quotes, and non-breaking spaces).
A bare ``.encode("cp1252", errors="replace")`` turns every such
character into ``?`` (e.g. "auto-scaling" rendered "auto?scaling"),
so common punctuation is mapped to readable ASCII equivalents FIRST.
Characters with no mapping (emoji, CJK, symbols) still fall back to
``?`` rather than crashing the CLI; that last resort is intentional
and covered by tests.
"""

_CONSOLE_REPLACEMENTS = {
    "\u2192": "->",  # rightwards arrow
    "\u2190": "<-",  # leftwards arrow
    "\u2194": "<->",  # left-right arrow
    "\u2014": "--",  # em dash
    "\u2013": "-",  # en dash
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2212": "-",  # minus sign
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote
    "\u2026": "...",  # ellipsis
    "\u00a0": " ",  # no-break space
    "\u202f": " ",  # narrow no-break space
    "\u2009": " ",  # thin space
    "\u200b": "",  # zero-width space
    "\u2713": "ok",  # check mark
    "\u2717": "x",  # ballot x
}


def sanitize_console_text(text: object) -> str:
    """Make dynamic text safe for legacy Windows consoles (cp1252)."""
    out = str(text)
    for src, dst in _CONSOLE_REPLACEMENTS.items():
        out = out.replace(src, dst)
    return out.encode("cp1252", errors="replace").decode("cp1252")
