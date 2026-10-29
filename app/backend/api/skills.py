from __future__ import annotations

import json
import logging
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from fastapi import APIRouter, Depends, Query

from backend import authz, queries, skill_ref
from backend.authz import Forbidden, Reason
from backend.data import SqlDenied, SqlError, UserSql
from backend.deps import get_app_client, get_cfg, get_sql, get_user_client, problem
from backend.schemas import EvalAccess, JournalRowOut, SkillDetail, SkillSummary
from skillhub.config import HubConfig
from skillhub.evalspec import EvalCase, EvalFile, Rubric
from skillhub.naming import live_schema
from skillhub.registry.uc_skills import SkillInfo, UCSkills

log = logging.getLogger("skillhub.app.skills")
router = APIRouter(tags=["skills"])
# Reads that do not depend on one another run side by side on a small pool that lives for one request: the
# departments of a catalog load, and the files and eval tables of a skill page. Each read still runs with the
# same client as before (the user's, or the app's only after the effective-permissions check), so running
# them at once changes no answer.
MAX_PARALLEL = 8
_ACCESS_ORDER: tuple[EvalAccess, ...] = ("ok", "denied", "unavailable")


def _read_dept(
    sql: UserSql, statement: tuple[str, dict[str, object]], dept: str
) -> tuple[list[dict], EvalAccess]:
    """Rows from one department's eval tables, plus whether the user could read them.

    A denial or a SQL failure only marks this department. A missing table means nothing is recorded yet.
    """
    try:
        return sql.query(*statement), "ok"
    except SqlDenied as exc:
        return [], "ok" if exc.not_found else "denied"
    except SqlError:
        log.warning("eval tables unavailable for department %s", dept)
        return [], "unavailable"


def _worst(*accesses: EvalAccess) -> EvalAccess:
    return max(accesses, key=_ACCESS_ORDER.index)


LiveReads = tuple[Future, Future]
LiveState = tuple[dict[str, int | None], dict[str, dict[str, str]], EvalAccess]


def _start_live_reads(
    pool: ThreadPoolExecutor, sql: UserSql, cfg: HubConfig, dept: str, skill: str | None = None
) -> LiveReads:
    """Start the journal and verdict reads for the department (or one skill in it), side by side."""
    return (
        pool.submit(_read_dept, sql, queries.journal_sql(cfg, dept, skill), dept),
        pool.submit(_read_dept, sql, queries.catalog_verdicts_sql(cfg, dept, skill), dept),
    )


def _live_state(reads: LiveReads) -> LiveState:
    """({skill: live version}, {skill: {model_profile: verdict}}, access) from the two reads.

    The catalog and the skill page both use this, so they always agree.
    """
    (journal, access), (matrix, matrix_access) = (read.result() for read in reads)
    by_skill: dict[str, list[dict]] = {}
    for r in journal:
        by_skill.setdefault(r["skill"], []).append(r)
    live = {k: skill_ref.live_version(v) for k, v in by_skill.items()}
    return live, skill_ref.live_verdicts(matrix, live), _worst(access, matrix_access)


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[SkillSummary]:
    needle = q.lower() if q else None
    out: list[SkillSummary] = []
    me = authz.CachedMe(user_w)  # one identity lookup per request, not per department
    memo = authz.RequestMemo()  # one catalog-level permissions lookup per request, not per department
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:

        def visible(dept: str) -> tuple[list[SkillInfo], LiveReads | None]:
            infos = authz.list_visible_skills(user_w, app_w, live_schema(cfg, dept), me, memo)
            # Only a department with skills to show has its eval tables read, as soon as its listing is in. The
            # reads go on the pool and this task ends, so it never waits for a worker.
            return infos, _start_live_reads(pool, sql, cfg, dept) if infos else None

        listings = [(dept, pool.submit(visible, dept)) for dept in cfg.departments]
        for dept, listing in listings:  # config order, as before
            try:
                infos, reads = listing.result()
            except authz.Forbidden:  # fail closed for this department only; the others still load
                log.warning("skipped department %s: its access could not be checked", dept)
                continue
            if reads is None:
                continue
            live, verdicts, access = _live_state(reads)
            for info in infos:
                name = info.fqn.rsplit(".", 1)[1]
                if needle and needle not in name.lower() and needle not in (info.description or "").lower():
                    continue
                out.append(
                    SkillSummary(
                        fqn=info.fqn,
                        dept=dept,
                        name=name,
                        description=info.description,
                        live_version=live.get(name),
                        verdicts=verdicts.get(name, {}),
                        eval_access=access,
                    )
                )
    return out


def _decode_text(files: dict[str, bytes]) -> dict[str, str]:
    text: dict[str, str] = {}
    for path, data in sorted(files.items()):
        if path.startswith("evals/"):
            continue  # cases and rubrics are never stored in skill files; anything under evals/ stays hidden
        try:
            text[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary files are not shown
    return text


def _json(value: object) -> dict:
    """The statement API returns complex columns as JSON text. A dict is accepted as well."""
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


def _expectations(value: object) -> dict:
    """Expectations by name. The dataset table stores each one as {"value": ...}; a bare value is read as is."""
    return {k: v["value"] if isinstance(v, dict) and "value" in v else v for k, v in _json(value).items()}


def _case(row: dict) -> EvalCase | None:
    """One ds_<skill> row (the eval dataset layout) as an EvalCase. A malformed row is skipped and logged."""
    try:
        inputs, exp, tags = (
            _json(row.get("inputs")),
            _expectations(row.get("expectations")),
            _json(row.get("tags")),
        )
        return EvalCase(
            id=int(inputs["case_id"]),
            kind=tags.get("kind", "trigger"),
            prompt=inputs["prompt"],
            should_trigger=bool(exp["should_trigger"]),
            expected_skill=exp.get("expected_skill"),
            assertions=list(exp.get("guidelines") or []),
            expected_tool_calls=exp.get("expected_tool_calls"),
            origin=tags.get("origin", "human"),
            reviewed=bool(exp.get("reviewed", False)),
        )
    except (KeyError, TypeError, ValueError):  # a pydantic ValidationError is a ValueError
        log.warning("skipped a malformed evaluation dataset row")
        return None


def _dataset_cases(sql: UserSql, cfg: HubConfig, dept: str, name: str) -> tuple[EvalFile | None, EvalAccess]:
    """Test cases from the eval dataset table ds_<skill>, read as the user."""
    try:
        try:
            rows = sql.query(*queries.dataset_sql(cfg, dept, name))
        except SqlError:  # the table may have no update-time column: read without it
            rows = sql.query(*queries.dataset_plain_sql(cfg, dept, name))
    except SqlDenied as exc:
        return None, "ok" if exc.not_found else "denied"  # no table yet: nothing generated
    except SqlError:  # a stopped or slow warehouse: the skill itself is still shown
        log.warning("eval dataset unavailable for department %s", dept)
        return None, "unavailable"
    if not rows:
        return None, "ok"
    # Records are keyed by their inputs, so an edited prompt leaves a second row with the same case_id. EvalFile
    # refuses repeated ids; keep the newest row of each id (the latest update time, then the larger prompt, so
    # the choice never depends on table order). Runs always take cases from evals.json.
    parsed = [(_case(r), str(r.get("last_update_time") or "")) for r in rows]
    cases = [c for c, _ in parsed if c is not None]
    best: dict[int, tuple[tuple[str, str], EvalCase]] = {}
    for case, stamp in parsed:
        if case is None:
            continue
        key = (stamp, case.prompt)
        if case.id not in best or key > best[case.id][0]:
            best[case.id] = (key, case)
    by_id = {i: c for i, (_, c) in best.items()}
    repeated = sum(1 for n in Counter(c.id for c in cases).values() if n > 1)
    if repeated:
        log.warning(
            "the eval dataset of %s/%s repeats %d case id(s); one row each is shown", dept, name, repeated
        )
    return EvalFile(skill_name=name, evals=[by_id[i] for i in sorted(by_id)]), "ok"


def _latest_rubric(sql: UserSql, cfg: HubConfig, dept: str, name: str) -> tuple[Rubric | None, EvalAccess]:
    """The newest row of {evals}.rubrics for the skill, read as the user."""
    try:
        rows = sql.query(*queries.rubric_sql(cfg, dept, name))
    except SqlDenied as exc:
        return None, "ok" if exc.not_found else "denied"
    except SqlError:
        log.warning("rubrics unavailable for department %s", dept)
        return None, "unavailable"
    if not rows:
        return None, "ok"
    try:
        return Rubric.model_validate_json(rows[0]["rubric_json"]), "ok"
    except (TypeError, ValueError):
        log.warning("a stored rubric could not be parsed")
        return None, "ok"


def _authorize_or_404(user_w: WorkspaceClient, app_w: WorkspaceClient, fqn: str):
    """(skill, client that may read its files). The app's client only after the effective-permissions fallback."""
    try:
        return authz.authorize_read_as(user_w, app_w, fqn)
    except Forbidden as exc:
        if exc.reason is Reason.SKILL_NOT_READABLE:
            raise problem(404, Reason.SKILL_NOT_FOUND) from None
        raise


@router.get("/skills/{fqn}", response_model=SkillDetail)
def skill_detail(
    fqn: str,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> SkillDetail:
    dept, name = skill_ref.parse_fqn(cfg, fqn)
    info, reader = _authorize_or_404(user_w, app_w, fqn)  # nothing is read before the user may read the skill
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        # The eval-table reads, as the user, run while the files are read.
        live_reads = _start_live_reads(pool, sql, cfg, dept, name)
        cases_read = pool.submit(_dataset_cases, sql, cfg, dept, name)  # never from the skill files
        rubric_read = pool.submit(_latest_rubric, sql, cfg, dept, name)
        try:
            # SKILL.md and other text files, read as the user. When the user token cannot call the skills API,
            # the fallback has already checked the user's READ VOLUME, and the app reads the files.
            files = UCSkills(reader).read_files(fqn)
        except (NotFound, PermissionDenied):
            raise problem(404, Reason.SKILL_NOT_FOUND) from None
        except Exception as exc:
            log.warning("reading skill files failed: %s", type(exc).__name__)
            raise problem(502, Reason.UC_FAILED) from None
        evals, evals_access = cases_read.result()
        rubric, rubric_access = rubric_read.result()
        live, verdicts, live_access = _live_state(live_reads)
    return SkillDetail(
        fqn=fqn,
        dept=dept,
        name=name,
        description=info.description,
        live_version=live.get(name),
        verdicts=verdicts.get(name, {}),
        files=_decode_text(files),
        evals=evals,
        rubric=rubric,
        eval_access=_worst(evals_access, rubric_access, live_access),
    )


@router.get("/skills/{fqn}/versions")
def skill_versions(
    fqn: str,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> list[JournalRowOut]:
    dept, name = skill_ref.parse_fqn(cfg, fqn)
    _authorize_or_404(user_w, app_w, fqn)
    try:
        rows = sql.query(*queries.journal_sql(cfg, dept))
    except SqlDenied as exc:
        if exc.not_found:  # no skill_versions table yet: nothing has been published
            return []
        raise Forbidden(Reason.EVALS_NOT_READABLE) from None
    return [JournalRowOut.model_validate(r) for r in rows if r["skill"] == name]
