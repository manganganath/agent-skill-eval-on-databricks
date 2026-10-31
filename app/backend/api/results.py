from __future__ import annotations

import logging
import re
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, Query, Response

from backend import authz, dashboard, queries, skill_ref
from backend.authz import Reason
from backend.data import SqlDenied, SqlError, UserSql
from backend.deps import ci_eval_principal, get_cfg, get_sql, get_user_client, problem
from backend.schemas import DashboardOut
from skillhub.config import HubConfig

log = logging.getLogger("skillhub.app.results")
router = APIRouter(tags=["results"])
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")  # an MLflow run ID
DENIED_HEADER = "X-Eval-Denied-Departments"  # the departments left out of the result
TRUNCATED_HEADER = "X-Result-Truncated"


def _cap(rows: list[dict], response: Response) -> list[dict]:
    """Trim to the cap. The queries fetch one extra row, so an over-cap list means the result is cut."""
    if len(rows) > queries.RESULT_LIMIT:
        response.headers[TRUNCATED_HEADER] = "true"
        return rows[: queries.RESULT_LIMIT]
    return rows


def _read_departments(sql: UserSql, depts: list[str], build, response: Response, fallback=None) -> list[dict]:
    """Rows from each department's eval tables, tagged with the department.

    A refused department is left out and named in X-Eval-Denied-Departments. A table that does not exist yet
    (`not_found`) holds nothing and is not a refusal. An empty result with any refusal is a 403, so a user who
    cannot read a department never mistakes that for "no results".

    `fallback` builds a second statement for a department whose first one fails with SqlError: only that department
    is read again, so one department on an older view does not change how the others are read.
    """
    out: list[dict] = []
    refused: list[str] = []
    truncated = False
    for d in depts:
        try:
            try:
                rows = sql.query(*build(d))
            except SqlError:
                if fallback is None:
                    raise
                log.warning("read of %s failed; reading it with the fallback statement", d)
                rows = sql.query(*fallback(d))
        except SqlDenied as exc:
            if not exc.not_found:
                refused.append(d)
            continue
        truncated = truncated or len(rows) > queries.RESULT_LIMIT
        out.extend({**r, "dept": d} for r in rows[: queries.RESULT_LIMIT])
    if refused and not out:
        raise problem(403, Reason.EVALS_NOT_READABLE)
    if refused:
        response.headers[DENIED_HEADER] = ",".join(refused)
    if truncated:
        response.headers[TRUNCATED_HEADER] = "true"
    return out


@router.get("/results/matrix")
def matrix(
    response: Response,
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
    skill: Annotated[str | None, Query(max_length=128)] = None,
    dept: Annotated[str | None, Query(max_length=64)] = None,
    fqn: Annotated[str | None, Query(max_length=256)] = None,
) -> list[dict]:
    """Matrix rows. `fqn` (a live skill's `catalog.schema.skill`) sets both `dept` and `skill`, so a caller never
    has to turn a schema name into a department key."""
    if fqn:
        dept, skill = skill_ref.parse_fqn(cfg, fqn)  # an unknown schema or name is 404 skill_not_found
    # An unknown dept is rejected by queries._check_dept (UnknownDepartment, 404 department_not_found).
    depts = [dept] if dept else list(cfg.departments)
    # A view from before the model catalog has no profile_kind: that department is read without it.
    return _read_departments(
        sql,
        depts,
        lambda d: queries.matrix_sql(cfg, d, skill),
        response,
        fallback=lambda d: queries.matrix_sql(cfg, d, skill, legacy=True),
    )


@router.get("/results/cases")
def cases(
    response: Response,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
    run_id: Annotated[str, Query(max_length=64)],
) -> list[dict]:
    if not _RUN_ID.match(run_id):
        raise problem(422, Reason.INVALID_RUN_ID)
    if authz.is_reviewer(user_w, cfg):  # fails closed: an error counts as not a reviewer
        try:
            # One query across departments.
            return _cap(sql.query(*queries.cases_all_sql(cfg, run_id)), response)
        except SqlDenied:
            pass  # the view refused: fall back to the department tables
    return _read_departments(
        sql, list(cfg.departments), lambda d: queries.cases_sql(cfg, d, run_id), response
    )


def _dashboard_statement(sql: UserSql, statement: str, params: dict[str, object]) -> list[dict]:
    try:
        return sql.query(statement, params)
    except SqlDenied as exc:
        if exc.not_found:  # the table does not exist yet: nothing to count
            return []
        raise


def _read_dashboard(sql: UserSql, cfg: HubConfig, response: Response) -> dict[str, list[dict]]:
    """The Dashboard's aggregate rows from every department the user can read, tagged with the department.

    A department is read whole or not at all: one refused statement leaves it out and names it in
    X-Eval-Denied-Departments. As in _read_departments, a refusal with no matrix rows read is a 403.
    """
    out: dict[str, list[dict]] = {}
    refused: list[str] = []
    for d in cfg.departments:
        try:
            section = {
                name: _dashboard_statement(sql, statement, params)
                for name, (statement, params) in queries.dashboard_sql(cfg, d).items()
            }
        except SqlDenied:
            refused.append(d)
            continue
        journal = section.pop("journal")
        cells = dashboard.live_rows(section.pop("cells"), journal)
        section["uplift"] = dashboard.live_rows(section["uplift"], journal)
        section["summary"] = [dashboard.department_summary(cells)]
        section["verdicts"] = dashboard.verdict_counts(cells)
        section["health"] = dashboard.health(cells)
        for name, rows in section.items():
            out.setdefault(name, []).extend({**r, "dept": d} for r in rows)
    if refused and not out.get("verdicts"):
        raise problem(403, Reason.EVALS_NOT_READABLE)
    if refused:
        response.headers[DENIED_HEADER] = ",".join(refused)
    return out


def _tokens(sql: UserSql, cfg: HubConfig) -> list[dict] | None:
    """Tokens per day and source of the configured serving endpoints. None when the system.serving tables cannot
    be read."""
    profiles = {m.endpoint: name for name, m in cfg.models.items() if m.route == "serving" and m.endpoint}
    if not profiles:
        return []
    try:
        rows = sql.query(*queries.tokens_sql(sorted(profiles), ci_eval_principal()))
    except (SqlDenied, SqlError):
        return None
    except Exception as exc:  # any other failure of this optional read leaves the rest of the page answering
        log.warning("token usage read failed: %s", type(exc).__name__)
        return None
    return [{**r, "model_profile": profiles.get(r["endpoint_name"])} for r in rows]


@router.get("/results/dashboard", response_model=DashboardOut)
def dashboard_results(
    response: Response,
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> dict:
    """Aggregates for the Dashboard tab, over the departments the user can read. Denied departments are named in
    X-Eval-Denied-Departments; `tokens` is null when the user cannot read the system.serving tables."""
    rows = _read_dashboard(sql, cfg, response)
    return {
        "summary": dashboard.summary(rows.get("summary", []), rows.get("verdicts", [])),
        "verdicts": rows.get("verdicts", []),
        "uplift": rows.get("uplift", []),
        "routing": rows.get("routing", []),
        "trigger_trend": rows.get("trigger_trend", []),
        "pass_rate_trend": rows.get("pass_rate_trend", []),
        "health": rows.get("health", []),
        "tokens": _tokens(sql, cfg),
    }
