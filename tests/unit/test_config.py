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
    ],
)
def test_defaults_out_of_range_are_rejected(tmp_path, old, new):
    with pytest.raises(ConfigError):
        load_config(_variant(tmp_path, old, new))


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
