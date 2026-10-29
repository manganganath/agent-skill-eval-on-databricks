"""Generate trigger test cases from a skill's text.

SKILL.md text is data: it is passed as the user message to the packaged `generator_tests` template and is
never part of an instruction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from skillhub import SkillHubError
from skillhub.bundle import SkillBundle
from skillhub.evalspec import EvalCase
from skillhub.gateway.models import ModelClient
from skillhub.prompts import load_packaged

T = TypeVar("T", bound=BaseModel)  # a TypeVar, not PEP 695 syntax: the Databricks Apps runtime is Python 3.11

MAX_TRIES = 3


class GenerationError(SkillHubError):
    pass


class GeneratedCase(BaseModel):
    prompt: str = Field(min_length=1)
    should_trigger: bool
    expected_skill: str | None = None
    kind: Literal["positive", "near_miss", "unrelated"]


class GeneratedCases(BaseModel):
    cases: list[GeneratedCase] = Field(min_length=1)


def parse_json_strict(text: str) -> dict:
    """Parse a JSON object, allowing one surrounding markdown fence. Anything else raises ValueError."""
    value = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def sibling_bundles(bundles: list[SkillBundle], bundle: SkillBundle) -> list[SkillBundle]:
    """Same department plus common. Common itself sees only common."""
    scope = {"common"} if bundle.dept == "common" else {bundle.dept, "common"}
    return [b for b in bundles if b.dept in scope and b.name != bundle.name]


def _normalise(parsed: GeneratedCases, siblings: set[str], skill_name: str) -> None:
    """Fix two self-consistent label slips in place; every other inconsistency stays a validation error."""
    for c in parsed.cases:
        if c.kind == "positive" and c.expected_skill == skill_name:
            c.expected_skill = None
        elif c.kind == "near_miss" and c.expected_skill in siblings and c.should_trigger:
            c.should_trigger = False  # a near-miss by definition does not trigger this skill


def _validate(parsed: GeneratedCases, siblings: set[str], skill_name: str) -> None:
    _normalise(parsed, siblings, skill_name)
    if not any(c.should_trigger for c in parsed.cases):
        raise ValueError("no positive cases")
    if all(c.should_trigger for c in parsed.cases):
        raise ValueError("no negative cases")
    # The name as a whole hyphenated word: "comps" in "compset", or "audit" in "audit-xls", is not the name.
    named = re.compile(rf"(?<![\w-]){re.escape(skill_name)}(?![\w-])", re.I)
    for c in parsed.cases:
        if named.search(c.prompt):
            raise ValueError(f"a test prompt must not contain the skill name {skill_name!r}")
    for c in parsed.cases:
        if c.kind == "near_miss" and c.expected_skill not in siblings:
            raise ValueError(
                f"near_miss must name a sibling in scope, got {c.expected_skill!r}; "
                f"siblings: {sorted(siblings)[:10]}"
            )
        if c.kind == "near_miss" and c.should_trigger:
            raise ValueError("near_miss must not trigger this skill (should_trigger must be false)")
        if c.kind == "positive" and (not c.should_trigger or c.expected_skill is not None):
            raise ValueError("positive case must have should_trigger=true and no expected_skill")
        if c.kind == "unrelated" and (c.should_trigger or c.expected_skill is not None):
            raise ValueError("unrelated case must have should_trigger=false and no expected_skill")


def ask_json(
    model: ModelClient,
    instructions: str,
    user_text: str,
    schema: type[T],
    validate: Callable[[T], None] | None = None,
) -> T:
    """Ask for JSON and validate it. Malformed output is retried; after MAX_TRIES it is a GenerationError."""
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": user_text}]
    last_error = ""
    for _ in range(MAX_TRIES):
        res = model.chat(messages, [], max_tokens=4096)
        try:
            parsed = schema.model_validate(parse_json_strict(res.text))
            if validate:
                validate(parsed)
            return parsed
        except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
            last_error = " ".join(str(exc).split())
            messages = messages + [
                res.assistant_message,
                {"role": "user", "content": f"Invalid output: {last_error}. Return only valid JSON."},
            ]
    raise GenerationError(f"model output was not valid after {MAX_TRIES} attempts: {last_error}")


def generate_trigger_cases(
    cfg, bundle: SkillBundle, siblings: list[SkillBundle], model: ModelClient, *, start_id: int = 1
) -> list[EvalCase]:
    sibling_names = {b.name for b in siblings}
    skill_text = bundle.files.get("SKILL.md", b"").decode("utf-8", "replace")
    user_text = (
        f"Skill name: {bundle.name}\n\nSKILL.md:\n{skill_text}\n\n"
        f"Sibling skills: {json.dumps(sorted(sibling_names))}"
    )
    parsed = ask_json(
        model,
        load_packaged("generator_tests"),
        user_text,
        GeneratedCases,
        validate=lambda p: _validate(p, sibling_names, bundle.name),
    )
    return [
        EvalCase(
            id=start_id + i,
            kind="trigger",
            prompt=c.prompt,
            should_trigger=c.should_trigger,
            expected_skill=c.expected_skill,
            origin="generated",
            reviewed=False,
        )
        for i, c in enumerate(parsed.cases)
    ]
