from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from skillhub.naming import eval_schema

log = logging.getLogger(__name__)

PROMPT_KEYS = ("agent_system", "judge_criterion", "judge_skill_use", "generator_tests", "generator_rubric")
PKG = Path(__file__).parent


@dataclass
class PromptRef:
    key: str
    name: str
    version: int
    template: str


def load_packaged(key: str) -> str:
    return (PKG / f"{key}.txt").read_text(encoding="utf-8")


def criterion_judge_template(criterion_text: str) -> str:
    """The criterion judge template with the criterion filled in; {{ inputs }}/{{ outputs }} stay."""
    return load_packaged("judge_criterion").replace("<<CRITERION>>", criterion_text)


def _prompt_name(cfg, key: str) -> str:
    return f"{eval_schema(cfg)}.{key}"


# The alias registration points at the newest packaged text. The Unity Catalog prompt registry resolves no
# `@latest`, so loads use this alias instead.
ALIAS = "current"


def _registry_load(name: str, version: int | None = None):
    import mlflow

    uri = f"prompts:/{name}/{version}" if version else f"prompts:/{name}@{ALIAS}"
    return mlflow.genai.load_prompt(uri)


def _registry_register(name: str, template: str):
    import mlflow

    created = mlflow.genai.register_prompt(name=name, template=template)
    mlflow.genai.set_prompt_alias(name, alias=ALIAS, version=int(created.version))
    return created


def load_prompt(cfg, key: str) -> PromptRef:
    name = _prompt_name(cfg, key)
    try:
        p = _registry_load(name)
        return PromptRef(key=key, name=name, version=int(p.version), template=p.template)
    except Exception as exc:  # registry unreachable or no version yet
        log.warning("prompt %s: falling back to packaged text (%s)", name, exc)
        return PromptRef(key=key, name=name, version=0, template=load_packaged(key))


def register_prompts(cfg) -> dict[str, PromptRef]:
    refs: dict[str, PromptRef] = {}
    for key in PROMPT_KEYS:
        name = _prompt_name(cfg, key)
        text = load_packaged(key)
        try:
            current = _registry_load(name)
            if current.template == text:
                refs[key] = PromptRef(key, name, int(current.version), text)
                continue
        except Exception:
            pass  # not registered yet
        created = _registry_register(name, text)
        refs[key] = PromptRef(key, name, int(created.version), text)
    return refs
