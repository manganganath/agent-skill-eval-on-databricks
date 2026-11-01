"""The LLM reviewer of the security scan: a model reads the skill as a reviewer would, for the risks the rules cannot
see, such as instructions to send data away. Its findings are at most `high`, so a person decides before anything is
blocked for good. An answer it cannot read, or one its endpoint's content filter stopped, is itself a high finding:
text in the skill may have caused it. Only a reviewer that cannot be reached is a warning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from skillhub.bundle import SkillBundle
from skillhub.gateway.models import ModelClient
from skillhub.generate.tests import ContentFiltered, GenerationError, ask_json
from skillhub.prompts import load_packaged
from skillhub.scan import Finding, mask_secrets

MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000
MAX_EVIDENCE_CHARS = 120


CATEGORIES = ("exfiltration", "unsafe-action", "policy", "injection")


class _Item(BaseModel):
    """Read leniently: a finding the model words differently is kept, never dropped."""

    file: str = "SKILL.md"
    severity: str = "high"
    category: str = "policy"
    evidence: str = ""
    why: str = ""

    def level(self) -> Literal["high", "medium", "low"]:
        s = self.severity.strip().lower()
        return s if s in ("medium", "low") else "high"  # type: ignore[return-value]  # critical and unknown are high

    def kind(self) -> str:
        c = self.category.strip().lower()
        return c if c in CATEGORIES else "policy"


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
    """The reviewer's findings. A file it names that is not in the bundle is reported on SKILL.md. A transport
    failure is raised, for the caller to report as a warning."""
    skill = f"{bundle.dept}/{bundle.name}"

    def person_must(message: str) -> list[Finding]:
        return [Finding(skill, "SKILL.md", 0, "llm-review", "high", message, source="llm")]

    try:
        parsed = ask_json(model, load_packaged("scan_reviewer"), review_text(bundle), Review)
    except ContentFiltered:  # the model would not even read it
        return person_must(
            "the reviewer model's content filter refused to read this skill; a person must read it"
        )
    except GenerationError:  # an answer it could not read, which the skill's text may have caused
        return person_must(
            "the reviewer's answer could not be read after three tries; a person must read this skill"
        )
    except (
        Exception
    ) as e:  # an endpoint that refuses this input (a guardrail, say) is a finding, not an outage
        code = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(code, int) and 400 <= code < 500 and code not in (401, 403, 404, 408, 429):
            return person_must(
                f"the reviewer's endpoint refused this skill (HTTP {code}); a person must read it"
            )
        raise
    out = []
    for item in parsed.findings:
        evidence = _clean(item.evidence, MAX_EVIDENCE_CHARS)
        why = _clean(item.why, 300) or "no reason given"
        message = f"{item.kind()}: {why}" + (f" (evidence: {evidence!r})" if evidence else "")
        path = item.file if item.file in bundle.files else "SKILL.md"
        out.append(Finding(skill, path, 0, "llm-review", item.level(), message, source="llm"))
    return out
