import stat
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.iam import ComplexValue, Patch, PatchOp, PatchSchema
from typer.testing import CliRunner

from skillhub import cli
from skillhub.naming import evals_schema, live_schema, versions_schema
from skillhub.registry.bootstrap import (
    ACCOUNT_GROUPS_PATH,
    ASSIGNMENTS_PATH,
    BootstrapError,
    ResultsSteps,
    SecretOutError,
    bootstrap,
    default_results_steps,
    grant_plan,
    print_identities,
    required_groups,
    run_hooks,
    table_grant_plan,
)
from tests.fakes import FakeSql


@pytest.fixture(autouse=True)
def _offline_hooks(monkeypatch, tmp_path_factory):
    """Keep bootstrap's optional hooks offline. The real modules call MLflow and the workspace, and the
    default skills root is the repository's skills/ tree. Tests that need a hook stub it themselves."""
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path_factory.mktemp("no-skills")))
    for name, attr in (
        ("skillhub.results.experiments", "ensure_experiment"),
        ("skillhub.prompts", "register_prompts"),
        ("skillhub.eval.judges", "register_judges"),
    ):
        stub = types.ModuleType(name)
        setattr(stub, attr, lambda *a, **k: None)
        monkeypatch.setitem(sys.modules, name, stub)


def _root(calls):
    """Stand-in for results.experiments.ensure_experiment_root (the results branch adds the real one)."""

    def ensure_experiment_root(c, w, *, ci, ci_eval):
        calls.append(("root", ci, ci_eval))
        return [f"experiment root locked for {ci}, {ci_eval}"]

    return ensure_experiment_root


def _steps(calls=None):
    calls = [] if calls is None else calls
    return ResultsSteps(
        ensure_tables=lambda c, s: calls.append("tables"),
        ensure_views=lambda c, s: calls.append("views"),
        ensure_alerts=lambda c, w: calls.append("alerts") or ["a1", "a2"],
        ensure_genie_space=lambda c, w: calls.append("genie") or "space-1",
        ensure_experiment_root=_root(calls),
    )


DEPLOYER = "deployer@example.com"
_APP_IDS = {"skillhub-ci": (1, "app-1"), "skillhub-ci-eval": (2, "app-2")}


class _Directory:
    """Account groups (the account SCIM proxy), workspace assignments and workspace-local groups."""

    def __init__(self, account=(), unassigned=(), local=()):
        self.account = {name: str(100 + i) for i, name in enumerate([*account, *unassigned])}
        self.assigned = {self.account[n] for n in account}
        self.local = set(local)
        self.calls = []

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")]

    def do(self, method, path, query=None, body=None, headers=None):
        self.calls.append((method, path, query, body))
        if path == ACCOUNT_GROUPS_PATH and method == "GET":
            name = query["filter"].split('"')[1]
            hit = [{"id": self.account.get(name), "displayName": name, "meta": {"resourceType": "Group"}}]
            hit = hit if name in self.account else []
            return {"Resources": hit, "totalResults": len(hit)}
        if path == ACCOUNT_GROUPS_PATH and method == "POST":
            assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:Group"]
            self.account[body["displayName"]] = str(900 + len(self.account))
            return {"id": self.account[body["displayName"]], "displayName": body["displayName"]}
        if path == ASSIGNMENTS_PATH and method == "GET":
            rows = [{"principal": {"principal_id": int(i)}, "permissions": ["USER"]} for i in self.assigned]
            return {"permission_assignments": rows}
        if path.startswith(ASSIGNMENTS_PATH + "/principals/") and method == "PUT":
            assert body == {"permissions": ["USER"]}
            self.assigned.add(path.rsplit("/", 1)[1])
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    def workspace_groups(self, filter=None, **kw):
        name = (filter or "").split('"')[1]
        if name in self.local:
            yield SimpleNamespace(display_name=name, meta=SimpleNamespace(resource_type="WorkspaceGroup"))
        elif self.account.get(name) in self.assigned:
            yield SimpleNamespace(display_name=name, meta=SimpleNamespace(resource_type="Group"))


def _no_identity_writes(w):
    w.groups.create.assert_not_called()
    w.service_principals.create.assert_not_called()
    assert w.dir.writes == []


def _w(existing_groups=(), existing_sps=(), secrets=None, entitlements=None, unassigned=(), local=()):
    secrets = (object(),) if secrets is None else secrets
    w = MagicMock()
    w.current_user.me.return_value = SimpleNamespace(user_name=DEPLOYER)
    w.schemas.get.side_effect = lambda name: SimpleNamespace(owner=DEPLOYER)
    w.tables.get.side_effect = lambda name: SimpleNamespace(owner=DEPLOYER)
    w.service_principal_secrets_proxy.list.side_effect = lambda **kw: iter(secrets)
    w.dir = _Directory(existing_groups, unassigned, local)
    w.api_client.do.side_effect = w.dir.do
    w.groups.list.side_effect = w.dir.workspace_groups
    w.service_principals.list.side_effect = lambda filter=None, **kw: iter(
        [
            SimpleNamespace(
                id=_APP_IDS[s][0],
                application_id=_APP_IDS[s][1],
                display_name=s,
                entitlements=[ComplexValue(value=e) for e in (entitlements or {}).get(s, ())],
            )
            for s in existing_sps
            if f'"{s}"' in (filter or "")
        ]
    )
    return w


def test_readers_get_schema_level_read_volume_only(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    assert "GRANT READ VOLUME ON SCHEMA skill_hub.dept_equity_research TO `dept-equity-research`" in plan
    assert not any(" ON VOLUME " in s or " ON SKILL" in s for s in plan)  # no per-skill grants
    assert not any("ON CATALOG" in s and "READ VOLUME" in s for s in plan)


def test_ci_writes_live_skills_and_modifies_only_the_journal(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    assert any("WRITE VOLUME ON SCHEMA skill_hub.dept_equity_research TO `app-1`" in s for s in plan)
    assert "GRANT USE SCHEMA, SELECT ON SCHEMA skill_hub.dept_equity_research_evals TO `app-1`" in plan
    assert "GRANT MODIFY ON TABLE skill_hub.dept_equity_research_evals.skill_versions TO `app-1`" in (
        table_grant_plan(cfg, "app-1", "app-2")
    )
    assert not any("`skillhub-ci`" in s for s in plan)


def test_missing_identities_fail_without_the_create_flag(cfg):
    with pytest.raises(BootstrapError, match="missing identities"):
        bootstrap(cfg, _w(), FakeSql(), create_identities=False, results=_steps())


def test_missing_catalog_error_names_the_config_key(cfg):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.catalogs.get.side_effect = NotFound("nope")
    with pytest.raises(BootstrapError, match="Set `catalog`"):
        bootstrap(cfg, w, FakeSql(), create_identities=False, results=_steps())


def _new_sp_w():
    w = _w(secrets=())
    ids = {"skillhub-ci": 42, "skillhub-ci-eval": 43}
    w.service_principals.create.side_effect = lambda display_name, entitlements=None: SimpleNamespace(
        id=ids[display_name], application_id=f"app-{ids[display_name]}"
    )
    w.service_principal_secrets_proxy.create.side_effect = lambda service_principal_id: SimpleNamespace(
        id=f"sec-{service_principal_id}", secret=f"s3cr3t-{service_principal_id}"
    )
    return w


def test_secret_goes_to_a_0600_file_and_nowhere_else(cfg, tmp_path, capsys, caplog):
    out, eval_out = tmp_path / "secret.txt", tmp_path / "eval-secret.txt"
    w = _new_sp_w()
    with caplog.at_level("DEBUG"):
        log = bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            skills_root=tmp_path / "none",
            results=_steps(),
            secret_out=out,
            eval_secret_out=eval_out,
        )
    cap = capsys.readouterr()
    assert "s3cr3t-" not in cap.out + cap.err + caplog.text + "\n".join(log)
    assert out.read_text() == "s3cr3t-42" and eval_out.read_text() == "s3cr3t-43"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and stat.S_IMODE(eval_out.stat().st_mode) == 0o600
    assert f"wrote OAuth secret for {cfg.roles.ci} to {out}" in log
    assert f"wrote OAuth secret for {cfg.roles.ci_eval} to {eval_out}" in log
    created = [
        c.kwargs["service_principal_id"] for c in w.service_principal_secrets_proxy.create.call_args_list
    ]
    assert created == ["42", "43"]
    assert {c.kwargs["display_name"] for c in w.service_principals.create.call_args_list} == {
        "skillhub-ci",
        "skillhub-ci-eval",
    }
    w.groups.create.assert_not_called()  # never a workspace-local group
    posts = [c[3]["displayName"] for c in w.dir.writes if c[0] == "POST"]
    assert sorted(posts) == required_groups(cfg)
    assert len([c for c in w.dir.writes if c[0] == "PUT"]) == len(required_groups(cfg))


def test_existing_secret_file_is_refused_before_anything_is_created(cfg, tmp_path):
    out = tmp_path / "secret.txt"
    out.write_text("old")
    w = _new_sp_w()
    with pytest.raises(SecretOutError, match="already exists"):
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            results=_steps(),
            secret_out=out,
            eval_secret_out=tmp_path / "e",
        )
    assert out.read_text() == "old" and not (tmp_path / "e").exists()
    _no_identity_writes(w)
    w.service_principals.create.assert_not_called()


def test_missing_parent_directory_is_refused(cfg, tmp_path):
    w = _new_sp_w()
    with pytest.raises(SecretOutError, match="parent directory"):
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            results=_steps(),
            secret_out=tmp_path / "no" / "s",
            eval_secret_out=tmp_path / "e",
        )
    w.service_principals.create.assert_not_called()


def test_missing_eval_secret_out_is_refused_before_anything_is_created(cfg, tmp_path):
    w = _new_sp_w()
    with pytest.raises(SecretOutError, match="--eval-secret-out"):
        bootstrap(cfg, w, FakeSql(), create_identities=True, results=_steps(), secret_out=tmp_path / "s")
    assert not (tmp_path / "s").exists()  # the ci file opened first is removed again
    _no_identity_writes(w)
    w.service_principals.create.assert_not_called()


def test_existing_eval_secret_file_is_refused_and_ci_file_is_removed(cfg, tmp_path):
    eval_out = tmp_path / "e"
    eval_out.write_text("old")
    w = _new_sp_w()
    with pytest.raises(SecretOutError, match="--eval-secret-out .* already exists"):
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            results=_steps(),
            secret_out=tmp_path / "s",
            eval_secret_out=eval_out,
        )
    assert eval_out.read_text() == "old" and not (tmp_path / "s").exists()
    w.service_principals.create.assert_not_called()


def test_cli_missing_secret_out_exits_2_before_creating(cfg, monkeypatch, tmp_path):
    w = _new_sp_w()
    monkeypatch.setattr(cli, "_services", lambda c: (w, None, FakeSql(), None))
    monkeypatch.setattr("skillhub.registry.bootstrap.default_results_steps", _steps)
    for extra in ([], ["--secret-out", ""], ["--secret-out", str(tmp_path / "x"), "--eval-secret-out", ""]):
        r = CliRunner().invoke(cli.app, [*_CFG, "bootstrap", "--create-identities", *extra])
        assert r.exit_code == 2, r.output
        assert "secret-out" in r.output
        assert not (tmp_path / "x").exists()
    _no_identity_writes(w)
    w.service_principals.create.assert_not_called()


def test_cli_writes_secret_file_and_prints_only_its_path(cfg, tmp_path, monkeypatch):
    w = _new_sp_w()
    monkeypatch.setattr(cli, "_services", lambda c: (w, None, FakeSql(), None))
    monkeypatch.setattr("skillhub.registry.bootstrap.default_results_steps", _steps)
    out, eval_out = tmp_path / "s.txt", tmp_path / "e.txt"
    args = ["--secret-out", str(out), "--eval-secret-out", str(eval_out)]
    r = CliRunner().invoke(cli.app, [*_CFG, "bootstrap", "--create-identities", *args])
    assert r.exit_code == 0, r.output
    assert "s3cr3t-" not in r.output
    assert out.read_text() == "s3cr3t-42" and eval_out.read_text() == "s3cr3t-43"
    assert f"wrote OAuth secret for skillhub-ci to {out}" in r.output
    assert f"wrote OAuth secret for skillhub-ci-eval to {eval_out}" in r.output


def test_existing_service_principal_gets_no_new_secret(cfg):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    bootstrap(cfg, w, FakeSql(), create_identities=True, results=_steps())
    w.service_principal_secrets_proxy.create.assert_not_called()


def test_print_identities_lists_names_and_no_secrets(cfg):
    text = print_identities(cfg)
    assert "dept-equity-research" in text and cfg.roles.ci in text and cfg.roles.ci_eval in text
    assert "secret" not in text.lower()


def test_hooks_register_judges_once_per_skill_experiment(cfg, make_bundle, tmp_path, monkeypatch):
    make_bundle("common", "audit-xls")
    make_bundle("equity_research", "comps")
    calls = []
    exps = types.ModuleType("skillhub.results.experiments")
    exps.ensure_experiment = lambda c, dept, skill: calls.append(("exp", dept, skill)) or f"exp-{skill}"
    judges = types.ModuleType("skillhub.eval.judges")  # matches the real signature
    judges.register_judges = lambda c, experiment_id: calls.append(("judges", experiment_id))
    monkeypatch.setitem(sys.modules, "skillhub.results", types.ModuleType("skillhub.results"))
    monkeypatch.setitem(sys.modules, "skillhub.results.experiments", exps)
    monkeypatch.setitem(sys.modules, "skillhub.eval.judges", judges)
    prompts = types.ModuleType(
        "skillhub.prompts"
    )  # stubbed so the hook stays offline once the real module is present
    prompts.register_prompts = lambda c: calls.append(("prompts",)) or {}
    monkeypatch.setitem(sys.modules, "skillhub.prompts", prompts)
    ran = run_hooks(cfg, FakeSql(), tmp_path / "skills")
    assert ("judges", "exp-audit-xls") in calls and ("judges", "exp-comps") in calls
    assert len([c for c in calls if c[0] == "judges"]) == 2
    assert ran == ["ensure_experiment", "register_prompts", "register_judges"]


def test_bootstrap_runs_results_steps_in_order_and_sets_genie_acl(cfg, tmp_path):
    calls = []
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps(calls))
    assert calls == ["tables", "views", ("root", "app-1", "app-2"), "alerts", "genie"]
    kw = w.permissions.update.call_args.kwargs
    assert kw["request_object_type"] == "genie" and kw["request_object_id"] == "space-1"
    acl = {(a.group_name, a.permission_level.value) for a in kw["access_control_list"]}
    assert acl == {(cfg.roles.authors, "CAN_RUN"), (cfg.roles.reviewers, "CAN_RUN")}
    assert any("alerts ensured: a1, a2" in line for line in log)


def test_genie_acl_failure_warns_with_manual_step_and_continues(cfg, tmp_path, caplog):
    from databricks.sdk.errors import PermissionDenied

    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.permissions.update.side_effect = PermissionDenied("no")
    with caplog.at_level("WARNING"):
        log = bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps())
    assert "Manual step" in caplog.text and "space-1" in caplog.text
    assert any(line.startswith("warning:") for line in log)


def test_eval_schema_is_granted_to_reviewers_only(cfg):
    plan = [
        s
        for s in grant_plan(cfg, "app-1", "app-2")
        if s.endswith("ON SCHEMA skill_hub.eval TO `skillhub-reviewers`")
    ]
    assert len(plan) == 2
    assert not any(
        "skill_hub.eval TO" in s and "skillhub-reviewers" not in s for s in grant_plan(cfg, "app-1", "app-2")
    )


def test_missing_results_module_raises_instead_of_skipping(cfg, monkeypatch):
    tables = types.ModuleType("skillhub.results.tables")
    tables.ensure_tables = tables.ensure_views = lambda c, s: None
    monkeypatch.setitem(sys.modules, "skillhub.results.tables", tables)
    monkeypatch.setitem(sys.modules, "skillhub.results", types.ModuleType("skillhub.results"))
    monkeypatch.setitem(sys.modules, "skillhub.results.alerts", None)  # makes the import fail
    with pytest.raises(BootstrapError, match="skillhub.results.alerts"):
        default_results_steps()
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    with pytest.raises(BootstrapError, match="skillhub.results"):
        bootstrap(cfg, w, FakeSql(), create_identities=False)
    _no_identity_writes(w)


def test_existing_principal_without_a_secret_gets_one(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval], secrets=())
    w.service_principal_secrets_proxy.create.return_value = SimpleNamespace(secret="x")
    out, eval_out = tmp_path / "s", tmp_path / "e"
    bootstrap(
        cfg,
        w,
        FakeSql(),
        create_identities=True,
        skills_root=tmp_path,
        results=_steps(),
        secret_out=out,
        eval_secret_out=eval_out,
    )
    created = [
        c.kwargs["service_principal_id"] for c in w.service_principal_secrets_proxy.create.call_args_list
    ]
    assert created == ["1", "2"]
    assert out.read_text() == "x" and eval_out.read_text() == "x"


_CFG = ["--config", str(Path(__file__).parents[2] / "config" / "hub.yaml")]


def test_cli_print_identities(cfg):
    r = CliRunner().invoke(
        cli.app,
        [
            "--config",
            str(Path(__file__).parents[2] / "config" / "hub.yaml"),
            "bootstrap",
            "--print-identities",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "dept-equity-research" in r.output and "skillhub-ci" in r.output
    assert "secret" not in r.output.lower()


def test_only_ci_eval_can_create_run_schemas_in_the_catalog(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    assert "GRANT CREATE SCHEMA ON CATALOG skill_hub TO `app-2`" in plan
    assert [s for s in plan if s.startswith("GRANT CREATE SCHEMA ON CATALOG")] == [
        "GRANT CREATE SCHEMA ON CATALOG skill_hub TO `app-2`"
    ]
    assert plan[0] == "REVOKE CREATE SCHEMA ON CATALOG skill_hub FROM `app-1`"  # first, before any grant


def _for(plan, principal):
    return [s for s in plan if s.endswith(f"TO `{principal}`")]


def test_ci_eval_gets_exactly_the_eval_set(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    mine = [s for s in _for(plan, "app-2") if s.startswith("GRANT")]
    assert "GRANT USE CATALOG ON CATALOG skill_hub TO `app-2`" in mine
    for dept in cfg.departments:  # quarantined ones included: external skills are evaluated
        live, ver, ev = live_schema(cfg, dept), versions_schema(cfg, dept), evals_schema(cfg, dept)
        assert f"GRANT USE SCHEMA, READ VOLUME ON SCHEMA {live} TO `app-2`" in mine
        assert f"GRANT USE SCHEMA, READ VOLUME ON SCHEMA {ver} TO `app-2`" in mine
        assert f"GRANT USE SCHEMA, SELECT ON SCHEMA {ev} TO `app-2`" in mine
    assert len(mine) == 2 + 3 * len(cfg.departments)  # use catalog, create schema, three per department
    for s in mine:  # no write path to the live or versions volumes; no schema-level MODIFY anywhere
        assert "WRITE VOLUME" not in s and "CREATE VOLUME" not in s and "MODIFY" not in s, s


def test_ci_eval_has_no_modify_on_the_journal_and_table_level_modify_elsewhere(cfg):
    stmts = table_grant_plan(cfg, "app-1", "app-2")
    mine = _for(stmts, "app-2")
    assert len(mine) == 4 * len(cfg.departments)  # CREATE TABLE plus three MODIFY per department
    assert not any("skill_versions" in s for s in mine)
    for dept in cfg.departments:
        ev = evals_schema(cfg, dept)
        for t in ("eval_runs", "eval_cases", "rubrics"):
            assert f"GRANT MODIFY ON TABLE {ev}.{t} TO `app-2`" in mine
        assert f"GRANT MODIFY ON TABLE {ev}.skill_versions TO `app-1`" in stmts
    assert [s for s in _for(stmts, "app-1") if "skill_versions" not in s] == []
    assert not any("MODIFY ON SCHEMA" in s for s in stmts)
    for dept in cfg.departments:
        assert f"GRANT CREATE TABLE ON SCHEMA {evals_schema(cfg, dept)} TO `app-2`" in mine


def test_ci_keeps_publish_and_switch_grants_and_nothing_for_eval_runs(cfg):
    mine = _for(grant_plan(cfg, "app-1", "app-2"), "app-1")
    assert not any("CREATE SCHEMA" in s or "MODIFY" in s or "CREATE TABLE" in s for s in mine)
    assert len(mine) == 1 + 3 * len(cfg.departments)
    vol = "GRANT USE SCHEMA, CREATE VOLUME, READ VOLUME, WRITE VOLUME ON SCHEMA"
    for dept in cfg.departments:
        assert f"{vol} {live_schema(cfg, dept)} TO `app-1`" in mine
        assert f"{vol} {versions_schema(cfg, dept)} TO `app-1`" in mine
        assert f"GRANT USE SCHEMA, SELECT ON SCHEMA {evals_schema(cfg, dept)} TO `app-1`" in mine


def test_table_grants_are_applied_after_ensure_tables(cfg, tmp_path):
    sql = FakeSql()
    order = []
    real = sql.execute
    sql.execute = lambda stmt, params=None: (order.append(stmt), real(stmt, params))[1]
    steps = ResultsSteps(
        ensure_tables=lambda c, s: order.append("TABLES"),
        ensure_views=lambda c, s: order.append("VIEWS"),
        ensure_alerts=lambda c, w: [],
        ensure_genie_space=lambda c, w: "space-1",
        ensure_experiment_root=_root([]),
    )
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=steps)
    tables_at = order.index("TABLES")
    table_grants = [i for i, s in enumerate(order) if " ON TABLE " in s]
    assert table_grants and min(table_grants) > tables_at
    late = [i for i, s in enumerate(order) if s.startswith("GRANT CREATE TABLE")]
    assert late and min(late) > tables_at  # ci_eval cannot pre-create journal or results tables
    assert all(
        i < tables_at
        for i, s in enumerate(order)
        if s.startswith(("GRANT", "REVOKE")) and " ON TABLE " not in s and "CREATE TABLE" not in s
    )


def test_the_two_ci_principals_must_differ(cfg):
    with pytest.raises(BootstrapError, match="different principals"):
        grant_plan(cfg, "app-1", "app-1")


def test_ci_eval_role_defaults_and_is_checked_like_ci(cfg):
    from skillhub.config import Roles

    assert Roles(authors="a", reviewers="r", ci="c").ci_eval == "skillhub-ci-eval"
    assert cfg.roles.ci_eval == "skillhub-ci-eval"
    with pytest.raises(BootstrapError, match="unsupported principal"):
        grant_plan(cfg, "app-1", "app-2\n")
    bad = cfg.model_copy(update={"roles": cfg.roles.model_copy(update={"ci_eval": 'x" or 1 eq 1'})})
    w = _w()
    with pytest.raises(BootstrapError, match="unsupported principal"):
        bootstrap(bad, w, FakeSql(), create_identities=True, results=_steps())
    _no_identity_writes(w)


def test_bootstrap_grants_ci_by_application_id(cfg, tmp_path):
    sql = FakeSql()
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    stmts = [c[0] for c in sql.calls]
    assert any("TO `app-1`" in x for x in stmts) and any("TO `app-2`" in x for x in stmts)
    assert not any("`skillhub-ci`" in x or "`skillhub-ci-eval`" in x for x in stmts)
    assert any("DATABRICKS_CLIENT_ID" in line and "app-1" in line for line in log)
    assert any("DATABRICKS_CLIENT_ID" in line and "app-2" in line for line in log)


def test_quarantined_department_is_reviewers_and_the_ci_principals_only(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    ext = [s for s in plan if "skill_hub.external" in s and s.startswith("GRANT")]
    assert ext
    for s in ext:
        assert s.endswith(("`skillhub-reviewers`", "`app-1`", "`app-2`")), s
    assert not any("skill_hub.external" in s and "skillhub-authors" in s for s in plan)
    assert not any("skill_hub.external" in s and "skillhub-users" in s for s in plan)
    # reviewers can still read the live and versions schemas
    assert "GRANT READ VOLUME ON SCHEMA skill_hub.external TO `skillhub-reviewers`" in plan
    assert "GRANT READ VOLUME ON SCHEMA skill_hub.external_versions TO `skillhub-reviewers`" in plan


def test_a_departments_readers_appear_only_on_their_own_schemas(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    for dept, d in cfg.departments.items():
        if d.quarantine:
            continue
        allowed = {live_schema(cfg, dept), evals_schema(cfg, dept)}
        for r in d.readers:
            for s in (x for x in plan if x.endswith(f"TO `{r}`")):
                if "ON CATALOG" in s:
                    assert s.startswith("GRANT USE CATALOG"), s
                else:
                    assert s.split(" ON SCHEMA ")[1].split(" ")[0] in allowed, (r, s)


def test_bad_identifiers_fail_before_any_change(cfg, tmp_path):
    bad = cfg.model_copy(update={"catalog": "Bad;DROP"})
    w = _w()
    with pytest.raises(BootstrapError, match="catalog"):
        bootstrap(bad, w, FakeSql(), create_identities=True, results=_steps())
    _no_identity_writes(w)
    w.catalogs.get.assert_not_called()
    dept = cfg.model_copy(update={"departments": {"Bad-Dept": cfg.departments["common"]}})
    with pytest.raises(BootstrapError, match="department key"):
        grant_plan(dept, "app-1", "app-2")


def test_config_rejects_bad_catalog_and_department_key(tmp_path, monkeypatch):
    import yaml

    from skillhub.config import ConfigError, load_config

    for k, v in (("DATABRICKS_HOST", "h"), ("SQL_WAREHOUSE_ID", "w"), ("SKILLHUB_CATALOG", "c")):
        monkeypatch.setenv(k, v)
    base = (Path(__file__).parents[2] / "config" / "hub.yaml").read_text()
    for mutate, key in (
        (lambda d: d.update(catalog="x;y"), "x;y"),
        (lambda d: d["departments"].update({"Bad-Dept": {"readers": []}}), "Bad-Dept"),
    ):
        d = yaml.safe_load(base)
        mutate(d)
        p = tmp_path / "hub.yaml"
        p.write_text(yaml.safe_dump(d))
        with pytest.raises(ConfigError, match=key):
            load_config(p)


def test_scim_filter_rejects_a_quote(cfg):
    w = _w()
    bad = cfg.model_copy(update={"roles": cfg.roles.model_copy(update={"authors": 'x" or 1 eq 1'})})
    with pytest.raises(BootstrapError, match="unsupported principal"):
        bootstrap(bad, w, FakeSql(), create_identities=True, results=_steps())
    _no_identity_writes(w)


def test_missing_ci_principal_without_create_flag_is_a_clear_error(cfg):
    w = _w(existing_groups=required_groups(cfg))
    with pytest.raises(BootstrapError, match="service principal skillhub-ci"):
        bootstrap(cfg, w, FakeSql(), create_identities=False, results=_steps())


def test_duplicate_service_principals_raise(cfg):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    two = [SimpleNamespace(id=1, application_id="a"), SimpleNamespace(id=2, application_id="b")]
    w.service_principals.list.side_effect = lambda **kw: iter(two)
    with pytest.raises(BootstrapError, match="2 service principals"):
        bootstrap(cfg, w, FakeSql(), create_identities=False, results=_steps())


def test_expired_secret_counts_as_absent(cfg, tmp_path):
    expired = SimpleNamespace(id="old", expire_time="2020-01-01T00:00:00Z")
    w = _w(
        existing_groups=required_groups(cfg),
        existing_sps=[cfg.roles.ci, cfg.roles.ci_eval],
        secrets=(expired,),
    )
    w.service_principal_secrets_proxy.create.return_value = SimpleNamespace(id="n", secret="x")
    out, eval_out = tmp_path / "s", tmp_path / "e"
    bootstrap(
        cfg, w, FakeSql(), create_identities=True, results=_steps(), secret_out=out, eval_secret_out=eval_out
    )
    assert out.read_text() == "x" and eval_out.read_text() == "x"


def test_secret_out_without_need_logs_nothing_written(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    out = tmp_path / "s"
    log = bootstrap(cfg, w, FakeSql(), create_identities=True, results=_steps(), secret_out=out)
    assert "--secret-out given but no secret was created; nothing written" in log
    assert not out.exists()


def test_failed_secret_create_removes_the_empty_file(cfg, tmp_path):
    w = _new_sp_w()
    w.service_principal_secrets_proxy.create.side_effect = RuntimeError("api down")
    out, eval_out = tmp_path / "s", tmp_path / "e"
    with pytest.raises(RuntimeError):
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            results=_steps(),
            secret_out=out,
            eval_secret_out=eval_out,
        )
    assert not out.exists() and not eval_out.exists()


def test_failed_secret_write_names_the_secret_id_not_the_value(cfg, tmp_path, monkeypatch):
    w = _new_sp_w()
    out, eval_out = tmp_path / "s", tmp_path / "e"

    def boom(fd, mode):
        raise OSError("disk full")

    monkeypatch.setattr("skillhub.registry.bootstrap.os.fdopen", boom)
    with pytest.raises(BootstrapError) as ei:
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            results=_steps(),
            secret_out=out,
            eval_secret_out=eval_out,
        )
    assert "sec-42" in str(ei.value) and "s3cr3t-" not in str(ei.value)
    assert not out.exists() and not eval_out.exists()


def test_sdk_debug_logging_cannot_record_the_secret(cfg, tmp_path, caplog):
    import logging

    sdk = logging.getLogger("databricks.sdk")
    w = _new_sp_w()

    def noisy(**kw):
        sdk.debug("response: s3cr3t-value")  # what SDK DEBUG logging would do
        return SimpleNamespace(id="sec-1", secret="s3cr3t-value")

    w.service_principal_secrets_proxy.create.side_effect = noisy
    sdk.setLevel(logging.DEBUG)
    try:
        with caplog.at_level("DEBUG"):
            bootstrap(
                cfg,
                w,
                FakeSql(),
                create_identities=True,
                results=_steps(),
                secret_out=tmp_path / "s",
                eval_secret_out=tmp_path / "e",
            )
        assert sdk.level == logging.DEBUG  # restored
    finally:
        sdk.setLevel(logging.NOTSET)
    assert "s3cr3t-value" not in caplog.text


def test_skipped_experiment_step_warns(cfg, caplog, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "skillhub.results.experiments", None
    )  # as if the results package were absent
    with caplog.at_level("WARNING"):
        run_hooks(cfg, FakeSql(), None)
    assert "experiments" in caplog.text


UUID = "123e4567-e89b-12d3-a456-426614174000"


def _uuid_cfg(cfg):
    return cfg.model_copy(update={"roles": cfg.roles.model_copy(update={"ci": UUID})})


def test_uuid_ci_role_is_looked_up_by_application_id(cfg):
    from skillhub.registry.bootstrap import _find_sp

    w = MagicMock()
    w.service_principals.list.return_value = iter([SimpleNamespace(id=7, application_id=UUID)])
    assert _find_sp(w, UUID).id == 7
    assert w.service_principals.list.call_args.kwargs["filter"] == f'applicationId eq "{UUID}"'


def test_uuid_ci_role_without_a_principal_raises_and_creates_nothing(cfg, tmp_path):
    ucfg = _uuid_cfg(cfg)
    w = _w(existing_groups=required_groups(ucfg))
    with pytest.raises(BootstrapError, match="no service principal has it"):
        bootstrap(ucfg, w, FakeSql(), create_identities=True, results=_steps(), secret_out=tmp_path / "s")
    w.service_principals.create.assert_not_called()
    assert not (tmp_path / "s").exists()


def test_print_identities_does_not_ask_for_a_principal_named_like_a_uuid(cfg):
    text = print_identities(_uuid_cfg(cfg))
    assert "must already exist (application ID): " + UUID in text
    assert text.count("display name") == 1 and "to create (display name): skillhub-ci-eval" in text


def test_secret_out_without_create_flag_logs_nothing_written(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    out = tmp_path / "s"
    log = bootstrap(cfg, w, FakeSql(), create_identities=False, results=_steps(), secret_out=out)
    assert "--secret-out given but no secret was created; nothing written" in log
    assert not out.exists()


def test_trailing_newline_is_rejected_in_identifiers(cfg):
    bad = cfg.model_copy(update={"catalog": "skill_hub\n"})
    with pytest.raises(BootstrapError, match="catalog"):
        grant_plan(bad, "app-1", "app-2")
    with pytest.raises(BootstrapError, match="unsupported principal"):
        grant_plan(cfg, "app-1\n", "app-2")


def test_config_rejects_a_catalog_with_a_trailing_newline(cfg):
    from skillhub.config import HubConfig

    data = cfg.model_dump(by_alias=True)
    data["catalog"] = "skill_hub\n"
    with pytest.raises(ValueError, match="catalog"):
        HubConfig.model_validate(data)


def test_find_helpers_reject_a_quote_on_their_own():
    from skillhub.registry.bootstrap import _find_group, _find_sp

    w = MagicMock()
    for fn in (_find_group, _find_sp):
        with pytest.raises(BootstrapError, match="unsupported principal"):
            fn(w, 'x" or displayName pr')
    w.groups.list.assert_not_called()
    w.service_principals.list.assert_not_called()


def test_revokes_come_before_the_grants_that_replace_them(cfg):
    plan = grant_plan(cfg, "app-1", "app-2")
    for dept in cfg.departments:
        ev = evals_schema(cfg, dept)
        revokes = [f"REVOKE MODIFY ON SCHEMA {ev} FROM `app-1`", f"REVOKE MODIFY ON SCHEMA {ev} FROM `app-2`"]
        assert all(r in plan for r in revokes)
        i = max(plan.index(r) for r in revokes)
        assert plan.index(f"CREATE SCHEMA IF NOT EXISTS {ev}") < i  # the schema exists when revoked
        assert all(f"ON SCHEMA {ev} TO" not in x for x in plan[: min(plan.index(r) for r in revokes)])


def test_squatted_schema_fails_bootstrap_naming_the_owner(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.schemas.get.side_effect = lambda name: SimpleNamespace(
        owner="app-2" if name.endswith("_evals") else DEPLOYER
    )
    sql = FakeSql()
    with pytest.raises(BootstrapError, match=r"schema skill_hub\.\w+_evals is owned by 'app-2'"):
        bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert not any(
        c[0].startswith("GRANT USE SCHEMA, SELECT ON SCHEMA") and "_evals" in c[0] for c in sql.calls
    )


def test_squatted_journal_table_fails_before_table_grants(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.tables.get.side_effect = lambda name: SimpleNamespace(owner="app-2")
    sql = FakeSql()
    with pytest.raises(
        BootstrapError, match=r"table skill_hub\.\w+_evals\.skill_versions is owned by 'app-2'"
    ):
        bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert not any(" ON TABLE " in c[0] for c in sql.calls)


class _TraceSql(FakeSql):
    """MLflow's UC trace storage makes managed tables and views; only tables take MODIFY."""

    def execute(self, statement, params=None):
        if "information_schema.tables" in statement:
            self.calls.append((statement, dict(params or {})))
            return [
                {"table_name": "traces_otel_spans", "table_type": "MANAGED"},
                {"table_name": "traces_trace_unified", "table_type": "VIEW"},
            ]
        return super().execute(statement, params)


def test_trace_tables_are_granted_to_ci_eval_after_the_hooks(cfg, tmp_path):
    sql = _TraceSql()
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    stmts = [c[0] for c in sql.calls]
    ev = evals_schema(cfg, "equity_research")
    lookup = next(
        i
        for i, c in enumerate(sql.calls)
        if "information_schema.tables" in c[0] and c[1].get("schema") == ev.split(".", 1)[1]
    )
    table = f"GRANT MODIFY, SELECT ON TABLE {ev}.traces_otel_spans TO `app-2`"
    view = f"GRANT SELECT ON TABLE {ev}.traces_trace_unified TO `app-2`"
    assert table in stmts and view in stmts and stmts.index(table) > lookup
    assert not any(
        "MODIFY" in s and "traces_trace_unified" in s for s in stmts
    )  # UC refuses MODIFY on a view
    assert f"granted ci_eval access to {2 * len(cfg.departments)} trace tables" in log


def test_app_grant_plan_reads_skills_and_writes_traces_but_never_manages(cfg):
    from skillhub.registry.bootstrap import app_grant_plan

    stmts = app_grant_plan(cfg, "app-9")
    assert f"GRANT USE CATALOG ON CATALOG {cfg.catalog} TO `app-9`" in stmts
    for dept in cfg.departments:
        for schema in (live_schema(cfg, dept), versions_schema(cfg, dept)):
            assert f"GRANT READ VOLUME ON SCHEMA {schema} TO `app-9`" in stmts
        assert f"GRANT USE SCHEMA ON SCHEMA {evals_schema(cfg, dept)} TO `app-9`" in stmts
    assert not any("MANAGE" in s or "MODIFY" in s or "CREATE" in s for s in stmts)


def test_bootstrap_grants_the_app_principal_when_it_is_set(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", "app-9")
    sql = _TraceSql()
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    stmts = [c[0] for c in sql.calls]
    ev = evals_schema(cfg, "equity_research")
    assert f"GRANT READ VOLUME ON SCHEMA {live_schema(cfg, 'equity_research')} TO `app-9`" in stmts
    assert f"GRANT MODIFY, SELECT ON TABLE {ev}.traces_otel_spans TO `app-9`" in stmts
    assert f"GRANT SELECT ON TABLE {ev}.traces_trace_unified TO `app-9`" in stmts
    assert any(line.startswith("granted the app principal app-9 ") for line in log)


def test_bootstrap_warns_and_grants_nothing_to_an_unset_app_principal(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SKILLHUB_APP_PRINCIPAL", raising=False)
    sql = _TraceSql()
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert not any("TO `app-9`" in c[0] for c in sql.calls)
    assert any("SKILLHUB_APP_PRINCIPAL is not set" in line for line in log)


def test_odd_trace_table_name_is_refused(cfg):
    class Odd(FakeSql):
        def execute(self, statement, params=None):
            if "information_schema.tables" in statement:
                return [{"table_name": "traces_x; DROP", "table_type": "MANAGED"}]
            return []

    with pytest.raises(BootstrapError, match="unexpected trace table"):
        from skillhub.registry.bootstrap import trace_table_grants

        trace_table_grants(cfg, Odd(), "app-2")


def test_table_grant_plan_checks_catalog_and_distinct_principals(cfg):
    with pytest.raises(BootstrapError, match="different principals"):
        table_grant_plan(cfg, "app-1", "app-1")
    with pytest.raises(BootstrapError, match="catalog"):
        table_grant_plan(cfg.model_copy(update={"catalog": "Bad;DROP"}), "app-1", "app-2")


def test_two_roles_resolving_to_one_principal_fail_before_creating_anything(cfg, tmp_path):
    w = _w(existing_groups=[])
    same = SimpleNamespace(id=5, application_id="app-5")
    w.service_principals.list.side_effect = lambda **kw: iter([same])
    with pytest.raises(BootstrapError, match="same service principal"):
        bootstrap(cfg, w, FakeSql(), create_identities=True, results=_steps(), secret_out=tmp_path / "s")
    _no_identity_writes(w)
    assert not (tmp_path / "s").exists()


def test_failure_after_the_first_secret_names_the_written_file(cfg, tmp_path):
    w = _new_sp_w()
    first = w.service_principal_secrets_proxy.create.side_effect

    def second_fails(service_principal_id):
        if service_principal_id == "43":
            raise RuntimeError("api down")
        return first(service_principal_id=service_principal_id)

    w.service_principal_secrets_proxy.create.side_effect = second_fails
    out, eval_out = tmp_path / "s", tmp_path / "e"
    with pytest.raises(BootstrapError) as ei:
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            results=_steps(),
            secret_out=out,
            eval_secret_out=eval_out,
        )
    assert str(out) in str(ei.value) and "s3cr3t-" not in str(ei.value)
    assert out.read_text() == "s3cr3t-42" and not eval_out.exists()


def test_eval_secret_out_without_need_logs_nothing_written(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    out = tmp_path / "e"
    log = bootstrap(cfg, w, FakeSql(), create_identities=True, results=_steps(), eval_secret_out=out)
    assert "--eval-secret-out given but no secret was created; nothing written" in log
    assert not out.exists()


def test_no_grant_or_revoke_has_a_principal_list(cfg):
    for stmt in [*grant_plan(cfg, "app-1", "app-2"), *table_grant_plan(cfg, "app-1", "app-2")]:
        if stmt.startswith("GRANT"):
            assert "," not in stmt.rsplit(" TO ", 1)[1], stmt
        elif stmt.startswith("REVOKE"):
            assert "," not in stmt.rsplit(" FROM ", 1)[1], stmt


def test_every_results_table_is_owner_checked(cfg, tmp_path):
    for table in ("eval_runs", "eval_cases", "rubrics"):
        w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
        w.tables.get.side_effect = lambda n, t=table: SimpleNamespace(
            owner="app-2" if n.endswith("." + t) else DEPLOYER
        )
        sql = FakeSql()
        with pytest.raises(BootstrapError, match=rf"table skill_hub\.\w+_evals\.{table} is owned by 'app-2'"):
            bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
        assert not any(" ON TABLE " in c[0] for c in sql.calls)


_BOTH = ("workspace-access", "databricks-sql-access")


def _ready_w(**kw):
    return _w(existing_sps=["skillhub-ci", "skillhub-ci-eval"], **kw)


def test_new_principals_are_created_with_both_entitlements(cfg, tmp_path):
    w = _new_sp_w()
    bootstrap(
        cfg,
        w,
        FakeSql(),
        create_identities=True,
        skills_root=tmp_path / "none",
        results=_steps(),
        secret_out=tmp_path / "s.txt",
        eval_secret_out=tmp_path / "e.txt",
    )
    calls = w.service_principals.create.call_args_list
    assert len(calls) == 2
    for c in calls:
        assert [e.value for e in c.kwargs["entitlements"]] == list(_BOTH)
    w.service_principals.patch.assert_not_called()


def test_existing_principal_missing_one_entitlement_gets_one_patch(cfg, tmp_path):
    ents = {"skillhub-ci": _BOTH, "skillhub-ci-eval": ("workspace-access",)}
    w = _ready_w(entitlements=ents)
    bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path / "none", results=_steps())
    w.service_principals.patch.assert_called_once_with(
        2,
        operations=[Patch(op=PatchOp.ADD, path="entitlements", value=[{"value": "databricks-sql-access"}])],
        schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
    )


def test_existing_principals_with_both_entitlements_get_no_patch(cfg, tmp_path):
    ents = {"skillhub-ci": _BOTH, "skillhub-ci-eval": tuple(reversed(_BOTH))}
    w = _ready_w(entitlements=ents)
    bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path / "none", results=_steps())
    w.service_principals.patch.assert_not_called()


def test_missing_entitlement_without_create_identities_warns_and_does_not_patch(cfg, tmp_path, caplog):
    w = _ready_w(
        existing_groups=required_groups(cfg),
        entitlements={"skillhub-ci": _BOTH, "skillhub-ci-eval": ()},
    )
    with caplog.at_level("WARNING"):
        bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path / "none", results=_steps())
    w.service_principals.patch.assert_not_called()
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "skillhub-ci-eval" in m and "workspace-access" in m and "databricks-sql-access" in m for m in warned
    )
    assert not any("skillhub-ci " in m or "skillhub-ci:" in m for m in warned)


def test_refused_secret_explains_the_ui_fallback_and_leaves_no_file(cfg, tmp_path):
    w = _new_sp_w()
    w.service_principal_secrets_proxy.create.side_effect = PermissionDenied("policy")
    out, eval_out = tmp_path / "secret.txt", tmp_path / "eval-secret.txt"
    with pytest.raises(BootstrapError) as ei:
        bootstrap(
            cfg,
            w,
            FakeSql(),
            create_identities=True,
            skills_root=tmp_path / "none",
            results=_steps(),
            secret_out=out,
            eval_secret_out=eval_out,
        )
    msg = str(ei.value)
    assert "skillhub-ci" in msg
    assert "The workspace refused to create an OAuth secret through the API" in msg
    assert (
        "Settings → Identity and access → Service principals → skillhub-ci → Secrets → Generate secret" in msg
    )
    assert "all-apis" in msg and str(out) in msg and "0600" in msg
    assert not out.exists() and not eval_out.exists()
    assert w.dir.writes  # nothing is rolled back


def test_print_identities_lists_entitlements(cfg):
    text = print_identities(cfg)
    assert text.count("  entitlements: workspace-access, databricks-sql-access") == 2
    assert "secret" not in text.lower()


# --- account groups and warehouse access ---


def _granted(sql):
    return [c[0] for c in sql.calls if c[0].startswith(("GRANT", "REVOKE"))]


def test_missing_groups_are_created_as_account_groups_and_assigned(cfg, tmp_path):
    w = _w(existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path, results=_steps())
    w.groups.create.assert_not_called()
    posts = [c for c in w.dir.writes if c[0] == "POST"]
    assert {c[1] for c in posts} == {ACCOUNT_GROUPS_PATH}
    assert sorted(c[3]["displayName"] for c in posts) == required_groups(cfg)
    puts = [c for c in w.dir.writes if c[0] == "PUT"]
    assert {c[1] for c in puts} == {
        f"{ASSIGNMENTS_PATH}/principals/{w.dir.account[g]}" for g in required_groups(cfg)
    }
    assert f"created account group {cfg.roles.authors}" in log
    assert f"assigned account group {cfg.roles.authors} to this workspace" in log


def test_an_existing_unassigned_account_group_is_only_assigned(cfg, tmp_path):
    groups = required_groups(cfg)
    w = _w(existing_groups=groups[1:], unassigned=groups[:1], existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path, results=_steps())
    assert w.dir.writes == [
        ("PUT", f"{ASSIGNMENTS_PATH}/principals/{w.dir.account[groups[0]]}", None, {"permissions": ["USER"]})
    ]


def test_without_the_flag_missing_or_unassigned_groups_stop_before_any_grant(cfg):
    groups = required_groups(cfg)
    w = _w(existing_groups=groups[2:], unassigned=groups[1:2], existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    sql = FakeSql()
    with pytest.raises(BootstrapError) as ei:
        bootstrap(cfg, w, sql, create_identities=False, results=_steps())
    msg = str(ei.value)
    assert f"account group {groups[0]}" in msg
    assert f"workspace assignment of account group {groups[1]}" in msg
    assert "account groups assigned to this workspace" in msg
    assert _granted(sql) == [] and w.dir.writes == []


@pytest.mark.parametrize("create", [True, False])
def test_a_workspace_local_group_with_a_required_name_is_an_error(cfg, tmp_path, create):
    w = _w(existing_groups=required_groups(cfg)[1:], local=required_groups(cfg)[:1])
    w.service_principals.list.side_effect = lambda filter=None, **kw: iter([])
    sql = FakeSql()
    with pytest.raises(BootstrapError, match="workspace-local group") as ei:
        bootstrap(cfg, w, sql, create_identities=create, results=_steps(), secret_out=tmp_path / "s")
    assert "Delete them" in str(ei.value) and "account groups" in str(ei.value)
    assert required_groups(cfg)[0] in str(ei.value)
    _no_identity_writes(w)
    assert _granted(sql) == [] and not (tmp_path / "s").exists()


def test_account_group_lookup_rejects_a_quote_and_duplicates():
    from skillhub.registry.bootstrap import _find_account_group

    w = MagicMock()
    with pytest.raises(BootstrapError, match="unsupported principal"):
        _find_account_group(w, 'x" or displayName pr')
    w.api_client.do.assert_not_called()
    w.api_client.do.return_value = {
        "Resources": [{"id": "1", "displayName": "g"}, {"id": "2", "displayName": "g"}]
    }
    with pytest.raises(BootstrapError, match="2 account groups"):
        _find_account_group(w, "g")
    w.api_client.do.return_value = {"Resources": [{"id": "1/../x", "displayName": "g"}]}
    from skillhub.registry.bootstrap import _scim_id

    with pytest.raises(BootstrapError, match="unexpected id"):
        _scim_id(_find_account_group(w, "g"), "g")


def test_print_identities_asks_for_account_groups(cfg):
    text = print_identities(cfg)
    assert text.startswith("Account groups to create and assign to this workspace")
    assert "workspace-local" in text


def test_create_identities_grants_both_ci_principals_can_use_on_the_warehouse(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path, results=_steps())
    (wh,) = [
        c for c in w.permissions.update.call_args_list if c.kwargs["request_object_type"] == "warehouses"
    ]
    assert wh.kwargs["request_object_id"] == cfg.workspace.sql_warehouse_id
    acl = {(a.service_principal_name, a.permission_level.value) for a in wh.kwargs["access_control_list"]}
    # ci deploys the bundle, and binding a warehouse to the app needs MANAGE on it
    assert acl == {("app-1", "CAN_MANAGE"), ("app-2", "CAN_USE")}
    assert f"SQL warehouse {cfg.workspace.sql_warehouse_id}: CAN_MANAGE for app-1, CAN_USE for app-2" in log


def _acl(*sps, level="CAN_USE"):
    from databricks.sdk.service.iam import (
        AccessControlResponse,
        ObjectPermissions,
        Permission,
        PermissionLevel,
    )

    return ObjectPermissions(
        access_control_list=[
            AccessControlResponse(
                service_principal_name=sp,
                all_permissions=[Permission(permission_level=PermissionLevel(level))],
            )
            for sp in sps
        ]
    )


def test_without_the_flag_missing_warehouse_access_only_warns(cfg, tmp_path, caplog):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.permissions.get.return_value = _acl("app-1")
    with caplog.at_level("WARNING"):
        log = bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps())
    assert not [
        c for c in w.permissions.update.call_args_list if c.kwargs["request_object_type"] == "warehouses"
    ]
    assert "app-2 may lack CAN_USE" in caplog.text and "Manual step" in caplog.text
    assert f"warning: app-2 may lack CAN_USE on SQL warehouse {cfg.workspace.sql_warehouse_id}" in log
    assert f"warning: app-1 may lack CAN_MANAGE on SQL warehouse {cfg.workspace.sql_warehouse_id}" in log
    w.permissions.get.return_value = _acl("app-1", "app-2", level="CAN_MANAGE")
    caplog.clear()
    with caplog.at_level("WARNING"):
        log = bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps())
    assert "CAN_USE" not in caplog.text and not any("warehouse" in line for line in log)


def test_a_refused_warehouse_grant_warns_and_bootstrap_continues(cfg, tmp_path, caplog):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.permissions.update.side_effect = lambda **kw: (_ for _ in ()).throw(PermissionDenied("no"))
    with caplog.at_level("WARNING"):
        log = bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path, results=_steps())
    assert any(line.startswith("warning: access on SQL warehouse") for line in log)
    assert "Manual step" in caplog.text and any(line.startswith("warning: Genie space") for line in log)


# --- ownership: a person bootstraps first, then ci owns the hub ---

CI, CI_EVAL = "app-1", "app-2"


class _Catalog:
    """Schema and table owners that CREATE SCHEMA statements update, like Unity Catalog."""

    def __init__(self, cfg, owner=DEPLOYER, missing=(), tables=None):
        from skillhub.registry.bootstrap import hub_schemas

        self.schemas = {s: owner for s in hub_schemas(cfg) if s not in missing}
        self.tables = tables or {}  # schema -> [(name, type, owner)]
        self.creator = DEPLOYER

    def get_schema(self, name):
        if name not in self.schemas:
            raise NotFound(name)
        return SimpleNamespace(owner=self.schemas[name])

    def list_tables(self, catalog_name, schema_name):
        from databricks.sdk.service.catalog import TableType

        return [
            SimpleNamespace(name=n, table_type=TableType(t), owner=o)
            for n, t, o in self.tables.get(f"{catalog_name}.{schema_name}", [])
        ]

    def sql(self):
        cat = self

        class Sql(FakeSql):
            def execute(self, statement, params=None):
                if statement.startswith("CREATE SCHEMA IF NOT EXISTS "):
                    cat.schemas.setdefault(statement.rsplit(" ", 1)[1], cat.creator)
                return super().execute(statement, params)

        return Sql()


def _owned_w(cfg, cat, user=DEPLOYER, catalog_owner=DEPLOYER):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.current_user.me.return_value = SimpleNamespace(user_name=user)
    w.catalogs.get.return_value = SimpleNamespace(owner=catalog_owner)
    w.schemas.get.side_effect = cat.get_schema
    w.tables.get.side_effect = lambda name: SimpleNamespace(owner=user)
    w.tables.list.side_effect = cat.list_tables
    return w


def _stmts(sql):
    return [c[0] for c in sql.calls]


def test_first_run_by_a_person_hands_schemas_and_tables_to_ci_and_keeps_views(cfg, tmp_path):
    from skillhub.registry.bootstrap import hub_schemas

    ev = evals_schema(cfg, "common")
    cat = _Catalog(
        cfg,
        missing=hub_schemas(cfg),  # a fresh catalog
        tables={
            ev: [
                ("eval_runs", "MANAGED", DEPLOYER),
                ("eval_matrix", "VIEW", DEPLOYER),
                ("ds_x", "MANAGED", CI_EVAL),
                ("ds_y", "MANAGED", DEPLOYER),  # made by a person's local eval run
            ]
        },
    )
    w, sql, calls = _owned_w(cfg, cat), cat.sql(), []
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps(calls))
    stmts = _stmts(sql)
    assert calls == ["tables", "views", ("root", "app-1", "app-2"), "alerts", "genie"]
    moved = [s for s in stmts if " OWNER TO " in s]
    assert f"ALTER TABLE {ev}.eval_runs OWNER TO `app-1`" in moved
    assert not any(s.startswith("ALTER VIEW") for s in moved)  # UC refuses a view hand-over to a principal
    assert not any("ds_x" in s for s in moved)  # ci_eval's dataset keeps its owner
    assert f"ALTER TABLE {ev}.ds_y OWNER TO `app-2`" in moved  # datasets belong to ci_eval, which writes them
    assert {s for s in moved if s.startswith("ALTER SCHEMA")} == {
        f"ALTER SCHEMA {s} OWNER TO `app-1`" for s in hub_schemas(cfg)
    }
    assert stmts[-len(moved) :] == moved  # last: every grant was applied while the person owned the schemas
    assert moved[0].startswith("ALTER TABLE")
    assert log[-1].startswith(f"handed {len(moved)} schemas and tables to the ci principal app-1; views stay")


def test_the_bootstrap_job_as_ci_passes_the_owner_checks_after_the_handover(cfg, tmp_path):
    cat = _Catalog(cfg, owner=CI)
    w, sql, calls = _owned_w(cfg, cat, user=CI), cat.sql(), []
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps(calls))
    stmts = _stmts(sql)
    assert calls == ["tables", "views", ("root", "app-1", "app-2"), "alerts", "genie"]
    assert not any(" OWNER TO " in s for s in stmts) and not any(s.startswith("CREATE SCHEMA") for s in stmts)
    assert not any(" ON CATALOG " in s for s in stmts)  # ci does not own the catalog
    assert any(s.startswith("GRANT USE SCHEMA") for s in stmts)  # ci owns the schemas, so it re-grants
    n = len([s for s in grant_plan(cfg, CI, CI_EVAL) if " ON CATALOG " in s])
    assert any(line.startswith(f"skipped {n} catalog-level grant statements") for line in log)


def test_the_bootstrap_job_as_ci_leaves_views_a_person_owns(cfg, tmp_path):
    """Views stay with the catalog owner, so the ci Job must not try to replace them."""
    cat = _Catalog(cfg, owner=CI)
    w, sql, calls = _owned_w(cfg, cat, user=CI), cat.sql(), []
    matrix = f"{evals_schema(cfg, next(iter(cfg.departments)))}.eval_matrix"
    w.tables.get.side_effect = lambda name: SimpleNamespace(owner=DEPLOYER if name == matrix else CI)
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps(calls))
    assert "tables" in calls and "views" not in calls
    assert any(
        line.startswith(f"results tables ensured; views left as they are: they are owned by {DEPLOYER}")
        for line in log
    )


def test_the_bootstrap_job_as_ci_with_the_real_tables_steps_creates_no_schema(cfg, tmp_path):
    """ci has no CREATE SCHEMA. With the real results tables and views steps, the Job still issues no
    CREATE SCHEMA, even IF NOT EXISTS: the schemas exist, and bootstrap checked them first."""
    from dataclasses import replace

    from skillhub.results import tables

    cat = _Catalog(cfg, owner=CI)
    w, sql = _owned_w(cfg, cat, user=CI), cat.sql()
    steps = replace(_steps(), ensure_tables=tables.ensure_tables, ensure_views=tables.ensure_views)
    bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=steps)
    stmts = _stmts(sql)
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS") for s in stmts)  # the real steps ran
    assert not [s for s in stmts if "CREATE SCHEMA" in s.upper() and not s.startswith("GRANT")]


def test_the_job_applies_catalog_grants_when_ci_owns_the_catalog(cfg, tmp_path):
    cat = _Catalog(cfg, owner=CI)
    w, sql = _owned_w(cfg, cat, user=CI, catalog_owner=CI), cat.sql()
    bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert "GRANT CREATE SCHEMA ON CATALOG skill_hub TO `app-2`" in _stmts(sql)


@pytest.mark.parametrize("user", [DEPLOYER, CI])
@pytest.mark.parametrize("stranger", [CI_EVAL, "someone@example.com"])
def test_owner_checks_refuse_ci_eval_and_any_other_principal(cfg, tmp_path, user, stranger):
    cat = _Catalog(cfg, owner=user)
    ev = evals_schema(cfg, "operations")
    cat.schemas[ev] = stranger
    w, sql = _owned_w(cfg, cat, user=user), cat.sql()
    with pytest.raises(
        BootstrapError, match=rf"schema {ev} is owned by '{stranger}'; bootstrap accepts only"
    ):
        bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert _granted(sql) == []


def test_the_job_refuses_a_schema_owned_by_the_person_who_has_not_handed_it_over(cfg, tmp_path):
    cat = _Catalog(cfg, owner=CI)
    cat.schemas[live_schema(cfg, "common")] = DEPLOYER
    w = _owned_w(cfg, cat, user=CI)
    with pytest.raises(BootstrapError, match=f"owned by '{DEPLOYER}'"):
        bootstrap(cfg, w, cat.sql(), create_identities=False, skills_root=tmp_path, results=_steps())


def test_the_job_cannot_create_a_missing_schema_and_says_who_can(cfg, tmp_path):
    ver = versions_schema(cfg, "operations")
    cat = _Catalog(cfg, owner=CI, missing=[ver])
    w, sql = _owned_w(cfg, cat, user=CI), cat.sql()
    with pytest.raises(BootstrapError, match=rf"{ver} do not exist, and ci cannot create schemas"):
        bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert sql.calls == []


def test_a_person_rerun_after_the_handover_creates_new_schemas_and_leaves_the_rest_to_the_job(cfg, tmp_path):
    new = [
        live_schema(cfg, "operations"),
        versions_schema(cfg, "operations"),
        evals_schema(cfg, "operations"),
    ]
    cat = _Catalog(cfg, owner=CI, missing=new)
    w, sql, calls = _owned_w(cfg, cat), cat.sql(), []
    log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps(calls))
    stmts = _stmts(sql)
    assert calls == []  # tables, views, hooks, alerts and Genie are the Job's
    assert [s for s in stmts if s.startswith("CREATE SCHEMA")] == [
        f"CREATE SCHEMA IF NOT EXISTS {s}" for s in new
    ]
    assert "GRANT CREATE SCHEMA ON CATALOG skill_hub TO `app-2`" in stmts  # the person owns the catalog
    assert not any(s.startswith("GRANT USE SCHEMA") for s in stmts)
    assert [s for s in stmts if " OWNER TO " in s] == [f"ALTER SCHEMA {s} OWNER TO `app-1`" for s in new]
    assert "run the skillhub-bootstrap Job" in log[-1]


def test_bootstrap_refuses_to_run_as_ci_eval(cfg, tmp_path):
    cat = _Catalog(cfg)
    w, sql = _owned_w(cfg, cat, user=CI_EVAL), cat.sql()
    with pytest.raises(BootstrapError, match="running as ci_eval"):
        bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert sql.calls == []


def test_ci_eval_gets_create_schema_only_after_every_production_schema_exists(cfg):
    plan = grant_plan(cfg, CI, CI_EVAL)
    grant = plan.index("GRANT CREATE SCHEMA ON CATALOG skill_hub TO `app-2`")
    assert grant == len(plan) - 1
    assert max(i for i, s in enumerate(plan) if s.startswith("CREATE SCHEMA")) < grant


def test_log_lines_are_emitted_before_a_later_failure(cfg, tmp_path):
    w = _new_sp_w()
    out, eval_out = tmp_path / "s", tmp_path / "e"
    seen = []

    def boom(c, s):
        raise RuntimeError("warehouse stopped")

    steps = ResultsSteps(boom, lambda c, s: None, lambda c, w: [], lambda c, w: "space-1", _root([]))
    with pytest.raises(RuntimeError):
        bootstrap(
            cfg, w, FakeSql(), create_identities=True, results=steps,
            secret_out=out, eval_secret_out=eval_out, emit=seen.append,
        )  # fmt: skip
    assert f"wrote OAuth secret for skillhub-ci to {out}" in seen
    assert any(line.startswith("applied ") for line in seen)


def test_the_read_only_job_falls_back_to_the_workspace_view_of_groups(cfg, tmp_path):
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    real = w.dir.do

    def refuse_admin_reads(method, path, **kw):
        if method == "GET":
            raise PermissionDenied("not an admin")
        return real(method, path, **kw)

    w.api_client.do.side_effect = refuse_admin_reads
    bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps())
    w2 = _w(existing_groups=required_groups(cfg)[1:], existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w2.api_client.do.side_effect = refuse_admin_reads
    with pytest.raises(BootstrapError, match=f"missing identities: account group {required_groups(cfg)[0]}"):
        bootstrap(cfg, w2, FakeSql(), create_identities=False, results=_steps())
    with pytest.raises(BootstrapError, match="needs a workspace admin"):
        bootstrap(cfg, w2, FakeSql(), create_identities=True, results=_steps())


def test_the_read_only_job_continues_when_it_cannot_list_groups(cfg, tmp_path, caplog):
    w = _w(existing_groups=[], existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    w.groups.list.side_effect = lambda **kw: (_ for _ in ()).throw(PermissionDenied("not an admin"))
    with caplog.at_level("WARNING"):
        bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps())
    assert "cannot list workspace groups" in caplog.text
    _no_identity_writes(w)
    with pytest.raises(BootstrapError, match="needs a workspace admin"):
        bootstrap(cfg, w, FakeSql(), create_identities=True, skills_root=tmp_path, results=_steps())


def test_handover_skips_a_schema_whose_tables_cannot_be_listed(cfg, tmp_path, caplog):
    new = [
        live_schema(cfg, "operations"),
        versions_schema(cfg, "operations"),
        evals_schema(cfg, "operations"),
    ]
    cat = _Catalog(cfg, owner=CI, missing=new)
    w, sql = _owned_w(cfg, cat), cat.sql()
    w.tables.list.side_effect = lambda **kw: (_ for _ in ()).throw(PermissionDenied("no USE SCHEMA"))
    with caplog.at_level("WARNING"):
        log = bootstrap(cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps())
    assert [s for s in _stmts(sql) if " OWNER TO " in s] == [
        f"ALTER SCHEMA {s} OWNER TO `app-1`" for s in new
    ]
    assert "keep their owner" in caplog.text and "run the skillhub-bootstrap Job" in log[-1]


def test_the_experiment_root_is_locked_before_the_hooks_create_experiments(cfg, tmp_path, monkeypatch):
    order = []
    exps = types.ModuleType("skillhub.results.experiments")
    exps.ensure_experiment = lambda c, dept, skill: order.append("experiment") or "exp-1"
    monkeypatch.setitem(sys.modules, "skillhub.results.experiments", exps)
    (tmp_path / "common" / "audit-xls").mkdir(parents=True)
    (tmp_path / "common" / "audit-xls" / "SKILL.md").write_text(
        "---\nname: audit-xls\ndescription: d\n---\nb\n"
    )
    w = _w(existing_groups=required_groups(cfg), existing_sps=[cfg.roles.ci, cfg.roles.ci_eval])
    log = bootstrap(cfg, w, FakeSql(), create_identities=False, skills_root=tmp_path, results=_steps(order))
    assert order.index(("root", "app-1", "app-2")) < order.index("experiment")
    assert "experiment root locked for app-1, app-2" in log


def test_bootstrap_needs_the_experiment_root_step(monkeypatch):
    """Locking the experiment folder is never skipped: without the results step that locks the folder, bootstrap stops."""
    for name, attrs in (
        ("skillhub.results.tables", ("ensure_tables", "ensure_views")),
        ("skillhub.results.alerts", ("ensure_alerts",)),
        ("skillhub.results.genie", ("ensure_genie_space",)),
        ("skillhub.results.experiments", ()),
    ):
        mod = types.ModuleType(name)
        for a in attrs:
            setattr(mod, a, lambda *a, **k: None)
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setitem(sys.modules, "skillhub.results", types.ModuleType("skillhub.results"))
    with pytest.raises(BootstrapError, match="skillhub.results.experiments.ensure_experiment_root"):
        default_results_steps()


# --- The person's first run hands the alerts and the Genie space to ci ---


def _steps_with_hand_over(calls):
    import dataclasses

    return dataclasses.replace(
        _steps(calls),
        hand_over_alerts=lambda w, ids, owner: (
            calls.append(("alerts to", tuple(ids), owner)) or ["alerts handed"]
        ),
        hand_over_genie=lambda w, space, owner: calls.append(("genie to", space, owner)) or "genie handed",
    )


def test_first_run_hands_the_alerts_and_the_genie_space_to_ci(cfg, tmp_path):
    from skillhub.registry.bootstrap import hub_schemas

    cat = _Catalog(cfg, missing=hub_schemas(cfg))
    w, sql, calls = _owned_w(cfg, cat), cat.sql(), []
    log = bootstrap(
        cfg, w, sql, create_identities=False, skills_root=tmp_path, results=_steps_with_hand_over(calls)
    )
    assert calls[-2:] == [("alerts to", ("a1", "a2"), "app-1"), ("genie to", "space-1", "app-1")]
    assert "alerts handed" in log and "genie handed" in log
    assert log.index("genie handed") < len(log) - 1  # before the schema hand-over line
