import os

import pytest

pytestmark = pytest.mark.integration
requires_it = pytest.mark.skipif(
    os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1 (dev workspace)"
)


@requires_it
def test_job_ids_resolve_by_name():
    from backend import jobs

    from skillhub.auth import workspace_client

    jobs.clear_cache()
    w = workspace_client()  # DATABRICKS_CONFIG_PROFILE selects the dev workspace
    assert isinstance(jobs.job_id(w, "skill-eval"), int)
    assert isinstance(jobs.job_id(w, "skill-switch"), int)


@requires_it
def test_user_without_access_is_refused():
    # Tokens come from the environment, never from the repo.
    from backend import authz
    from backend.authz import Forbidden
    from backend.deps import user_client

    from skillhub.auth import workspace_client

    token = os.environ["SKILLHUB_IT_NO_ACCESS_TOKEN"]
    fqn = os.environ["SKILLHUB_IT_RESTRICTED_FQN"]
    with pytest.raises(Forbidden):
        authz.authorize_read(user_client(token), workspace_client(), fqn)


@requires_it
def test_dataset_rows_parse_for_a_registered_skill():
    # The ds_<skill> columns decode the way the skill detail screen expects them to.
    from backend import queries, skill_ref
    from backend.api.skills import _case
    from backend.data import UserSql
    from backend.deps import get_cfg, user_client

    from skillhub.sql import SqlClient

    cfg = get_cfg()
    dept, name = skill_ref.parse_fqn(cfg, os.environ["SKILLHUB_IT_SKILL_FQN"])
    sql = UserSql(
        SqlClient(
            user_client(os.environ["SKILLHUB_IT_USER_TOKEN"]), cfg.workspace.sql_warehouse_id, timeout_s=60
        )
    )
    rows = sql.query(*queries.dataset_sql(cfg, dept, name))
    assert rows and all(_case(r) is not None for r in rows)


@requires_it
def test_try_scope_is_user():
    # Review focus 5: the UG skills server offers a user only the skills that user can read.
    from backend.deps import get_cfg

    from skillhub.agent.loop import SkillsBridge
    from skillhub.gateway.skills_mcp import SkillsMcpClient

    token = os.environ["SKILLHUB_IT_NO_ACCESS_TOKEN"]
    fqn = os.environ["SKILLHUB_IT_RESTRICTED_FQN"]
    schema = fqn.rsplit(".", 1)[0]
    client = SkillsMcpClient(get_cfg().workspace.host, [schema], lambda: {"Authorization": f"Bearer {token}"})
    with SkillsBridge(client) as bridge:
        names = [t.name for t in bridge.list_tools()]
    assert names, "the skills server offered no tools at all, so the check proves nothing"
    assert all(fqn not in name for name in names)
