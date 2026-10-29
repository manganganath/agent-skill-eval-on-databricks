from __future__ import annotations

from pathlib import Path

from mlflow.genai.judges import make_judge
from mlflow.genai.scorers import ExpectationsGuidelines, Safety, ToolCallCorrectness, ToolCallEfficiency

from skillhub.config import ConfigError
from skillhub.eval.scorers import make_planted_bugs_scorer, make_skill_trigger_scorer
from skillhub.naming import judge_name
from skillhub.prompts import criterion_judge_template, load_packaged
from skillhub.text import neutralise_braces


def _model_uri(cfg, profile: str) -> str:
    return f"databricks:/{cfg.models[profile].endpoint}"


def criterion_instructions(text: str) -> str:
    """The generic criterion template with `text` filled in. Criterion text is data: runs of `{{` or `}}`
    are collapsed to one brace so it can never add a template variable to the judge."""
    return criterion_judge_template(neutralise_braces(text))


def rubric_judges(cfg, rubric, profile: str) -> list:
    return [
        make_judge(
            name=judge_name(f"rubric_{c.id}", profile),
            instructions=criterion_instructions(c.text),
            model=_model_uri(cfg, profile),
            feedback_value_type=bool,
        )
        for c in rubric.criteria
    ]


def skill_use_judge(cfg, profile: str):
    return make_judge(
        name=judge_name("skill_use", profile),
        instructions=load_packaged("judge_skill_use"),
        model=_model_uri(cfg, profile),
        feedback_value_type=bool,
    )


def assertion_scorers(cfg, profile: str) -> list:
    # ExpectationsGuidelines reads each case's expectations["guidelines"] (the case assertions).
    return [ExpectationsGuidelines(name=judge_name("assertions", profile), model=_model_uri(cfg, profile))]


def builtin_scorers(cfg, profile: str) -> list:
    # ToolCallCorrectness reads expectations["expected_tool_calls"] and the TOOL span inputs.
    uri = _model_uri(cfg, profile)
    return [ToolCallCorrectness(model=uri), ToolCallEfficiency(model=uri), Safety(model=uri)]


def scorers_for(
    cfg,
    rubric,
    profiles: list[str],
    mode: str,
    *,
    skill_name: str,
    cases: dict | None = None,
    fixtures_root: Path | None = None,
) -> list:
    """Scorer list for one (skill, mode) across the judge panel. `cases` and `fixtures_root` feed the
    audit-xls planted-bug scorer, which scopes itself to the workbook cases; `fixtures_root` is
    required for that skill and a ValueError is raised without it.

    The built-in scorers (tool-call correctness and efficiency, safety) have one fixed name each, so they
    are deduped: the first profile in `profiles` is the one that judges them.
    """
    if mode == "trigger":
        return [make_skill_trigger_scorer(cases or {}, skill_name)]
    if skill_name == "audit-xls" and fixtures_root is None:
        raise ValueError("fixtures_root is required for audit-xls (it holds the planted-bug manifest)")
    out: list = []
    for p in profiles:
        out += [*rubric_judges(cfg, rubric, p), *assertion_scorers(cfg, p)]
        if mode == "with_skill":
            out.append(skill_use_judge(cfg, p))
        out += builtin_scorers(cfg, p)
    if skill_name == "audit-xls":
        out.append(make_planted_bugs_scorer(cases or {}, fixtures_root))
    return _dedupe(out)


def register_judges(cfg, experiment_id: str) -> list[str]:
    """Register the skill-use judge of `judges[0]` only. Called by `skillhub bootstrap`.

    The rest of the panel runs at eval time without registration. The registered judge is the one reviewers
    run from the MLflow UI, so it must not be the generator, which wrote the rubric and the cases."""
    profile = cfg.judges[0]
    if profile == getattr(cfg, "generator", None):
        raise ConfigError(
            f"judges[0] ({profile!r}) is the registered judge and must not be the generator model; "
            "put another judge profile first"
        )
    judge = skill_use_judge(cfg, profile)
    judge.register(experiment_id=experiment_id)
    return [judge.name]


def _dedupe(scorers: list) -> list:
    # Built-ins are created once per profile but named once: keep the first.
    seen, out = set(), []
    for s in scorers:
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append(s)
    return out
