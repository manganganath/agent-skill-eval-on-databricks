import os
import shutil

# CLI tests read help and error text. On CI runners (GITHUB_ACTIONS, FORCE_COLOR) Typer forces a colour
# terminal, and ANSI codes split words such as `--config`. Set before anything imports typer.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")
os.environ.setdefault("NO_COLOR", "1")
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config, items):
    if os.environ.get("SKILLHUB_IT") == "1":
        return
    skip = pytest.mark.skip(reason="integration test: set SKILLHUB_IT=1 to run against the dev workspace")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_workspace_root_from_the_shell(monkeypatch):
    """hub.yaml falls back to /Shared/skill-hub without SKILLHUB_WORKSPACE_ROOT; tests set it when they need it."""
    monkeypatch.delenv("SKILLHUB_WORKSPACE_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _reset_auth_client():
    from skillhub import auth

    auth._default_client.cache_clear()
    yield
    auth._default_client.cache_clear()


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.invalid")
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "test-warehouse")
    monkeypatch.setenv("SKILLHUB_CATALOG", "skill_hub")
    from skillhub.config import load_config

    return load_config(ROOT / "config" / "hub.yaml")


@pytest.fixture
def make_bundle(tmp_path):
    """Write a skill bundle under tmp_path/skills/<dept>/<name> and load it."""

    def _make(dept, name, *, body="Use this skill when asked.", description="Does a thing.", files=None):
        from skillhub.bundle import load_bundle

        root = tmp_path / "skills" / dept / name
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
        )
        for rel, text in (files or {}).items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return load_bundle(root, dept)

    return _make


@pytest.fixture(scope="session")
def _mlflow_db(tmp_path_factory):
    """One sqlite store per session: creating the schema once keeps per-test setup cheap."""
    return f"sqlite:///{tmp_path_factory.mktemp('mlflow') / 'mlflow.db'}"


@pytest.fixture(autouse=True)
def _isolated_mlflow(tmp_path, _mlflow_db):
    """Tmp tracking store and a fresh experiment per test, so no test writes mlruns/ or leaks MLflow state."""
    import mlflow
    from mlflow.tracking import fluent

    prev_uri = mlflow.get_tracking_uri()
    prev_exp_id = fluent._active_experiment_id
    mlflow.set_tracking_uri(_mlflow_db)
    exp_id = mlflow.create_experiment(tmp_path.name, artifact_location=(tmp_path / "artifacts").as_uri())
    mlflow.set_experiment(experiment_id=exp_id)
    yield
    mlflow.flush_trace_async_logging()
    mlflow.set_tracking_uri(prev_uri)
    fluent._active_experiment_id = prev_exp_id


SKILL_VERSIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS {evals}.skill_versions (dept STRING, skill STRING, action STRING, "
    "version INT, from_version INT, to_version INT, versioned_fqn STRING, content_hash STRING, "
    "git_sha STRING, eval_run_id STRING, actor STRING, job_run_id STRING, ts TIMESTAMP) "
    "TBLPROPERTIES ('delta.appendOnly' = 'true')"
)


@pytest.fixture
def reg(cfg):
    """Fake UC, sqlite-backed SQL with the skill_versions table in every department, and a Journal."""
    from skillhub.naming import evals_schema
    from skillhub.registry.journal import Journal
    from tests.fakes import FakeSql, FakeUCSkills

    sql = FakeSql()
    for dept in cfg.departments:
        sql.execute(SKILL_VERSIONS_DDL.format(evals=evals_schema(cfg, dept)))
    return SimpleNamespace(cfg=cfg, sql=sql, uc=FakeUCSkills(), journal=Journal(cfg, sql))


@pytest.fixture
def live(reg):
    """Publish a bundle and switch the live skill to it. Returns the published version number."""

    def _go(bundle):
        from skillhub.registry.switch import switch
        from skillhub.registry.versions import publish

        p = publish(
            reg.cfg,
            reg.uc,
            reg.journal,
            bundle,
            git_sha="a" * 40,
            eval_run_id="run-1",
            actor="ci",
            job_run_id=None,
        )
        res = switch(
            reg.cfg, reg.uc, reg.journal, bundle.dept, bundle.name, p.version, actor="ci", job_run_id=None
        )
        assert res.status == "switched", res
        return p.version

    return _go
