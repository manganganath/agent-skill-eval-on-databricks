"""The Models page: the model catalog, its connection tests, and the serving endpoints a reviewer can pick from.

Everyone signed in reads the list. Adding, editing, testing, enabling and disabling are for reviewers only (403
not_reviewer); the app then writes as its principal. A row is disabled, never deleted. A row can be enabled only
after its last test passed, and changing what it points at disables it and clears that test.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, InvalidParameterValue, NotFound, PermissionDenied
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend import authz, jobs
from backend import model_catalog as catalog
from backend.api.runs import _jobs_call
from backend.authz import Reason
from backend.deps import get_app_client, get_app_sql, get_cfg, get_user_client, get_user_token, problem
from backend.schemas import ModelOut, ModelsOut, ProbeStarted, ProbeStatus, ServingEndpointOut
from skillhub.config import HubConfig
from skillhub.model_catalog import store
from skillhub.model_catalog.entry import CatalogEntry, ConnectionTest, Target, clashes, problems
from skillhub.sql import SqlExecutor

log = logging.getLogger("skillhub.app.models")
router = APIRouter(tags=["models"])
PROBE_JOB = "skillhub-probe"
ROUTE_OF_KIND = {"ug_model": "serving", "external_model": "bedrock"}


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=64)
    kind: str
    route: str | None = None  # default: serving for ug_model, bedrock for external_model
    endpoint: str | None = Field(default=None, max_length=200)
    model_id: str | None = Field(default=None, max_length=256)
    region: str | None = Field(
        default=None, max_length=32
    )  # default: hub.yaml external.bedrock.default_region
    model_class: Literal["frontier", "fast"]
    price_in: float | None = None
    price_out: float | None = None


class ModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # name, kind, route and harness are fixed
    endpoint: str | None = Field(default=None, max_length=200)
    model_id: str | None = Field(default=None, max_length=256)
    region: str | None = Field(default=None, max_length=32)
    model_class: Literal["frontier", "fast"] | None = None
    price_in: float | None = None
    price_out: float | None = None
    enabled: bool | None = None


def _invalid(errors: list[str]) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason": Reason.MODEL_INVALID.value, "errors": errors})


def _parse(model: type[BaseModel], body: dict) -> BaseModel:
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise _invalid(
            [f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in e.errors()]
        ) from None


def _catalog_row(entries: list[CatalogEntry], cfg: HubConfig, name: str) -> CatalogEntry:
    if name in cfg.models:
        raise problem(409, Reason.RELEASE_GATE_MODEL)  # changed only in hub.yaml
    entry = catalog.find(entries, name)
    if entry is None:
        raise problem(404, Reason.MODEL_NOT_FOUND)
    return entry


@router.get("/models", response_model=ModelsOut, dependencies=[Depends(get_user_token)])
def list_models(
    cfg: Annotated[HubConfig, Depends(get_cfg)], sql: Annotated[SqlExecutor, Depends(get_app_sql)]
) -> ModelsOut:
    try:
        entries, available = catalog.read(sql, cfg), True
    except catalog.CatalogUnavailable:
        entries, available = [], False
    return ModelsOut(
        models=[*catalog.hub_rows(cfg), *catalog.catalog_rows(entries)],
        catalog_available=available,
        bedrock_region=cfg.external.bedrock.default_region,
    )


@router.post("/models", response_model=ModelOut, status_code=201)
def add_model(
    body: Annotated[dict, Body()],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[SqlExecutor, Depends(get_app_sql)],
) -> ModelOut:
    actor = authz.require_reviewer(user_w, cfg)
    req = _parse(ModelIn, body)
    if req.kind == "external_agent":
        raise problem(422, Reason.KIND_NOT_SUPPORTED)
    if req.kind not in ROUTE_OF_KIND:
        raise _invalid(["kind: use ug_model or external_model"])
    region = req.region or (cfg.external.bedrock.default_region if req.kind == "external_model" else None)
    fields = req.model_dump(exclude={"route", "region"})
    entry = CatalogEntry(**fields, route=req.route or ROUTE_OF_KIND[req.kind], region=region, enabled=False)
    found = problems(entry)
    if found:
        raise _invalid(found)
    if clashes(entry.name, cfg.models):
        raise problem(409, Reason.MODEL_EXISTS)
    if not catalog.write_or_503(lambda: store.insert_entry(sql, cfg, entry, actor, datetime.now(UTC))):
        raise problem(409, Reason.MODEL_EXISTS)
    return catalog.entry_out(catalog.find(catalog.read_or_503(sql, cfg), entry.name) or entry)


@router.patch("/models/{name}", response_model=ModelOut)
def update_model(
    name: Annotated[str, Path(max_length=64)],
    body: Annotated[dict, Body()],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[SqlExecutor, Depends(get_app_sql)],
) -> ModelOut:
    actor = authz.require_reviewer(user_w, cfg)
    patch = _parse(ModelPatch, body)
    entry = _catalog_row(catalog.read_or_503(sql, cfg), cfg, name)
    if not entry.runnable:
        raise problem(422, Reason.KIND_NOT_SUPPORTED)
    changed = entry.model_copy(update={k: getattr(patch, k) for k in patch.model_fields_set})
    retarget = changed.target() != entry.target()
    if retarget:  # the last test proved another target: it no longer counts
        changed = changed.model_copy(update={"enabled": False, "tested_at": None, "test_result": None})
    if patch.enabled and (retarget or (entry.last_test is None or entry.last_test.status != "passed")):
        raise problem(409, Reason.TEST_NOT_PASSED)
    found = problems(changed)
    if found:
        raise _invalid(found)
    catalog.write_or_503(
        lambda: store.update_entry(sql, cfg, changed, actor, datetime.now(UTC), clear_test=retarget)
    )
    return catalog.entry_out(catalog.find(catalog.read_or_503(sql, cfg), name) or changed)


@router.post("/models/{name}/test", response_model=ProbeStarted)
def test_model(
    name: Annotated[str, Path(max_length=64)],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[SqlExecutor, Depends(get_app_sql)],
) -> ProbeStarted:
    actor = authz.require_reviewer(user_w, cfg)
    entry = _catalog_row(catalog.read_or_503(sql, cfg), cfg, name)
    if not entry.runnable:
        raise problem(422, Reason.KIND_NOT_SUPPORTED)
    # Every parameter is sent; "" means unset. The probe refuses a row whose target is no longer this one.
    params = {"name": entry.name, **entry.target().params(), "requested_by": actor}
    return ProbeStarted(job_run_id=_jobs_call(lambda: jobs.start_job(app_w, PROBE_JOB, params)))


@router.get("/models/{name}/test/{job_run_id}", response_model=ProbeStatus)
def test_status(
    name: Annotated[str, Path(max_length=64)],
    job_run_id: Annotated[int, Path(gt=0)],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[SqlExecutor, Depends(get_app_sql)],
) -> ProbeStatus:
    """The state of a connection test. When it has finished, its result is recorded on the row, if the row still
    points at the target that was tested (else `stale`). The record is written once, however often this is asked."""
    authz.require_reviewer(user_w, cfg)
    try:
        run = app_w.jobs.get_run(run_id=job_run_id)
        probe_job = jobs.job_id(app_w, PROBE_JOB)
    except (NotFound, InvalidParameterValue, PermissionDenied, jobs.JobNotFound):
        raise problem(404, Reason.RUN_NOT_FOUND) from None
    except DatabricksError as exc:
        log.warning("probe run lookup failed: %s", type(exc).__name__)
        raise problem(502, Reason.JOB_FAILED) from None
    params = {p.name: p.value for p in (run.job_parameters or [])}
    if getattr(run, "job_id", None) != probe_job or params.get("name") != name:
        raise problem(404, Reason.RUN_NOT_FOUND)
    if not catalog.is_finished(run):
        return ProbeStatus(state="running")
    status, message = catalog.outcome(app_w, run)
    tested_at = catalog.finished_at(run)
    entry = catalog.find(catalog.read_or_503(sql, cfg), name)
    tested = Target.from_params(params)
    if entry is None or entry.target() != tested:
        return ProbeStatus(state="stale", message="The model changed while it was tested. Test it again.")
    result = ConnectionTest(status, message, job_run_id).to_json()
    catalog.write_or_503(lambda: store.record_test(sql, cfg, name, tested, result, tested_at))
    return ProbeStatus(state=status, message=message, tested_at=tested_at.isoformat())


@router.get("/serving-endpoints", response_model=list[ServingEndpointOut])
def serving_endpoints(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)], cfg: Annotated[HubConfig, Depends(get_cfg)]
) -> list[ServingEndpointOut]:
    """The chat endpoints the reviewer can see, read with the reviewer's own token, for a new model on Unity Gateway.
    Whether the eval service principal may query one is what the connection test proves."""
    authz.require_reviewer(user_w, cfg)
    try:
        endpoints = list(user_w.serving_endpoints.list())
    except DatabricksError as exc:
        log.warning("serving endpoint list failed: %s", type(exc).__name__)
        raise problem(502, Reason.SERVING_UNAVAILABLE) from None
    chat = [e for e in endpoints if e.name and (e.task is None or e.task.endswith("/chat"))]
    return sorted((ServingEndpointOut(name=e.name, task=e.task) for e in chat), key=lambda e: e.name)
