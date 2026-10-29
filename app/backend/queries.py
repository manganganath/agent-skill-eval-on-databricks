"""SQL text for the Skill Hub. Identifiers come from skillhub.naming and the config; values are parameters."""

from __future__ import annotations

from skillhub.config import HubConfig
from skillhub.naming import dataset_table, eval_schema, evals_schema


class UnknownDepartment(ValueError):
    """The department is not in the config. Routes map this to 404 or 403."""


def _check_dept(cfg: HubConfig, dept: str) -> None:
    # dept becomes part of a schema name, so only configured departments may reach evals_schema().
    if dept not in cfg.departments:
        raise UnknownDepartment("unknown department")


def journal_sql(cfg: HubConfig, dept: str, skill: str | None = None) -> tuple[str, dict[str, object]]:
    """The department's skill_versions rows, oldest first. `skill` narrows them to one skill."""
    _check_dept(cfg, dept)
    params: dict[str, object] = {"dept": dept}
    where = "dept = :dept"
    if skill is not None:
        where += " AND skill = :skill"
        params["skill"] = skill
    sql = (
        "SELECT dept, skill, action, version, from_version, to_version, versioned_fqn, content_hash, "
        "git_sha, eval_run_id, actor, job_run_id, ts "
        f"FROM {evals_schema(cfg, dept)}.skill_versions WHERE {where} ORDER BY ts"
    )
    return sql, params


# Explicit column lists that match the frontend MatrixRow and CaseRow (app/frontend/src/api/types.ts).
# `dept` is not stored in a department's own views, so the route adds it. requested_by is never selected.
MATRIX_COLUMNS = (
    "skill, skill_version, content_hash, model_profile, harness, endpoint, model_class, mode, verdict, run_id, "
    "experiment_id, n_cases, n_reviewed, pass_rate, spread, error_rate, accuracy, precision, recall, "
    "pass_rate_without, uplift_task_quality, uplift_skill_format, started_at"
)
CASE_COLUMNS = (
    "run_id, trace_id, assessment_name, case_id, repeat, mode, user_input, expected_skill, invoked_skill, "
    "trigger_outcome, value, rationale, judge_model, source_type, reviewed"
)
RESULT_LIMIT = 5000
# One more row than the cap is fetched, so the route can tell that the cap was hit.
_FETCH = RESULT_LIMIT + 1


def matrix_sql(cfg: HubConfig, dept: str, skill: str | None) -> tuple[str, dict[str, object]]:
    _check_dept(cfg, dept)
    where = ["1 = 1"]
    params: dict[str, object] = {}
    if skill is not None:
        where.append("skill = :skill")
        params["skill"] = skill
    sql = (
        f"SELECT {MATRIX_COLUMNS} FROM {evals_schema(cfg, dept)}.eval_matrix WHERE {' AND '.join(where)} "
        f"ORDER BY started_at DESC, run_id LIMIT {_FETCH}"
    )
    return sql, params


def cases_sql(cfg: HubConfig, dept: str, run_id: str) -> tuple[str, dict[str, object]]:
    _check_dept(cfg, dept)
    sql = (
        f"SELECT {CASE_COLUMNS} FROM {evals_schema(cfg, dept)}.eval_cases WHERE run_id = :run_id "
        f"ORDER BY case_id, repeat, assessment_name LIMIT {_FETCH}"
    )
    return sql, {"run_id": run_id}


def cases_all_sql(cfg: HubConfig, run_id: str) -> tuple[str, dict[str, object]]:
    """Reviewer-only cross-department view. The view carries `dept`. The user's own SELECT decides."""
    sql = (
        f"SELECT {CASE_COLUMNS}, dept FROM {eval_schema(cfg)}.cases_all WHERE run_id = :run_id "
        f"ORDER BY case_id, repeat, assessment_name LIMIT {_FETCH}"
    )
    return sql, {"run_id": run_id}


def catalog_verdicts_sql(
    cfg: HubConfig, dept: str, skill: str | None = None
) -> tuple[str, dict[str, object]]:
    """Verdict rows for the catalog; reduce them with skill_ref.live_verdicts(). `skill` narrows them to one skill."""
    _check_dept(cfg, dept)
    sql = f"SELECT skill, skill_version, model_profile, mode, verdict FROM {evals_schema(cfg, dept)}.eval_matrix"
    if skill is None:
        return sql, {}
    return f"{sql} WHERE skill = :skill", {"skill": skill}


def dataset_sql(cfg: HubConfig, dept: str, skill: str) -> tuple[str, dict[str, object]]:
    # Test cases live in the eval dataset table, never in skill files. parse_fqn has already checked the
    # skill name against the skill-name pattern, so it is safe as part of an identifier.
    _check_dept(cfg, dept)
    return (
        f"SELECT inputs, expectations, tags, last_update_time FROM {dataset_table(cfg, dept, skill)} LIMIT 5000",
        {},
    )


def dataset_plain_sql(cfg: HubConfig, dept: str, skill: str) -> tuple[str, dict[str, object]]:
    """The same read without the update-time column, for a dataset table that has none."""
    _check_dept(cfg, dept)
    return f"SELECT inputs, expectations, tags FROM {dataset_table(cfg, dept, skill)} LIMIT 5000", {}


def rubric_sql(cfg: HubConfig, dept: str, skill: str) -> tuple[str, dict[str, object]]:
    _check_dept(cfg, dept)
    sql = (
        "SELECT rubric_json, reviewed, ts "
        f"FROM {evals_schema(cfg, dept)}.rubrics WHERE skill = :skill ORDER BY ts DESC LIMIT 1"
    )
    return sql, {"skill": skill}


# The eval_runs columns a candidate page shows. requested_by is never selected.
CANDIDATE_RUN_COLUMNS = (
    "run_key, skill, skill_version, content_hash, model_profile, mode, verdict, git_sha, trigger_source, run_id, "
    "experiment_id, n_cases, pass_rate, error_rate, accuracy, started_at"
)
MAX_CANDIDATE_KEYS = 200  # candidates.MAX_KEYS pull request uploads and as many drafts


def candidate_runs_sql(
    cfg: HubConfig, dept: str, run_keys: list[str], skill: str | None = None
) -> tuple[str, dict[str, object]]:
    """Child-run rows of eval_runs whose run key starts with one of the candidate keys: each candidate's own PR eval
    and the Hub runs started for it (`<key>_h<hex>`), newest first. A key such as pr5_x_a1 also starts pr5_x_a10,
    so the caller keeps only the rows each candidate owns (backend.candidates.owns_run_key). eval_matrix keeps one
    row per skill version, and candidates have none, so this reads the runs table. Keys are parameters."""
    _check_dept(cfg, dept)
    keys = list(dict.fromkeys(run_keys))
    if not keys or len(keys) > MAX_CANDIDATE_KEYS:
        raise ValueError(f"between 1 and {MAX_CANDIDATE_KEYS} run keys")
    params: dict[str, object] = {f"k{i}": k for i, k in enumerate(keys)}
    match = " OR ".join(f"startswith(run_key, :k{i})" for i in range(len(keys)))
    where = f"parent_run_id IS NOT NULL AND ({match})"
    if skill is not None:
        where += " AND skill = :skill"
        params["skill"] = skill
    sql = (
        f"SELECT {CANDIDATE_RUN_COLUMNS} FROM {evals_schema(cfg, dept)}.eval_runs WHERE {where} "
        f"ORDER BY started_at DESC, run_id LIMIT {_FETCH}"
    )
    return sql, params
