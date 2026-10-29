"""Every reason code the API sends has a plain-language message in the UI (ErrorState.tsx)."""

import re
from pathlib import Path

from backend.authz import Reason

ERROR_STATE = (
    Path(__file__).resolve().parents[3] / "app" / "frontend" / "src" / "components" / "ErrorState.tsx"
)


def _message_keys() -> set[str]:
    text = ERROR_STATE.read_text(encoding="utf-8")
    block = text.split("const MESSAGES", 1)[1].split("};", 1)[0]
    return set(re.findall(r"^\s*([a-z_]+):", block, re.MULTILINE))


def test_every_reason_code_has_a_message_in_the_ui():
    keys = _message_keys()
    assert keys, "no MESSAGES entries found in ErrorState.tsx"
    assert sorted(r.value for r in Reason if r.value not in keys) == []
