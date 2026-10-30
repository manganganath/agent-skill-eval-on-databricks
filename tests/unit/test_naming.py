import pytest

from skillhub import naming as n


def test_schema_names(cfg):
    assert n.live_schema(cfg, "equity_research") == "skill_hub.dept_equity_research"
    assert n.versions_schema(cfg, "common") == "skill_hub.common_versions"
    assert n.evals_schema(cfg, "external") == "skill_hub.external_evals"
    assert n.eval_schema(cfg) == "skill_hub.eval"
    assert n.run_schema(cfg, "pr12_ab12cd3", "equity_research") == (
        "skill_hub.run_pr12_ab12cd3_dept_equity_research"
    )


def test_fqns_tables_and_names(cfg):
    assert n.live_fqn(cfg, "common", "audit-xls") == "skill_hub.common.audit-xls"
    assert n.version_fqn(cfg, "common", "audit-xls", 3) == "skill_hub.common_versions.audit-xls-v3"
    assert n.dataset_table(cfg, "equity_research", "earnings-preview") == (
        "skill_hub.dept_equity_research_evals.ds_earnings_preview"
    )
    assert n.experiment_path(cfg, "common", "audit-xls") == "/Shared/skill-hub/skill_hub/common/audit-xls"
    assert n.judge_name("rubric_c1", "gpt") == "rubric_c1__gpt"


def test_run_keys(cfg):
    assert n.make_run_key("pr12-ab12cd3") == "pr12_ab12cd3"
    assert n.make_run_key("Nightly-20261028") == "nightly_20261028"
    assert n.make_run_key("hub-3F9a/x") == "hub_3f9a_x"
    with pytest.raises(ValueError):
        n.make_run_key("---")
    assert n.make_run_key("x" * 50) == "x" * 40
    with pytest.raises(ValueError):
        n.run_schema(cfg, "Bad-Key", "common")
    with pytest.raises(ValueError):
        n.run_schema(cfg, n.make_run_key(""), "common")


def test_the_unreleased_experiment_cannot_collide_with_a_skill(cfg):
    path = n.unreleased_experiment_path(cfg, "common")
    assert path == "/Shared/skill-hub/skill_hub/common/_unreleased"
    assert n.experiment_parts(cfg, path) == ("common", None)  # its runs name their skill
    assert n.experiment_parts(cfg, n.experiment_path(cfg, "common", "audit-xls")) == ("common", "audit-xls")
    with pytest.raises(ValueError):
        n.check_skill_name(n.UNRELEASED)
