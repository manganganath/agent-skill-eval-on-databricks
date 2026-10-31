from pathlib import Path

import pytest

from skillhub.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.invalid")
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "test-warehouse")
    monkeypatch.setenv("SKILLHUB_CATALOG", "skill_hub")


def _variant(tmp_path, old: str, new: str) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text, old
    p = tmp_path / "hub.yaml"
    p.write_text(text.replace(old, new), encoding="utf-8")
    return p


def test_example_config_loads(cfg):
    assert cfg.catalog == "skill_hub"
    assert cfg.judges == ["gpt", "claude-opus", "gemini-flash"]
    assert cfg.judges_pr == ["gpt"]
    assert cfg.generator == "claude-opus"
    assert cfg.defaults.min_reviewed_cases == 5
    assert cfg.defaults.skill_workers == 6 and cfg.defaults.concurrency_per_endpoint == 6
    assert cfg.workspace.host == "https://example.invalid"
    assert cfg.departments["external"].quarantine is True
    assert cfg.models["gpt"].provider == "gpt"
    assert cfg.models["gemini-flash"].provider == "gemini"
    assert cfg.import_.exclude == ["verify"]


def test_missing_env_var_is_a_config_error(monkeypatch):
    monkeypatch.delenv("SQL_WAREHOUSE_ID", raising=False)
    with pytest.raises(ConfigError, match="SQL_WAREHOUSE_ID"):
        load_config(EXAMPLE)


def test_default_path_comes_from_skillhub_config(tmp_path, monkeypatch):
    p = _variant(tmp_path, "catalog: ${SKILLHUB_CATALOG}", "catalog: from_env_path")
    monkeypatch.setenv("SKILLHUB_CONFIG", str(p))
    assert load_config().catalog == "from_env_path"


def test_unknown_judge_is_rejected(tmp_path):
    p = _variant(tmp_path, "judges: [gpt, claude-opus, gemini-flash]", "judges: [gpt, nope]")
    with pytest.raises(ConfigError, match="nope"):
        load_config(p)


def test_warn_rate_below_fail_rate_is_rejected(tmp_path):
    p = _variant(tmp_path, "warn_rate: 0.8", "warn_rate: 0.3")
    with pytest.raises(ConfigError, match="warn_rate"):
        load_config(p)


@pytest.mark.parametrize("key", ["x_versions", "x_evals"])
def test_department_keys_that_collide_with_schema_suffixes_are_rejected(tmp_path, key):
    p = _variant(tmp_path, "  operations:", f"  {key}:")
    with pytest.raises(ConfigError, match="must not end in _versions or _evals"):
        load_config(p)


def test_common_department_is_required(tmp_path):
    p = _variant(tmp_path, "  common:            {readers: [skillhub-users]}\n", "")
    with pytest.raises(ConfigError, match="must include common"):
        load_config(p)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("repeats: 3", "repeats: 0"),
        ("max_error_rate: 0.1", "max_error_rate: 2"),
        ("pass_score: 0.7", "pass_score: 0"),
        ("skill_workers: 6", "skill_workers: 0"),
    ],
)
def test_defaults_out_of_range_are_rejected(tmp_path, old, new):
    with pytest.raises(ConfigError):
        load_config(_variant(tmp_path, old, new))


def test_skill_workers_defaults_to_six():
    from skillhub.config import Defaults

    assert Defaults(models=["gpt"]).skill_workers == 6


def test_lint_settings_need_no_environment(monkeypatch, tmp_path):
    from skillhub.config import load_lint_settings

    for k in ("DATABRICKS_HOST", "SQL_WAREHOUSE_ID", "SKILLHUB_CATALOG"):
        monkeypatch.delenv(k, raising=False)
    s = load_lint_settings(EXAMPLE)
    assert "common" in s.departments and "external" in s.departments
    assert s.defaults.fail_rate == 0.5
    assert s.quarantined == frozenset({"external"})
    with pytest.raises(ConfigError, match="must not end in"):
        load_lint_settings(_variant(tmp_path, "  operations:", "  ops_evals:"))


def test_a_variable_default_applies_when_the_variable_is_unset_or_empty(monkeypatch):
    from skillhub.config import _expand

    monkeypatch.delenv("SKILLHUB_X", raising=False)
    assert _expand("a ${SKILLHUB_X:-/Shared/skill-hub} b") == "a /Shared/skill-hub b"
    monkeypatch.setenv("SKILLHUB_X", "")
    assert _expand("${SKILLHUB_X:-d}") == "d"
    monkeypatch.setenv("SKILLHUB_X", "/Users/abc/hub")
    assert _expand({"k": ["${SKILLHUB_X:-d}"]}) == {"k": ["/Users/abc/hub"]}
    assert _expand("${SKILLHUB_X:-}") == "/Users/abc/hub"
    monkeypatch.delenv("SKILLHUB_X")
    assert _expand("${SKILLHUB_X:-}") == ""


def test_a_variable_without_a_default_must_still_be_set(monkeypatch):
    from skillhub.config import _expand

    monkeypatch.delenv("SKILLHUB_X", raising=False)
    with pytest.raises(ConfigError, match="SKILLHUB_X is not set"):
        _expand("${SKILLHUB_X}")
    monkeypatch.setenv("SKILLHUB_X", "")
    assert _expand("${SKILLHUB_X}") == ""


def test_workspace_root_defaults_to_shared_and_follows_the_environment(monkeypatch):
    """The bundle points SKILLHUB_WORKSPACE_ROOT at the ci principal's home folder."""
    assert load_config(EXAMPLE).workspace_root == "/Shared/skill-hub"
    monkeypatch.setenv(
        "SKILLHUB_WORKSPACE_ROOT", "/Workspace/Users/1b2c3d4e-0000-4000-8000-000000000001/skill-hub/"
    )
    assert load_config(EXAMPLE).workspace_root == "/Users/1b2c3d4e-0000-4000-8000-000000000001/skill-hub"


@pytest.mark.parametrize(
    "bad", ["skill-hub", "/", "/Workspace", "/Users/../Shared", "/Users/a b", "/Users/a;rm"]
)
def test_a_bad_workspace_root_is_a_config_error(monkeypatch, bad):
    monkeypatch.setenv("SKILLHUB_WORKSPACE_ROOT", bad)
    with pytest.raises(ConfigError, match="workspace_root"):
        load_config(EXAMPLE)


def test_candidates_is_not_a_department_key(tmp_path):
    p = _variant(tmp_path, "  operations:", "  candidates:")
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


def test_drafts_is_not_a_department_key(tmp_path):
    """<workspace_root>/<catalog>/drafts holds the Skill Hub drafts, next to the department experiment folders."""
    p = _variant(tmp_path, "  operations:", "  drafts:")
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


def test_gating_modes_default_to_both_and_reject_unknown_or_empty():
    from pydantic import ValidationError

    from skillhub.config import Defaults

    assert Defaults(models=["gpt"]).gating_modes == ["trigger", "effectiveness"]
    for bad in ([], ["with_skill"]):
        with pytest.raises(ValidationError):
            Defaults(models=["gpt"], gating_modes=bad)


@pytest.mark.parametrize("value", ["", None])
def test_an_empty_or_unset_host_falls_back_to_the_sdk_host(monkeypatch, value):
    """A Databricks Job gets DATABRICKS_HOST="" from the bundle, while the runtime knows its own host."""
    import skillhub.config as config

    if value is None:
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    else:
        monkeypatch.setenv("DATABRICKS_HOST", value)
    monkeypatch.setattr(config, "_sdk_host", lambda: "https://ws.example.com/")
    assert config.load_config(EXAMPLE).workspace.host == "https://ws.example.com"


def test_a_host_without_a_scheme_gets_https(monkeypatch):
    import skillhub.config as config

    monkeypatch.setenv("DATABRICKS_HOST", "ws.example.com")
    assert config.load_config(EXAMPLE).workspace.host == "https://ws.example.com"


def test_no_host_anywhere_is_a_config_error(monkeypatch):
    import skillhub.config as config

    monkeypatch.setenv("DATABRICKS_HOST", "")
    monkeypatch.setattr(config, "_sdk_host", lambda: "")
    with pytest.raises(ConfigError, match="workspace host"):
        config.load_config(EXAMPLE)


GEMINI = "  gemini-flash:  {route: serving, endpoint: databricks-gemini-3-8-flash,  class: fast}"


def test_shipped_config_names_the_bedrock_secret_scope_and_only_release_gate_profiles(cfg):
    assert cfg.external.bedrock.secret_scope == "skillhub-aws"
    assert cfg.external.bedrock.default_region == "us-east-1"
    assert all(p.kind == "hub_yaml" and p.catalog_version is None for p in cfg.models.values())


@pytest.mark.parametrize(
    ("old", "new", "says"),
    [
        ("default_region: us-east-1", "default_region: Virginia", "default_region"),
        ("secret_scope: skillhub-aws", "secret_scope: 'skill hub'", "secret_scope"),
    ],
)
def test_bedrock_settings_are_checked(tmp_path, old, new, says):
    with pytest.raises(ConfigError, match=says):
        load_config(_variant(tmp_path, old, new))


@pytest.mark.parametrize(
    "profile",
    [
        "{route: bedrock, model_id: amazon.nova-lite-v1:0, region: us-east-1, class: fast}",
        "{kind: ug_model, route: serving, endpoint: databricks-x, class: fast}",
        "{route: serving, endpoint: databricks-x, catalog_version: 3, class: fast}",
    ],
)
def test_hub_yaml_profiles_are_databricks_serving_endpoints(tmp_path, profile):
    with pytest.raises(ConfigError, match="models.extra: .*model catalog"):
        load_config(_variant(tmp_path, GEMINI, f"{GEMINI}\n  extra: {profile}"))


def test_an_endpoint_of_another_model_family_gets_no_provider_parameters():
    from skillhub.config import ModelProfile
    from skillhub.gateway.models import adapt_request

    p = ModelProfile(endpoint="databricks-llama-4-maverick", params={"seed": 1}, **{"class": "fast"})
    assert p.provider == "other"
    body = adapt_request(p, [{"role": "user", "content": "hi"}], [], max_tokens=None, trigger=False)
    assert body == {"model": "databricks-llama-4-maverick", "messages": [{"role": "user", "content": "hi"}]}


def test_catalog_profiles_survive_the_eval_worker_round_trip(cfg):
    from skillhub.config import HubConfig, ModelProfile

    nova = ModelProfile(
        route="bedrock", model_id="amazon.nova-lite-v1:0", region="us-east-1", kind="external_model",
        catalog_version=7, **{"class": "fast"},
    )  # fmt: skip
    merged = cfg.model_copy(update={"models": {**cfg.models, "nova-lite": nova}})
    again = HubConfig.model_validate(merged.model_dump(mode="json", by_alias=True))
    assert again.models["nova-lite"] == nova
    assert again.external == cfg.external
