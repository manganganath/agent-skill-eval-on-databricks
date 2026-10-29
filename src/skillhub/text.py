"""Small text helpers with no heavy imports."""

from __future__ import annotations

import re

_BRACES = re.compile(r"\{{2,}|\}{2,}")


def neutralise_braces(text: str) -> str:
    """Collapse each run of `{{` or `}}` to one brace so text can never add a template variable to a judge."""
    return _BRACES.sub(lambda m: m.group(0)[0], text)
