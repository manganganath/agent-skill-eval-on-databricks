from __future__ import annotations

import hashlib
from typing import Any

MAX_LEN = 128


class ToolNameMap:
    """Sanitise tool names for providers and map them back.

    Providers accept ^[a-zA-Z0-9_-]{1,128}$. Dots become '__'. Names longer than
    128 characters become name[:119] + '_' + sha1(name)[:8].

    Also rewrites staged skill FQNs to production FQNs (what the model sees) and
    back again (what the skills server understands).
    """

    def __init__(self) -> None:
        self._to_original: dict[str, str] = {}
        self._to_sanitized: dict[str, str] = {}
        self._rewrites: list[tuple[str, str]] = []
        self._reverse: list[tuple[str, str]] = []

    @staticmethod
    def _sanitise(name: str) -> str:
        s = name.replace(".", "__")
        if len(s) > MAX_LEN:
            digest = hashlib.sha1(name.encode()).hexdigest()[:8]
            s = s[: MAX_LEN - 9] + "_" + digest
        return s

    def register(self, original: str) -> str:
        if original in self._to_sanitized:
            return self._to_sanitized[original]
        s = self._sanitise(original)
        prev = self._to_original.get(s)
        if prev is not None and prev != original:
            raise ValueError(f"tool name collision: {original!r} and {prev!r} both sanitise to {s!r}")
        self._to_original[s] = original
        self._to_sanitized[original] = s
        return s

    def original(self, sanitized: str) -> str:
        return self._to_original[sanitized]

    def known(self) -> set[str]:
        return set(self._to_original)

    def set_fqn_rewrite(self, staged_to_production: dict[str, str]) -> None:
        # Longest name first, so a shorter name that is a prefix of a longer one is not rewritten inside it.
        self._rewrites = sorted(staged_to_production.items(), key=lambda kv: len(kv[0]), reverse=True)
        self._reverse = sorted(
            ((prod, staged) for staged, prod in staged_to_production.items()),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )

    def to_production(self, text: str) -> str:
        for staged, production in self._rewrites:
            text = text.replace(staged, production)
        return text

    def to_staged(self, value: Any) -> Any:
        """Reverse of to_production, applied recursively to strings in dicts and lists."""
        if isinstance(value, str):
            for production, staged in self._reverse:
                value = value.replace(production, staged)
            return value
        if isinstance(value, dict):
            return {k: self.to_staged(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.to_staged(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.to_staged(v) for v in value)
        return value
