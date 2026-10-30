from types import SimpleNamespace as NS

import pytest
from mlflow.entities import UnityCatalog
from typer.testing import CliRunner

from skillhub import cli
from skillhub.naming import evals_schema
from skillhub.results import experiments as exp
from tests.fakes import make_cfg

SP_ID = "11111111-2222-3333-4444-555555555555"


# Workspace folders: path -> object id. The experiment root of the test catalog is 300.
DIRS = {
    "/Shared": 100,
    "/Shared/skill-hub": 200,
    "/Shared/skill-hub/skill_hub": 300,
    "/Shared/skill-hub/skill_hub/candidates": 310,
    "/Shared/skill-hub/skill_hub/drafts": 320,
}


class FakeW:
    def __init__(self, grants=(), acl=None, groups=(), sps=(), dirs=None, me="ci-eval-app-id"):
        self._grants = list(grants)
        self._acl = acl or {}
        self._groups = list(groups)
        self._sps = list(sps)
        self._dirs = dict(DIRS if dirs is None else dirs)
        self.updates = []
        self.sets = []
        self.mkdirs = []
        self.effective_calls = []
        self.grants = NS(get_effective=self._get_effective)
        self.permissions = NS(
            get=lambda request_object_type, request_object_id: NS(
                access_control_list=self._acl.get(request_object_id, [])
            ),
            update=self._update,
            set=self._set,
        )
        self.workspace = NS(get_status=self._get_status, mkdirs=self._mkdirs)
        self.groups = NS(list=lambda **kw: iter([NS(display_name=g) for g in self._groups]))
        self.service_principals = NS(list=lambda **kw: iter(self._sps))
        self.current_user = NS(me=lambda: NS(user_name=me))

    def _get_effective(self, **kw):
        self.effective_calls.append(kw)
        return NS(privilege_assignments=self._grants, next_page_token=None)

    def _update(self, request_object_type, request_object_id, access_control_list):
        self.updates.append((request_object_id, access_control_list))

    def _set(self, request_object_type, request_object_id, access_control_list):
        assert request_object_type == "directories"
        self.sets.append((request_object_id, access_control_list))

    def _get_status(self, path):
        from databricks.sdk.errors import NotFound

        if path not in self._dirs:
            raise NotFound(path)
        return NS(object_id=self._dirs[path])

    def _mkdirs(self, path):
        self.mkdirs.append(path)
        self._dirs.setdefault(path, 1000 + len(self._dirs))


def entry(group=None, user=None, sp=None, level="CAN_READ", inherited_from=None):
    return NS(
        group_name=group,
        user_name=user,
        service_principal_name=sp,
        all_permissions=[
            NS(
                permission_level=NS(value=level),
                inherited=inherited_from is not None,
                inherited_from_object=[inherited_from] if inherited_from else None,
            )
        ],
    )


def grant(principal, *privs):
    return NS(principal=principal, privileges=[NS(privilege=NS(value=p)) for p in privs])


@pytest.fixture(autouse=True)
def _isolated_mlflow(monkeypatch):
    """No tracking server, and no leaked environment between tests."""
    # setenv first so monkeypatch always records a restore entry, even when the variable starts unset
    monkeypatch.setenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", "")
    monkeypatch.delenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID")
    monkeypatch.setattr(exp.mlflow, "set_tracking_uri", lambda uri: None)


def test_ensure_experiment_binds_uc_location_on_creation(monkeypatch):
    cfg = make_cfg()
    created = []
    looks = iter(
        [
            None,
            NS(
                experiment_id="7",
                trace_location=UnityCatalog("skill_hub", "common_evals", table_prefix="traces"),
            ),
        ]
    )
    monkeypatch.setattr(exp.mlflow, "get_experiment_by_name", lambda path: next(looks))
    monkeypatch.setattr(
        exp.mlflow,
        "set_experiment",
        lambda experiment_name, trace_location: created.append((experiment_name, trace_location)),
    )
    assert exp.ensure_experiment(cfg, "common", "earnings-preview") == "7"
    ((path, loc),) = created
    assert path == "/Shared/skill-hub/skill_hub/common/earnings-preview"
    assert loc == UnityCatalog(*evals_schema(cfg, "common").split("."), table_prefix="traces")
    assert exp.os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] == "test-warehouse"


def test_ensure_experiment_is_idempotent_when_binding_matches(monkeypatch):
    cfg = make_cfg()
    loc = UnityCatalog("skill_hub", "common_evals", table_prefix="traces")
    monkeypatch.setattr(
        exp.mlflow, "get_experiment_by_name", lambda path: NS(experiment_id="7", trace_location=loc)
    )

    def boom(**kw):
        raise AssertionError("must not recreate an existing experiment")

    monkeypatch.setattr(exp.mlflow, "set_experiment", boom)
    assert exp.ensure_experiment(cfg, "common", "earnings-preview") == "7"


def test_ensure_experiment_refuses_existing_experiment_without_binding(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp.mlflow, "get_experiment_by_name", lambda path: NS(experiment_id="7", trace_location=None)
    )
    with pytest.raises(RuntimeError, match="trace location"):
        exp.ensure_experiment(cfg, "common", "earnings-preview")


COMMON_TRACES = UnityCatalog("skill_hub", "common_evals", table_prefix="traces")
UNRELEASED = "/Shared/skill-hub/skill_hub/common/_unreleased"


def _no_create(**kw):
    raise AssertionError("an eval or a Try must never create an experiment")


def test_ensure_unreleased_experiment_creates_the_department_experiment_bound_to_its_traces(monkeypatch):
    created = []
    looks = iter([None, NS(experiment_id="8", trace_location=COMMON_TRACES)])
    monkeypatch.setattr(exp.mlflow, "get_experiment_by_name", lambda path: next(looks))
    monkeypatch.setattr(
        exp.mlflow,
        "set_experiment",
        lambda experiment_name, trace_location: created.append((experiment_name, trace_location)),
    )
    assert exp.ensure_unreleased_experiment(make_cfg(), "common") == "8"
    assert created == [(UNRELEASED, COMMON_TRACES)]


def test_eval_experiment_uses_the_skill_experiment_when_it_exists(monkeypatch):
    found = {
        "/Shared/skill-hub/skill_hub/common/audit-xls": NS(experiment_id="7", trace_location=COMMON_TRACES)
    }
    found[UNRELEASED] = NS(experiment_id="8", trace_location=COMMON_TRACES)
    monkeypatch.setattr(exp.mlflow, "get_experiment_by_name", found.get)
    monkeypatch.setattr(exp.mlflow, "set_experiment", _no_create)
    assert exp.eval_experiment(make_cfg(), "common", "audit-xls") == "7"


def test_eval_experiment_falls_back_to_the_unreleased_experiment_for_a_new_skill(monkeypatch):
    """A draft or a pull request that adds a skill: ci_eval and the app cannot create its experiment."""
    looked = []
    found = {UNRELEASED: NS(experiment_id="8", trace_location=COMMON_TRACES)}
    monkeypatch.setattr(
        exp.mlflow, "get_experiment_by_name", lambda path: looked.append(path) or found.get(path)
    )
    monkeypatch.setattr(exp.mlflow, "set_experiment", _no_create)
    assert exp.eval_experiment(make_cfg(), "common", "estimate-revision") == "8"
    assert looked == ["/Shared/skill-hub/skill_hub/common/estimate-revision", UNRELEASED]
    assert exp.os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] == "test-warehouse"


def test_eval_experiment_fails_closed_without_the_unreleased_experiment(monkeypatch):
    monkeypatch.setattr(exp.mlflow, "get_experiment_by_name", lambda path: None)
    monkeypatch.setattr(exp.mlflow, "set_experiment", _no_create)
    with pytest.raises(RuntimeError, match="Run bootstrap"):
        exp.eval_experiment(make_cfg(), "common", "estimate-revision")


@pytest.mark.parametrize(
    "location", [None, UnityCatalog("skill_hub", "external_evals", table_prefix="traces")]
)
def test_eval_experiment_refuses_a_wrong_binding_instead_of_falling_back(monkeypatch, location):
    found = {
        "/Shared/skill-hub/skill_hub/common/audit-xls": NS(experiment_id="7", trace_location=location),
        UNRELEASED: NS(experiment_id="8", trace_location=COMMON_TRACES),
    }
    monkeypatch.setattr(exp.mlflow, "get_experiment_by_name", found.get)
    with pytest.raises(RuntimeError, match="audit-xls"):
        exp.eval_experiment(make_cfg(), "common", "audit-xls")


def test_derived_acl_maps_select_to_read_and_modify_to_edit():
    cfg = make_cfg()
    w = FakeW(grants=[grant("g-common-readers", "SELECT"), grant("ci-sp", "MODIFY", "SELECT")])
    assert exp.derived_acl(w, cfg, "common") == {"g-common-readers": "CAN_READ", "ci-sp": "CAN_EDIT"}


def test_derived_acl_reads_table_level_grants_on_eval_runs():
    cfg = make_cfg()
    w = FakeW(grants=[grant("ci_eval", "MODIFY", "SELECT")])
    assert exp.derived_acl(w, cfg, "common") == {"ci_eval": "CAN_EDIT"}
    assert w.effective_calls == [
        {
            "securable_type": "table",
            "full_name": f"{evals_schema(cfg, 'common')}.eval_runs",
            "page_token": None,
        }
    ]


def test_ci_with_modify_only_on_skill_versions_gets_no_edit():
    # Effective grants on eval_runs for `ci` hold only inherited SELECT: its MODIFY is on skill_versions.
    w = FakeW(grants=[grant("ci", "SELECT"), grant("ci_eval", "MODIFY")])
    acl = exp.derived_acl(w, make_cfg(), "common")
    assert acl == {"ci": "CAN_READ", "ci_eval": "CAN_EDIT"}


def test_apply_experiment_acl_sends_group_requests():
    cfg = make_cfg()
    w = FakeW(grants=[grant("g-er", "SELECT")], groups=["g-er"])
    exp.apply_experiment_acl(w, cfg, "equity_research", "9")
    ((eid, reqs),) = w.updates
    assert eid == "9"
    assert reqs[0].group_name == "g-er"
    assert reqs[0].permission_level.value == "CAN_READ"


def test_apply_experiment_acl_uses_application_id_for_service_principal():
    cfg = make_cfg()
    w = FakeW(grants=[grant(SP_ID, "MODIFY")])  # no group or SP lookup should be needed
    exp.apply_experiment_acl(w, cfg, "common", "9")
    ((_, reqs),) = w.updates
    assert reqs[0].service_principal_name == SP_ID
    assert reqs[0].permission_level.value == "CAN_EDIT"


def test_apply_experiment_acl_resolves_sp_display_name():
    cfg = make_cfg()
    w = FakeW(grants=[grant("ci-sp", "MODIFY")], sps=[NS(application_id=SP_ID)])
    exp.apply_experiment_acl(w, cfg, "common", "9")
    ((_, reqs),) = w.updates
    assert reqs[0].service_principal_name == SP_ID


def _acl_drifts(cfg, w):
    """check_permissions without the missing unreleased experiments, which tests that stub only skill experiments
    would otherwise all report."""
    return [d for d in exp.check_permissions(cfg, w) if d.kind != "missing_experiment"]


def _two_experiments(monkeypatch, **tags):
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [
            NS(
                name="/Shared/skill-hub/skill_hub/equity_research/earnings-preview",
                experiment_id="1",
                tags=tags,
            ),
            NS(name="/Shared/skill-hub/skill_hub/equity_research/model-update", experiment_id="2", tags=tags),
        ],
    )


def test_check_permissions_reports_missing_and_extra_readers(monkeypatch):
    cfg = make_cfg()
    _two_experiments(monkeypatch)
    acl = {
        "1": [],  # g-er missing
        "2": [entry(group="g-er"), entry(group="g-intruder")],  # extra reader
    }
    w = FakeW(grants=[grant("g-er", "SELECT")], acl=acl, groups=["g-er", "g-intruder"])
    drifts = _acl_drifts(cfg, w)
    kinds = sorted((d.experiment.rsplit("/", 1)[1], d.principal, d.kind) for d in drifts)
    assert kinds == [("earnings-preview", "g-er", "missing"), ("model-update", "g-intruder", "unexpected")]


def test_check_permissions_configures_mlflow_first(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(exp, "skill_experiments", lambda cfg: [])
    seen = []
    monkeypatch.setattr(exp, "configure_mlflow", lambda c: seen.append(c))
    exp.check_permissions(cfg, FakeW())
    assert seen == [cfg]


def test_check_permissions_clean_when_acl_matches(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [
            NS(
                name="/Shared/skill-hub/skill_hub/equity_research/earnings-preview",
                experiment_id="1",
                tags={},
            )
        ],
    )
    w = FakeW(grants=[grant("g-er", "SELECT")], acl={"1": [entry(group="g-er")]})
    assert _acl_drifts(cfg, w) == []


def _unreleased(cfg, first_id=50):
    return [
        NS(name=f"/Shared/skill-hub/skill_hub/{d}/_unreleased", experiment_id=str(first_id + i), tags={})
        for i, d in enumerate(cfg.departments)
    ]


def test_check_permissions_reports_each_missing_unreleased_experiment(monkeypatch):
    cfg = make_cfg()
    present = _unreleased(cfg)
    monkeypatch.setattr(exp, "skill_experiments", lambda cfg: present[1:])
    w = FakeW(grants=[grant("g-er", "SELECT")], acl={e.experiment_id: [entry(group="g-er")] for e in present})
    drifts = exp.check_permissions(cfg, w)
    assert [(d.experiment, d.principal, d.kind) for d in drifts] == [
        (present[0].name, "-", "missing_experiment")
    ]
    assert exp.fails_check(drifts[0])
    monkeypatch.setattr(exp, "skill_experiments", lambda cfg: present)
    assert exp.check_permissions(cfg, w) == []  # bootstrap's setup is clean


def test_check_permissions_checks_the_unreleased_experiment_acl_like_a_skill_experiment(monkeypatch):
    """ci_eval holds CAN_EDIT through the root; MANAGE on the unreleased experiment is drift, like anywhere else."""
    cfg = make_cfg()
    present = _unreleased(cfg)
    monkeypatch.setattr(exp, "skill_experiments", lambda cfg: present)
    acl = {e.experiment_id: [entry(group="g-er"), entry(sp=SP_ID, level="CAN_EDIT")] for e in present}
    acl["50"] = [
        entry(group="g-er"),
        entry(sp=SP_ID, level="CAN_MANAGE"),
        entry(group="users", level="CAN_EDIT"),
    ]
    w = FakeW(grants=[grant("g-er", "SELECT"), grant(SP_ID, "MODIFY")], acl=acl)
    assert sorted((d.experiment, d.principal, d.kind) for d in exp.check_permissions(cfg, w)) == [
        (present[0].name, SP_ID, "excessive"),
        (present[0].name, "users", "unexpected"),
    ]


def test_check_permissions_flags_unknown_department(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [NS(name="/Shared/skill-hub/skill_hub/ghost/x", experiment_id="3", tags={})],
    )
    drifts = _acl_drifts(cfg, FakeW())
    assert [d.kind for d in drifts] == ["unknown_department"]


def test_check_permissions_tolerates_experiment_directly_under_root(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [NS(name="/Shared/skill-hub/skill_hub/stray", experiment_id="3", tags={})],
    )
    assert [d.kind for d in _acl_drifts(cfg, FakeW())] == ["unknown_department"]


def test_can_manage_is_flagged_except_for_owner_and_admins(monkeypatch):
    cfg = make_cfg()
    _two_experiments(monkeypatch, **{"mlflow.ownerEmail": "owner@example.com"})
    acl = {
        "1": [
            entry(group="g-er"),
            entry(user="owner@example.com", level="CAN_MANAGE"),
            entry(group="admins", level="CAN_MANAGE"),
            entry(group="users", level="CAN_MANAGE"),  # inherited broad ACL must be flagged
        ],
        "2": [entry(group="g-er")],
    }
    w = FakeW(grants=[grant("g-er", "SELECT")], acl=acl)
    drifts = _acl_drifts(cfg, w)
    assert [(d.experiment.rsplit("/", 1)[1], d.principal, d.kind) for d in drifts] == [
        ("earnings-preview", "users", "unexpected")
    ]


def test_can_edit_for_modify_holder_satisfied_only_by_edit_or_higher(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [NS(name="/Shared/skill-hub/skill_hub/equity_research/a", experiment_id="1", tags={})],
    )
    w = FakeW(grants=[grant(SP_ID, "MODIFY")], acl={"1": [entry(sp=SP_ID, level="CAN_READ")]})
    drifts = _acl_drifts(cfg, w)
    assert [(d.principal, d.kind) for d in drifts] == [(SP_ID, "missing")]


def test_cli_check_permissions_exit_codes(monkeypatch):
    from skillhub.results import experiments

    monkeypatch.setenv("DATABRICKS_HOST", "https://example.invalid")
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "test-warehouse")
    monkeypatch.setenv("SKILLHUB_CATALOG", "skill_hub")
    monkeypatch.setattr("skillhub.auth.workspace_client", lambda: object())
    monkeypatch.setattr(experiments, "check_permissions", lambda cfg, w: [])
    runner = CliRunner()
    r = runner.invoke(cli.app, ["check-permissions"])
    assert r.exit_code == 0 and "ok:" in r.output
    drift = experiments.Drift("/Shared/skill-hub/common/x", "p", "missing", "d")
    monkeypatch.setattr(experiments, "check_permissions", lambda cfg, w: [drift])
    r = runner.invoke(cli.app, ["check-permissions"])
    assert r.exit_code == 1 and '"kind": "missing"' in r.output


def test_cli_check_permissions_reports_inherited_users_access_without_failing(monkeypatch):
    """With workspace_root under /Workspace/Shared, `users` inherits access. It is reported every night, but
    the nightly Job does not fail on it, so a real eval or reconcile failure is not hidden."""
    from skillhub.results import experiments

    monkeypatch.setenv("DATABRICKS_HOST", "https://example.invalid")
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "test-warehouse")
    monkeypatch.setenv("SKILLHUB_CATALOG", "skill_hub")
    monkeypatch.setattr("skillhub.auth.workspace_client", lambda: object())
    users = experiments.Drift(ROOT, "users", "inherited_access", "CAN_MANAGE inherited from /directories/100")
    monkeypatch.setattr(experiments, "check_permissions", lambda cfg, w: [users])
    r = CliRunner().invoke(cli.app, ["check-permissions"])
    assert r.exit_code == 0 and '"kind": "inherited_access"' in r.output and "warning:" in r.output
    other = experiments.Drift(ROOT, "mallory@example.com", "inherited_access", "CAN_EDIT inherited")
    monkeypatch.setattr(experiments, "check_permissions", lambda cfg, w: [users, other])
    assert CliRunner().invoke(cli.app, ["check-permissions"]).exit_code == 1


@pytest.mark.parametrize(
    "priv,held",
    [("SELECT", "CAN_MANAGE"), ("SELECT", "CAN_EDIT"), ("MODIFY", "CAN_MANAGE")],
)
def test_over_privileged_principal_is_flagged_excessive(monkeypatch, priv, held):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [NS(name="/Shared/skill-hub/skill_hub/equity_research/a", experiment_id="1", tags={})],
    )
    w = FakeW(grants=[grant("g-er", priv)], acl={"1": [entry(group="g-er", level=held)]})
    assert [(d.principal, d.kind) for d in _acl_drifts(cfg, w)] == [("g-er", "excessive")]


def test_tag_claimed_owner_without_grant_is_flagged_unless_can_manage(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [
            NS(
                name="/Shared/skill-hub/skill_hub/equity_research/a",
                experiment_id="1",
                tags={"mlflow.ownerEmail": "mallory@example.com"},
            )
        ],
    )
    w = FakeW(acl={"1": [entry(user="mallory@example.com", level="CAN_EDIT")]})
    assert [(d.principal, d.kind) for d in _acl_drifts(cfg, w)] == [("mallory@example.com", "unexpected")]


def test_mlflow_env_does_not_leak_between_tests():
    import os

    assert (
        "MLFLOW_TRACING_SQL_WAREHOUSE_ID" not in os.environ
        or not os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"]
    )


# --- Experiment root ACL, root drift and trusted run authors ---

CI, CI_EVAL, APP = (f"{n}1111111-2222-3333-4444-555555555555" for n in ("a", "b", "c"))
ROOT = "/Shared/skill-hub/skill_hub"


def _acl_of(reqs) -> dict[str, str]:
    return {(r.group_name or r.service_principal_name or r.user_name): r.permission_level.value for r in reqs}


def test_ensure_experiment_root_replaces_folder_acls_and_gives_users_nothing(monkeypatch):
    monkeypatch.delenv("SKILLHUB_APP_PRINCIPAL", raising=False)
    cfg = make_cfg()
    w = FakeW()
    lines = exp.ensure_experiment_root(cfg, w, ci=CI, ci_eval=CI_EVAL, app=APP)
    assert w.mkdirs[0] == ROOT
    assert set(w.mkdirs[1:]) == {
        f"{ROOT}/candidates",
        f"{ROOT}/drafts",
        *(f"{ROOT}/{d}" for d in cfg.departments),
    }
    (root_id, root), (cand_id, cand), (drafts_id, drafts), (base_id, base) = (
        w.sets
    )  # root first: replacing the base may drop the caller
    assert (base_id, root_id, cand_id, drafts_id) == ("200", "300", "310", "320")
    # Creating a run's candidate folder needs MANAGE; ci_eval gets it on the candidates folder only.
    assert _acl_of(cand) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_MANAGE"}
    # The app creates Skill Hub drafts in drafts/ only: it never gets MANAGE on the PR uploads in candidates/.
    assert _acl_of(drafts) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_MANAGE", APP: "CAN_MANAGE"}
    assert _acl_of(base) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_EDIT"}
    assert _acl_of(root) == {
        CI: "CAN_MANAGE",
        CI_EVAL: "CAN_EDIT",
        APP: "CAN_EDIT",
        cfg.roles.reviewers: "CAN_READ",
    }
    assert "users" not in _acl_of(base) | _acl_of(root)
    assert not [ln for ln in lines if ln.startswith("warning")]
    assert w.updates == []  # set, never a merging update


def test_without_the_app_principal_the_drafts_folder_has_only_the_ci_principals(monkeypatch):
    monkeypatch.delenv("SKILLHUB_APP_PRINCIPAL", raising=False)
    w = FakeW()
    lines = exp.ensure_experiment_root(make_cfg(), w, ci=CI, ci_eval=CI_EVAL)
    drafts = next(acl for oid, acl in w.sets if oid == "320")
    assert _acl_of(drafts) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_MANAGE"}
    assert any(ln.startswith("warning") and "SKILLHUB_APP_PRINCIPAL" in ln for ln in lines)


def test_ensure_experiment_root_reads_the_app_principal_from_the_environment(monkeypatch):
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", APP)
    w = FakeW()
    exp.ensure_experiment_root(make_cfg(), w, ci=CI, ci_eval=CI_EVAL)
    assert _acl_of(w.sets[0][1])[APP] == "CAN_EDIT"


def test_ensure_experiment_root_warns_without_an_app_principal(monkeypatch, caplog):
    monkeypatch.delenv("SKILLHUB_APP_PRINCIPAL", raising=False)
    w = FakeW()
    with caplog.at_level("WARNING"):
        lines = exp.ensure_experiment_root(make_cfg(), w, ci=CI, ci_eval=CI_EVAL)
    assert any("SKILLHUB_APP_PRINCIPAL is not set" in ln for ln in lines)
    assert "SKILLHUB_APP_PRINCIPAL" in caplog.text


def test_ensure_experiment_root_warns_about_access_inherited_from_shared(monkeypatch, caplog):
    """Folder ACLs only add: the default `users` CAN_MANAGE on /Workspace/Shared still reaches the root."""
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", APP)
    acl = {
        "300": [
            entry(group="users", level="CAN_MANAGE", inherited_from="/directories/100"),
            entry(sp=CI, level="CAN_MANAGE", inherited_from="/directories/200"),  # our own base ACL
            entry(group="admins", level="CAN_MANAGE", inherited_from="/directories/100"),
        ]
    }
    w = FakeW(acl=acl)
    with caplog.at_level("WARNING"):
        lines = exp.ensure_experiment_root(make_cfg(), w, ci=CI, ci_eval=CI_EVAL)
    warnings = [ln for ln in lines if ln.startswith("warning")]
    assert len(warnings) == 1
    assert "users has CAN_MANAGE" in warnings[0] and "/Workspace/Shared" in warnings[0]


def test_check_root_acl_reports_a_missing_root():
    drifts = exp.check_root_acl(make_cfg(), FakeW(dirs={"/Shared": 100}))
    assert [(d.experiment, d.kind) for d in drifts] == [(ROOT, "missing_root")]


def test_check_root_acl_reports_inherited_access_once(monkeypatch):
    monkeypatch.delenv("SKILLHUB_CI_PRINCIPAL", raising=False)
    acl = {"300": [entry(group="users", level="CAN_MANAGE", inherited_from="/directories/100")]}
    drifts = exp.check_root_acl(make_cfg(), FakeW(acl=acl))
    assert [(d.principal, d.kind) for d in drifts] == [("users", "inherited_access")]


def test_check_root_acl_flags_direct_users_and_unknown_principals(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setenv("SKILLHUB_CI_PRINCIPAL", CI)
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", CI_EVAL)
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", APP)
    good = [
        entry(sp=CI, level="CAN_MANAGE"),
        entry(sp=CI_EVAL, level="CAN_EDIT"),
        entry(sp=APP, level="CAN_EDIT"),
        entry(group=cfg.roles.reviewers),
        entry(group="admins", level="CAN_MANAGE"),
    ]
    assert exp.check_root_acl(cfg, FakeW(acl={"300": good})) == []
    bad = [*good, entry(group="users", level="CAN_EDIT"), entry(user="mallory@example.com", level="CAN_EDIT")]
    drifts = exp.check_root_acl(cfg, FakeW(acl={"300": bad}))
    assert sorted((d.principal, d.kind) for d in drifts) == [
        ("mallory@example.com", "unexpected"),
        ("users", "unexpected"),
    ]


def test_check_root_acl_without_principal_ids_still_flags_users(monkeypatch):
    for name in ("SKILLHUB_CI_PRINCIPAL", "SKILLHUB_CI_EVAL_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    acl = {"300": [entry(group="users", level="CAN_READ"), entry(user="someone@example.com")]}
    assert [(d.principal, d.kind) for d in exp.check_root_acl(make_cfg(), FakeW(acl=acl))] == [
        ("users", "unexpected")
    ]


def test_check_permissions_reports_root_inheritance_once_not_per_experiment(monkeypatch):
    """The root ACL reaches every experiment by inheritance; it is reported at the root only."""
    monkeypatch.delenv("SKILLHUB_CI_PRINCIPAL", raising=False)
    cfg = make_cfg()
    _two_experiments(monkeypatch)
    users = entry(group="users", level="CAN_MANAGE", inherited_from="/directories/100")
    ci = entry(sp=CI, level="CAN_MANAGE", inherited_from="/directories/300")
    acl = {"300": [users], "1": [entry(group="g-er"), users, ci], "2": [entry(group="g-er"), users, ci]}
    w = FakeW(grants=[grant("g-er", "SELECT")], acl=acl)
    drifts = _acl_drifts(cfg, w)
    assert [(d.experiment, d.principal, d.kind) for d in drifts] == [(ROOT, "users", "inherited_access")]


def test_check_permissions_still_flags_grants_set_on_the_experiment(monkeypatch):
    cfg = make_cfg()
    _two_experiments(monkeypatch)
    acl = {"1": [entry(group="g-er"), entry(group="users", level="CAN_EDIT")], "2": [entry(group="g-er")]}
    w = FakeW(grants=[grant("g-er", "SELECT")], acl=acl)
    assert [(d.principal, d.kind) for d in _acl_drifts(cfg, w)] == [("users", "unexpected")]


def test_skill_experiments_ignore_other_catalogs(monkeypatch):
    """Dev and prod experiments live under different roots; each target reads only its own."""
    cfg = make_cfg()

    class C:
        def search_experiments(self, page_token, max_results):
            return _ExpPage(
                [
                    NS(name=f"{ROOT}/equity_research/a", experiment_id="1", tags={}),
                    NS(name="/Shared/skill-hub/skill_hub_dev/equity_research/a", experiment_id="2", tags={}),
                    NS(name="/Shared/skill-hub/equity_research/a", experiment_id="3", tags={}),
                ]
            )

    monkeypatch.setattr(exp, "MlflowClient", C)
    assert [e.experiment_id for e in exp.skill_experiments(cfg)] == ["1"]


class _ExpPage(list):
    token = None


def test_trusted_run_authors_are_the_env_principals_and_the_caller(monkeypatch):
    """The app principal is not trusted; it never creates eval runs, and Hub evals run as ci_eval."""
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", CI_EVAL)
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", APP)
    monkeypatch.setenv("SKILLHUB_DEPLOYER", "deployer@example.com")
    assert exp.trusted_run_authors(FakeW(me="me@example.com")) == {
        CI_EVAL,
        "deployer@example.com",
        "me@example.com",
    }


def test_trusted_run_authors_ignore_blank_values_and_survive_a_failed_identity_call(monkeypatch, caplog):
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", CI_EVAL)
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", "  ")
    monkeypatch.delenv("SKILLHUB_DEPLOYER", raising=False)

    def boom():
        raise RuntimeError("no network")

    w = NS(current_user=NS(me=boom))
    with caplog.at_level("WARNING"):
        assert exp.trusted_run_authors(w) == {CI_EVAL}
    assert "could not read the current identity" in caplog.text


def test_check_root_acl_reports_an_unreadable_acl_instead_of_failing():
    from databricks.sdk.errors import PermissionDenied

    w = FakeW()

    def denied(request_object_type, request_object_id):
        raise PermissionDenied("no CAN_MANAGE")

    w.permissions.get = denied
    assert [(d.kind, d.principal) for d in exp.check_root_acl(make_cfg(), w)] == [("unreadable", "-")]


def test_bootstrap_resolves_the_experiment_root_step_and_its_call_binds():
    """bootstrap calls steps.ensure_experiment_root(cfg, w, ci=..., ci_eval=...) before run_hooks. Its default
    step is this function, and that call binds to its signature."""
    import inspect

    from skillhub.registry.bootstrap import default_results_steps

    step = default_results_steps().ensure_experiment_root
    assert step is exp.ensure_experiment_root
    inspect.signature(step).bind(make_cfg(), object(), ci="app-1", ci_eval="app-2")  # TypeError on drift


# --- The workspace root lives in the ci principal's home folder ---

HOME = f"/Users/{CI}"
HOME_DIRS = {
    "/Users": 10,
    HOME: 20,
    f"{HOME}/skill-hub": 30,
    f"{HOME}/skill-hub/skill_hub": 40,
    f"{HOME}/skill-hub/skill_hub/candidates": 41,
}


def _home_cfg(monkeypatch):
    monkeypatch.setenv("SKILLHUB_WORKSPACE_ROOT", f"{HOME}/skill-hub")
    return make_cfg()


def test_the_experiment_root_follows_workspace_root(monkeypatch):
    cfg = _home_cfg(monkeypatch)
    assert exp.experiment_path(cfg, "common", "a") == f"{HOME}/skill-hub/skill_hub/common/a"
    w = FakeW(dirs=HOME_DIRS)
    exp.ensure_experiment_root(cfg, w, ci=CI, ci_eval=CI_EVAL, app=APP)
    assert w.mkdirs[0] == f"{HOME}/skill-hub/skill_hub"
    (root_id, root), (_, cand), (_, drafts), (base_id, base) = w.sets
    assert (root_id, base_id) == ("40", "30")
    assert _acl_of(cand) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_MANAGE"}
    assert _acl_of(drafts) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_MANAGE", APP: "CAN_MANAGE"}
    assert _acl_of(base) == {CI: "CAN_MANAGE", CI_EVAL: "CAN_EDIT"}
    assert "users" not in _acl_of(root)


def test_ci_inheriting_manage_from_its_home_folder_is_not_a_warning(monkeypatch, caplog):
    """The default root is in ci's home: ci inherits CAN_MANAGE from there, and `users` inherits nothing."""
    cfg = _home_cfg(monkeypatch)
    acl = {
        "40": [
            entry(sp=CI, level="CAN_MANAGE", inherited_from="/directories/20"),
            entry(group="admins", level="CAN_MANAGE", inherited_from="/directories/20"),
        ]
    }
    with caplog.at_level("WARNING"):
        lines = exp.ensure_experiment_root(
            cfg, FakeW(dirs=HOME_DIRS, acl=acl), ci=CI, ci_eval=CI_EVAL, app=APP
        )
    assert not [ln for ln in lines if ln.startswith("warning")]
    monkeypatch.setenv("SKILLHUB_CI_PRINCIPAL", CI)
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", CI_EVAL)
    monkeypatch.setenv("SKILLHUB_APP_PRINCIPAL", APP)
    assert exp.check_root_acl(cfg, FakeW(dirs=HOME_DIRS, acl=acl)) == []


def test_anyone_else_inheriting_access_from_the_home_folder_is_drift(monkeypatch):
    cfg = _home_cfg(monkeypatch)
    monkeypatch.setenv("SKILLHUB_CI_PRINCIPAL", CI)
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", CI_EVAL)
    acl = {
        "40": [
            entry(user="mallory@example.com", level="CAN_EDIT", inherited_from="/directories/20"),
            entry(sp=CI_EVAL, level="CAN_MANAGE", inherited_from="/directories/20"),  # more than CAN_EDIT
        ]
    }
    drifts = exp.check_root_acl(cfg, FakeW(dirs=HOME_DIRS, acl=acl))
    assert sorted((d.principal, d.kind) for d in drifts) == [
        (CI_EVAL, "inherited_access"),
        ("mallory@example.com", "inherited_access"),
    ]
    assert all(exp.fails_check(d) for d in drifts)


def test_per_experiment_checks_ignore_everything_inherited_from_the_root_and_its_parents(monkeypatch):
    cfg = _home_cfg(monkeypatch)
    monkeypatch.setattr(
        exp,
        "skill_experiments",
        lambda cfg: [NS(name=f"{HOME}/skill-hub/skill_hub/equity_research/a", experiment_id="1", tags={})],
    )
    from_home = entry(sp=CI, level="CAN_MANAGE", inherited_from="/directories/20")
    w = FakeW(dirs=HOME_DIRS, grants=[grant("g-er", "SELECT")], acl={"1": [entry(group="g-er"), from_home]})
    assert _acl_drifts(cfg, w) == []


def test_parent_folders_this_identity_cannot_see_are_skipped(monkeypatch):
    from databricks.sdk.errors import PermissionDenied

    cfg = _home_cfg(monkeypatch)
    w = FakeW(dirs=HOME_DIRS)
    status = w.workspace.get_status

    def get_status(path):
        if path == "/Users":
            raise PermissionDenied(path)
        return status(path)

    w.workspace.get_status = get_status
    assert exp._parent_ids(w, cfg) == {"/directories/40", "/directories/30", "/directories/20"}


def test_only_inherited_users_access_is_report_only():
    assert not exp.fails_check(exp.Drift(ROOT, "users", "inherited_access", "x"))
    assert exp.fails_check(exp.Drift(ROOT, "users", "unexpected", "x"))
    assert exp.fails_check(exp.Drift(ROOT, "-", "missing_root", "x"))


def test_bootstrap_resolves_the_hand_over_steps():
    """The person's first bootstrap hands the alerts and the Genie space to ci with these."""
    from skillhub.registry.bootstrap import default_results_steps
    from skillhub.results import alerts, genie

    steps = default_results_steps()
    assert (
        steps.hand_over_alerts is alerts.hand_over_alerts and steps.hand_over_genie is genie.hand_over_space
    )
