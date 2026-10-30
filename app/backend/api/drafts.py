"""Upload a draft skill. Validation and storage are in backend.drafts; visibility in backend.candidates."""

from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend import authz, candidates, drafts
from backend.api.skills import MAX_PARALLEL
from backend.authz import Reason
from backend.deps import get_app_client, get_cfg, get_user_client, problem
from skillhub.config import HubConfig

router = APIRouter(tags=["drafts"])


class DraftFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=drafts.MAX_PATH)
    content: str  # base64


class DraftIn(BaseModel):
    """A zip of the skill folder (`archive`, base64), or the folder's files one by one (`files`)."""

    model_config = ConfigDict(extra="forbid")
    dept: str = Field(min_length=1, max_length=64)
    archive: str | None = None
    files: list[DraftFileIn] | None = Field(default=None, max_length=drafts.MAX_FILES)


class DraftOut(BaseModel):
    run_key: str
    dept: str
    name: str
    has_rubric: bool


def invalid(errors: list[str]) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason": Reason.DRAFT_INVALID.value, "errors": errors})


async def _capped_body(request: Request) -> bytes:
    """The request body, refused with 413 as soon as it passes MAX_REQUEST_BYTES, before anything parses it."""
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > drafts.MAX_REQUEST_BYTES:
        raise problem(413, Reason.DRAFT_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > drafts.MAX_REQUEST_BYTES:
            raise problem(413, Reason.DRAFT_TOO_LARGE)
    return bytes(body)


def _upload(body: bytes, user_w: WorkspaceClient, app_w: WorkspaceClient, cfg: HubConfig) -> DraftOut:
    me = authz.fetch_me(user_w)
    if not candidates.may_see(me, cfg):  # authors and reviewers only; nothing else is read or written
        raise problem(403, Reason.NOT_AUTHOR)
    try:
        payload = DraftIn.model_validate_json(body)
    except ValidationError as e:
        raise invalid(
            [f"the request is not a draft upload: {err['msg']}" for err in e.errors()[:5]]
        ) from None
    if (payload.archive is None) == (payload.files is None):
        raise invalid(["send an archive or files, not both and not neither"])
    if payload.dept not in cfg.departments:
        raise invalid([f"unknown department {payload.dept!r}"])
    # The same rule as for seeing candidates: a reviewer may upload to any department, an author only to one whose
    # live skills they can read.
    if not authz.has_reviewer_role(me, cfg) and not candidates.reads_live(app_w, cfg, me, payload.dept):
        raise problem(403, Reason.DEPARTMENT_NOT_ALLOWED)
    try:
        if payload.archive is not None:
            try:
                archive = base64.b64decode(payload.archive, validate=True)
            except (binascii.Error, ValueError):
                raise drafts.DraftInvalid(["the archive is not base64"]) from None
            files = drafts.unzip(archive)
        else:
            files = drafts.from_files([(f.path, f.content) for f in payload.files or []])
        draft = drafts.build(cfg, payload.dept, files)
    except drafts.DraftInvalid as e:
        raise invalid(e.errors) from None
    except drafts.DraftTooLarge:
        raise problem(413, Reason.DRAFT_TOO_LARGE) from None
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        try:
            key = drafts.store(app_w, cfg, draft, me.user, pool)
        except drafts.DraftStoreFailed:
            raise problem(502, Reason.DRAFT_STORE_FAILED) from None
    return DraftOut(run_key=key.run_key, dept=draft.dept, name=draft.name, has_rubric=draft.has_rubric)


@router.post("/drafts", response_model=DraftOut, status_code=201)
async def upload_draft(
    request: Request,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
) -> DraftOut:
    """Store a draft skill for evaluation. Authors and reviewers only. 422 `draft_invalid` lists every problem
    found; 413 `draft_too_large`; 403 `department_not_allowed`; 502 `draft_store_failed`."""
    body = await _capped_body(request)
    return await run_in_threadpool(_upload, body, user_w, app_w, cfg)


class DiscardOut(BaseModel):
    discarded: str  # the draft's run key


@router.delete("/drafts/{run_key}", response_model=DiscardOut)
def discard_draft(
    run_key: str,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
) -> DiscardOut:
    """Remove a draft's files, as the app. Its uploader or a reviewer only: 403 `not_draft_owner` for anyone else,
    404 `candidate_not_found` for a draft that is gone or never was. Eval results of the draft stay in the
    eval tables, which are append-only."""
    key = candidates.parse_key(run_key)
    if key is None or key.kind != "draft":
        raise problem(404, Reason.CANDIDATE_NOT_FOUND)
    me = authz.fetch_me(user_w)
    meta = candidates.workspace_call(lambda: candidates.read_meta(app_w, cfg, key, cfg.departments))
    if meta is None:
        raise problem(404, Reason.CANDIDATE_NOT_FOUND)
    if not (candidates.is_uploader(meta, me) or authz.has_reviewer_role(me, cfg)):
        raise problem(403, Reason.NOT_DRAFT_OWNER)

    def delete() -> None:
        try:
            app_w.workspace.delete(candidates.folder(cfg, key), recursive=True)
        except NotFound:
            pass  # removed meanwhile (the cleanup, or a second click): the outcome is the same

    candidates.workspace_call(delete)
    return DiscardOut(discarded=key.run_key)
