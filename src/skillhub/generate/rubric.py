"""Generate a scoring rubric from a skill's text. The skill text is data, never an instruction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from skillhub.bundle import SkillBundle, content_hash
from skillhub.evalspec import Criterion, Rubric
from skillhub.gateway.models import ModelClient
from skillhub.generate.tests import ask_json
from skillhub.prompts import load_packaged
from skillhub.text import neutralise_braces


class GeneratedCriterion(BaseModel):
    id: str = Field(pattern=r"^c\d+$")
    text: str = Field(min_length=3)
    type: Literal["task_quality", "skill_format"]
    weight: int = Field(ge=1, le=5)
    source: str = Field(min_length=1)


class GeneratedRubric(BaseModel):
    criteria: list[GeneratedCriterion] = Field(min_length=5, max_length=10)


def _unique_ids(parsed: GeneratedRubric) -> None:
    ids = [c.id for c in parsed.criteria]
    if len(set(ids)) != len(ids):
        raise ValueError("criterion ids must be unique")


def generate_rubric(cfg, bundle: SkillBundle, model: ModelClient) -> Rubric:
    parts = [
        f"--- {path} ---\n{data.decode('utf-8', 'replace')}"
        for path, data in sorted(bundle.files.items())
        if path.endswith((".md", ".txt"))
    ]
    user_text = f"Skill name: {bundle.name}\n\n" + "\n\n".join(parts)
    parsed = ask_json(model, load_packaged("generator_rubric"), user_text, GeneratedRubric, _unique_ids)
    # Criterion text comes from an LLM and is later filled into a judge template: neutralise `{{ }}`.
    criteria = [Criterion(**{**c.model_dump(), "text": neutralise_braces(c.text)}) for c in parsed.criteria]
    return Rubric(
        skill_name=bundle.name,
        source_hash=content_hash(bundle.files),
        generated_by=cfg.generator,
        pass_score=cfg.defaults.pass_score,
        reviewed=False,
        criteria=criteria,
    )
