import os
import uuid

import pytest

from skillhub.auth import workspace_client
from skillhub.registry.uc_skills import UCSkills

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch_schema():
    schema = os.environ.get("SKILLHUB_IT_SCHEMA")  # "<catalog>.<schema>" the test may write to
    if not schema:
        pytest.skip("set SKILLHUB_IT_SCHEMA=<catalog>.<schema>")
    return schema


def test_create_upload_finalize_read_tag_delete(scratch_schema):
    uc = UCSkills(workspace_client())
    skill_id = f"it-{uuid.uuid4().hex[:8]}"
    fqn = f"{scratch_schema}.{skill_id}"
    uc.create(scratch_schema, skill_id, comment="integration test")
    try:
        uc.upload_file(fqn, "SKILL.md", f"---\nname: {skill_id}\ndescription: it\n---\nbody\n".encode())
        assert uc.finalize(fqn).description == "it"
        before = uc.get(fqn).etag
        uc.upload_file(fqn, "references/a.md", b"x")
        print("etag before and after a file upload (D1 open question):", before, uc.get(fqn).etag)
        assert set(uc.read_files(fqn)) == {"SKILL.md", "references/a.md"}
        uc.set_tag(fqn, "version", "1")
        assert uc.get_tags(fqn)["version"] == "1"
    finally:
        uc.delete(fqn)
    assert uc.get(fqn) is None
