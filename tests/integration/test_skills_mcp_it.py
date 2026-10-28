import os

import pytest

from skillhub.auth import auth_headers
from skillhub.config import load_config
from skillhub.gateway.skills_mcp import SkillsMcpClient

pytestmark = pytest.mark.integration


@pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="needs SKILLHUB_IT=1 and a dev workspace")
async def test_lists_skills_and_hides_authoring_tools():
    cfg = load_config()
    schemas = [f"{cfg.catalog}.common"]
    async with SkillsMcpClient(cfg.workspace.host, schemas, auth_headers) as c:
        names = [t.name for t in await c.list_tools()]
    assert not {"create_skill", "update_skill", "delete_skill"} & set(names)
    assert any(n.startswith("skill_") for n in names) or "list_skills" in names
