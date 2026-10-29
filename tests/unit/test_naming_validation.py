"""Names that reach SQL, REST paths and file paths are validated."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillhub import cli
from skillhub import naming as n
from skillhub.bundle import BundleError, discover_bundles

CONFIG = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"
BAD_DEPTS = ["", "Dept", "a-b", "x y", "x;DROP", "common\n", "../x", "a.b"]
BAD_SKILLS = ["", "-x", "_x", "Foo", "a/b", "../x", "a.b", "x\n", "x y", "a" * 65, "x'"]


@pytest.mark.parametrize("dept", BAD_DEPTS)
def test_schema_builders_refuse_a_bad_department(cfg, dept):
    for build in (n.live_schema, n.versions_schema, n.evals_schema):
        with pytest.raises(ValueError, match="invalid department"):
            build(cfg, dept)


@pytest.mark.parametrize("skill", BAD_SKILLS)
def test_skill_builders_refuse_a_bad_name(cfg, skill):
    for build in (lambda: n.live_fqn(cfg, "common", skill), lambda: n.version_fqn(cfg, "common", skill, 1)):
        with pytest.raises(ValueError, match="invalid skill name"):
            build()
    with pytest.raises(ValueError):
        n.dataset_table(cfg, "common", skill)


def test_good_names_and_version_numbers(cfg):
    assert n.live_fqn(cfg, "equity_research", "a1_b-c") == "skill_hub.dept_equity_research.a1_b-c"
    assert n.version_fqn(cfg, "common", "x" * 64, 2).endswith("-v2")
    for bad in (0, -1, True, "2"):
        with pytest.raises(ValueError, match="invalid version"):
            n.version_fqn(cfg, "common", "x", bad)


def test_run_key_with_a_trailing_newline_is_refused(cfg):
    with pytest.raises(ValueError):
        n.run_schema(cfg, "pr1_ab\n", "common")


def test_discover_bundles_refuses_unsafe_folder_names(tmp_path):
    for dept, name in (("Bad Dept", "ok"), ("common", "Bad.Name")):
        root = tmp_path / dept / name
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
        with pytest.raises(BundleError, match="invalid"):
            discover_bundles(tmp_path)
        (root / "SKILL.md").unlink()
        root.rmdir()


@pytest.mark.parametrize(
    "args",
    [
        ["job-switch", "--action", "retire", "--dept", "x;DROP TABLE t", "--skill", "foo"],
        ["job-switch", "--action", "retire", "--dept", "rogue", "--skill", "foo"],
        ["job-switch", "--action", "retire", "--dept", "common", "--skill", "../../x"],
        ["retire", "--dept", "common", "--skill", "a/b"],
        ["switch", "--dept", "nope", "--skill", "foo", "--to", "1"],
        ["publish", "--dept", "common", "--skill", "x'", "--git-sha", "a", "--eval-run-id", "r"],
    ],
)
def test_registry_commands_refuse_bad_names_with_exit_2_before_any_call(monkeypatch, args):
    def boom(cfg):
        raise AssertionError("no service may be created for a bad name")

    monkeypatch.setattr(cli, "_services", boom)
    for k, v in (
        ("DATABRICKS_HOST", "https://x.invalid"),
        ("SQL_WAREHOUSE_ID", "w"),
        ("SKILLHUB_CATALOG", "c"),
    ):
        monkeypatch.setenv(k, v)
    r = CliRunner().invoke(cli.app, ["--config", str(CONFIG), *args, "--actor", "ci"])
    assert r.exit_code == 2, r.output


def test_uc_skills_refuses_unsafe_names_before_any_request():
    from unittest.mock import MagicMock

    from skillhub.registry.uc_skills import UCSkills, UnsafePath

    w = MagicMock()
    uc = UCSkills(w)
    calls = [
        lambda: uc.get("cat.sch.x/../y"),
        lambda: uc.delete("cat.sch"),
        lambda: uc.read_files("cat.sch.x?etag=1"),
        lambda: uc.create("cat.sch", "../x"),
        lambda: uc.list("cat.sch.x"),
        lambda: uc.upload_file("cat.sch.X", "SKILL.md", b""),
        lambda: uc.finalize("cat.sch.x\n"),
        lambda: uc.ensure_schema("cat.sch;x"),
    ]
    for call in calls:
        with pytest.raises(UnsafePath):
            call()
    w.api_client.do.assert_not_called()
    w.files.upload.assert_not_called()
    w.schemas.get.assert_not_called()
