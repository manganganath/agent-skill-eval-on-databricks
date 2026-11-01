"""The LLM reviewer of the security scan (spec §9.2): a model reads the skill as a reviewer would, for the risks the
rules cannot see, such as instructions to send data away. Its findings are at most `high`, so a person decides
before anything is blocked for good."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from skillhub.bundle import SkillBundle
from skillhub.gateway.models import ModelClient
from skillhub.generate.tests import ContentFiltered, ask_json
from skillhub.prompts import load_packaged
from skillhub.scan import Finding, mask_secrets

MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000
MAX_EVIDENCE_CHARS = 120


class _Item(BaseModel):
    file: str
    severity: Literal["high", "medium", "low"]
    category: Literal["exfiltration", "unsafe-action", "policy", "injection"]
    evidence: str = ""
    why: str


class Review(BaseModel):
    findings: list[_Item] = []


def review_text(bundle: SkillBundle) -> str:
    """The bundle's text files, SKILL.md first, each cut at MAX_FILE_CHARS and all together at MAX_TOTAL_CHARS."""
    parts: list[str] = [f"Skill: {bundle.dept}/{bundle.name}"]
    total = 0
    for path in sorted(bundle.files, key=lambda p: (p != "SKILL.md", p)):
        try:
            text = bundle.files[path].decode("utf-8")
        except UnicodeDecodeError:
            parts.append(f"=== {path} (binary, not shown) ===")
            continue
        room = min(MAX_FILE_CHARS, MAX_TOTAL_CHARS - total)
        if room <= 0:
            parts.append(f"=== {path} (not shown: the review's size limit was reached) ===")
            continue
        cut = text[:room]
        total += len(cut)
        parts.append(f"=== {path} ===\n{cut}" + ("\n[... cut ...]" if len(cut) < len(text) else ""))
    return "\n\n".join(parts)


def _clean(text: str, limit: int) -> str:
    """One line, at most `limit` characters, with anything that looks like a credential masked."""
    return mask_secrets(" ".join(text.split()))[:limit]


def review_bundle(bundle: SkillBundle, model: ModelClient) -> list[Finding]:
    """The reviewer's findings. A file it names that is not in the bundle is reported on SKILL.md."""
    skill = f"{bundle.dept}/{bundle.name}"
    try:
        parsed = ask_json(model, load_packaged("scan_reviewer"), review_text(bundle), Review)
    except ContentFiltered:  # the model would not even read it: a person must
        message = "the reviewer model's content filter refused to read this skill; a person must read it"
        return [Finding(skill, "SKILL.md", 0, "llm-review", "high", message, source="llm")]
    out = []
    for item in parsed.findings:
        evidence = _clean(item.evidence, MAX_EVIDENCE_CHARS)
        message = f"{item.category}: {_clean(item.why, 300)}" + (
            f" (evidence: {evidence!r})" if evidence else ""
        )
        path = item.file if item.file in bundle.files else "SKILL.md"
        out.append(Finding(skill, path, 0, "llm-review", item.severity, message, source="llm"))
    return out
