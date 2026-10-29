"""AI/BI Genie space over the eval views: ask questions of eval results in plain English (C20).

The space reads eval.runs_all and eval.cases_all, which are reviewer-only. Share the space with
the reviewers group only.

The instructions and sample questions describe result columns. They are not prompts for skills and
contain no skill text.

Verify the serialized_space shape against `w.genie.get_space(id, include_serialized_space=True)` of a
UI-created space in the integration pass.
"""

from __future__ import annotations

import hashlib
import json
import logging

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

from skillhub.config import HubConfig
from skillhub.naming import eval_schema

log = logging.getLogger(__name__)
VIEWS = ("matrix_all", "runs_all", "cases_all")

INSTRUCTIONS = [
    "Each row of matrix_all is the latest evaluation of one skill version, model profile and mode. "
    "verdict is one of pass, warn, fail, inconclusive or advisory. advisory means too few reviewed cases "
    "to gate a merge; unreviewed_cases counts generated cases nobody has reviewed yet.",
    "pass_rate is the share of passing cases. For trigger runs it is accuracy. uplift_task_quality and "
    "uplift_skill_format are the gain from the skill on with_skill runs. model_class shows whether a model "
    "is frontier or fast; compare models only within a class.",
    "cases_all holds one row per trace assessment. For trigger questions use "
    "assessment_name = 'skill_trigger'. trigger_outcome is correct, correct_reject, wrong_skill, missed or "
    "false_trigger. expected_skill is the skill that should have been invoked and invoked_skill is the one "
    "that was.",
    "runs_all has one row per child run. parent_run_id is not null for child runs. Use started_at to "
    "order versions and nightly runs.",
]

# "{ev}" is replaced with the eval schema (catalog.eval) from naming.eval_schema.
SAMPLE_QUESTIONS = [
    (
        "Which skills failed in their latest run?",
        "SELECT skill, model_profile, mode, verdict, pass_rate FROM {ev}.matrix_all "
        "WHERE verdict = 'fail' ORDER BY started_at DESC",
    ),
    (
        "What is the task-quality uplift from each skill?",
        "SELECT skill, model_profile, uplift_task_quality, uplift_skill_format FROM {ev}.matrix_all "
        "WHERE mode = 'with_skill'",
    ),
    (
        "Which skills were invoked when they should not have been?",
        "SELECT expected_skill, invoked_skill, COUNT(*) AS n FROM {ev}.cases_all "
        "WHERE assessment_name = 'skill_trigger' AND trigger_outcome = 'false_trigger' "
        "GROUP BY expected_skill, invoked_skill ORDER BY n DESC",
    ),
    (
        "How many cases still need review, per skill?",
        "SELECT skill, SUM(unreviewed_cases) AS unreviewed FROM {ev}.matrix_all GROUP BY skill "
        "ORDER BY unreviewed DESC",
    ),
    (
        "Which models were inconclusive recently?",
        "SELECT model_profile, skill, started_at FROM {ev}.matrix_all WHERE verdict = 'inconclusive' "
        "ORDER BY started_at DESC",
    ),
]


def _id(text: str) -> str:
    """Deterministic 32-hex id, so the same config always serializes to the same payload."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:32]


def serialized_space(cfg: HubConfig) -> str:
    ev = eval_schema(cfg)
    payload = {
        "version": 2,  # VERIFY against a UI-created space (get_space(include_serialized_space=True))
        "data_sources": {"tables": [{"identifier": f"{ev}.{v}"} for v in sorted(VIEWS)]},
        "instructions": {
            # Genie may allow only one text instruction, so all of them go in one entry.
            "text_instructions": [{"id": _id("\n".join(INSTRUCTIONS)), "content": list(INSTRUCTIONS)}],
            "example_question_sqls": sorted(
                (
                    {"id": _id(q), "question": [q], "sql": [sql.replace("{ev}", ev)]}
                    for q, sql in SAMPLE_QUESTIONS
                ),
                key=lambda e: e["id"],
            ),
        },
    }
    return json.dumps(payload, sort_keys=True)


def space_title(cfg: HubConfig) -> str:
    return f"Skill Hub eval results ({cfg.catalog})"


def _find_space(w: WorkspaceClient, title: str) -> str | None:
    token = None
    while True:
        page = w.genie.list_spaces(page_token=token)
        for s in page.spaces or []:
            if s.title == title:
                return s.space_id
        token = page.next_page_token
        if not token:
            return None


def ensure_genie_space(cfg: HubConfig, w: WorkspaceClient) -> str:
    """Create the Genie space if missing and return its ID.

    Idempotent by title, and the title includes cfg.catalog, so two deployments in one workspace never
    share a space. A space that already exists is returned as is and is NOT modified, even if the payload
    changed. To change it, delete the space in the workspace and run this again.
    """
    title = space_title(cfg)
    existing = _find_space(w, title)
    if existing:
        return existing
    space = w.genie.create_space(
        warehouse_id=cfg.workspace.sql_warehouse_id,
        serialized_space=serialized_space(cfg),
        title=title,
        description="Questions over skill evaluation results: verdicts, uplift, trigger accuracy and reviews.",
    )
    return space.space_id


def hand_over_space(w: WorkspaceClient, space_id: str, owner: str) -> str:
    """Give the Genie space to `owner` (the ci principal), so the ci Job finds it and keeps its ACL.

    Tries IS_OWNER through the Permissions API (object type `genie`), then CAN_MANAGE. A refusal of both is a
    WARNING with the manual step, never a failure."""
    for level in (PermissionLevel.IS_OWNER, PermissionLevel.CAN_MANAGE):
        try:
            w.permissions.update(
                request_object_type="genie",
                request_object_id=space_id,
                access_control_list=[
                    AccessControlRequest(service_principal_name=owner, permission_level=level)
                ],
            )
            return f"Genie space {space_id}: {level.value} for {owner}"
        except DatabricksError as e:
            log.info("Genie space %s: %s for %s refused (%s)", space_id, level.value, owner, type(e).__name__)
    msg = (
        f"could not give Genie space {space_id} to {owner}. Manual step: open the space, choose Share, and give "
        "the ci service principal CAN MANAGE"
    )
    log.warning(msg)
    return f"warning: {msg}"
