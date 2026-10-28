import inspect
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound

from skillhub import SkillHubError
from skillhub.registry.uc_skills import UCSkills, UnsafePath


def test_create_posts_json_to_the_parent_schema():
    w = MagicMock()
    w.api_client.do.return_value = {"name": "skills/cat.sch.demo", "etag": "e1"}
    info = UCSkills(w).create("cat.sch", "demo")
    args, kw = w.api_client.do.call_args.args, w.api_client.do.call_args.kwargs
    assert args == ("POST", "/api/2.1/unity-catalog/skills")
    assert kw["query"] == {"parent": "schemas/cat.sch", "skill_id": "demo"}
    assert kw["headers"]["Content-Type"] == "application/json"
    assert (info.fqn, info.name, info.etag) == ("cat.sch.demo", "demo", "e1")


def test_patch_sends_json_and_etag_in_query():
    w = MagicMock()
    w.api_client.do.return_value = {"name": "skills/cat.sch.demo", "etag": "e2"}
    UCSkills(w).update_comment("cat.sch.demo", "hello", etag="OLD+/==")
    args, kw = w.api_client.do.call_args.args, w.api_client.do.call_args.kwargs
    assert args == ("PATCH", "/api/2.1/unity-catalog/skills/cat.sch.demo")
    assert kw["query"] == {"etag": "OLD+/==", "update_mask": "comment"}
    assert kw["headers"]["Content-Type"] == "application/json"
    assert "etag" not in kw["body"]


def test_delete_sends_etag_in_query_and_json_header():
    w = MagicMock()
    UCSkills(w).delete("cat.sch.demo", etag="E/=")
    args, kw = w.api_client.do.call_args.args, w.api_client.do.call_args.kwargs
    assert args == ("DELETE", "/api/2.1/unity-catalog/skills/cat.sch.demo")
    assert kw["query"] == {"etag": "E/="}
    assert kw["headers"]["Content-Type"] == "application/json"


def test_get_returns_none_when_missing():
    w = MagicMock()
    w.api_client.do.side_effect = NotFound("nope")
    assert UCSkills(w).get("cat.sch.demo") is None


def test_list_follows_page_tokens():
    w = MagicMock()
    w.api_client.do.side_effect = [
        {"skills": [{"name": "skills/cat.sch.a", "etag": "1"}], "next_page_token": "p2"},
        {"skills": [{"name": "skills/cat.sch.b", "etag": "2"}]},
    ]
    assert [s.name for s in UCSkills(w).list("cat.sch")] == ["a", "b"]
    assert w.api_client.do.call_args_list[1].kwargs["query"]["page_token"] == "p2"


def test_upload_uses_files_api_with_overwrite():
    w = MagicMock()
    UCSkills(w).upload_file("cat.sch.demo", "SKILL.md", b"x")
    args, kw = w.files.upload.call_args.args, w.files.upload.call_args.kwargs
    assert args[0] == "/Skills/cat/sch/demo/SKILL.md"
    assert args[1].read() == b"x"
    assert kw == {"overwrite": True}


def test_read_files_recurses_into_directories():
    w = MagicMock()
    root = "/Skills/cat/sch/demo"
    listing = {
        root: [
            SimpleNamespace(path=f"{root}/SKILL.md", is_directory=False),
            SimpleNamespace(path=f"{root}/references", is_directory=True),
        ],
        f"{root}/references": [SimpleNamespace(path=f"{root}/references/a.md", is_directory=False)],
    }
    data = {f"{root}/SKILL.md": b"S", f"{root}/references/a.md": b"A"}
    w.files.list_directory_contents.side_effect = lambda p: iter(listing[p])
    w.files.download.side_effect = lambda p: SimpleNamespace(contents=io.BytesIO(data[p]))
    assert UCSkills(w).read_files("cat.sch.demo") == {"SKILL.md": b"S", "references/a.md": b"A"}


def test_set_tag_creates_when_absent_and_updates_when_present():
    w = MagicMock()
    w.entity_tag_assignments.list.return_value = iter([])
    UCSkills(w).set_tag("cat.sch.demo", "version", "3")
    w.entity_tag_assignments.update.assert_not_called()
    created = w.entity_tag_assignments.create.call_args.args[0]
    assert (created.entity_type, created.entity_name, created.tag_key, created.tag_value) == (
        "skills",
        "cat.sch.demo",
        "version",
        "3",
    )

    w2 = MagicMock()
    w2.entity_tag_assignments.list.return_value = iter([SimpleNamespace(tag_key="version", tag_value="2")])
    UCSkills(w2).set_tag("cat.sch.demo", "version", "3")
    w2.entity_tag_assignments.create.assert_not_called()
    args, kw = (
        w2.entity_tag_assignments.update.call_args.args,
        w2.entity_tag_assignments.update.call_args.kwargs,
    )
    assert args[:3] == ("skills", "cat.sch.demo", "version")
    assert args[3].tag_value == "3" and kw == {"update_mask": "tag_value"}


def test_get_tags_lists_skills_entity():
    w = MagicMock()
    w.entity_tag_assignments.list.return_value = iter(
        [SimpleNamespace(tag_key="a", tag_value="1"), SimpleNamespace(tag_key="b", tag_value=None)]
    )
    assert UCSkills(w).get_tags("cat.sch.demo") == {"a": "1", "b": ""}
    w.entity_tag_assignments.list.assert_called_once_with("skills", "cat.sch.demo")


def test_fake_and_real_expose_the_same_methods():
    from tests.fakes import FakeUCSkills

    def surface(cls):
        return {
            n: [(p.name, p.kind) for p in list(inspect.signature(f).parameters.values())[1:]]
            for n, f in inspect.getmembers(cls, inspect.isfunction)
            if not n.startswith("_")
        }

    controls = {"fail_once", "tamper_after_finalize", "external_edit"}  # test-only helpers on the fake
    fake = {n: sig for n, sig in surface(FakeUCSkills).items() if n not in controls}
    assert fake == surface(UCSkills)


@pytest.mark.parametrize(
    "bad", ["", "/etc/passwd", "../x", "a/../b", "a//b", "..", "a/", "./a", "a/./b", "a\\b", "a\x00b", "a\nb"]
)
def test_upload_and_delete_file_reject_unsafe_paths(bad):
    w = MagicMock()
    uc = UCSkills(w)
    with pytest.raises(UnsafePath):
        uc.upload_file("cat.sch.demo", bad, b"x")
    with pytest.raises(UnsafePath):
        uc.delete_file("cat.sch.demo", bad)
    w.files.upload.assert_not_called()
    w.files.delete.assert_not_called()
    assert issubclass(UnsafePath, SkillHubError)


def test_read_files_returns_empty_only_when_the_root_is_missing():
    w = MagicMock()
    w.files.list_directory_contents.side_effect = NotFound("gone")
    assert UCSkills(w).read_files("cat.sch.demo") == {}


def test_read_files_propagates_not_found_during_the_walk():
    w = MagicMock()
    root = "/Skills/cat/sch/demo"
    w.files.list_directory_contents.side_effect = lambda p: iter(
        [SimpleNamespace(path=f"{root}/SKILL.md", is_directory=False)]
    )
    w.files.download.side_effect = NotFound("half-deleted")
    with pytest.raises(NotFound):
        UCSkills(w).read_files("cat.sch.demo")


def test_safe_relative_paths_reach_the_files_api_unchanged():
    w = MagicMock()
    for rel in ["SKILL.md", "scripts/a.py", "references/x.md"]:
        UCSkills(w).upload_file("cat.sch.demo", rel, b"x")
    assert [c.args[0] for c in w.files.upload.call_args_list] == [
        "/Skills/cat/sch/demo/SKILL.md",
        "/Skills/cat/sch/demo/scripts/a.py",
        "/Skills/cat/sch/demo/references/x.md",
    ]
