from __future__ import annotations

import re
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, Query, Response

from backend import authz, queries, skill_ref
from backend.authz import Reason
from backend.data import SqlDenied, UserSql
from backend.deps import get_cfg, get_sql, get_user_client, problem
from skillhub.config import HubConfig

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


def _read_departments(sql: UserSql, depts: list[str], build, response: Response) -> list[dict]:
    """Rows from each department's eval tables, tagged with the department.

    A refused department is left out and named in X-Eval-Denied-Departments. A table that does not exist yet
    (`not_found`) holds nothing and is not a refusal. An empty result with any refusal is a 403, so a user who
    cannot read a department never mistakes that for "no results".
    """
    out: list[dict] = []
    refused: list[str] = []
    truncated = False
    for d in depts:
        try:
            rows = sql.query(*build(d))
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
    return _read_departments(sql, depts, lambda d: queries.matrix_sql(cfg, d, skill), response)


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
