import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound
from typer.testing import CliRunner

from skillhub import cli
from skillhub.lint import LintRefused
from skillhub.registry.stage import (
    StageError,
    cleanup_stale,
    cleanup_try_schemas,
    drop_run_schema,
    drop_run_schemas,
    stage,
    stage_candidate,
)
from skillhub.registry.versions import publish

CONFIG = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"


def test_candidate_overrides_live_and_other_live_skills_are_copied(reg, live, make_bundle):
    live(make_bundle("equity_research", "earnings-preview", body="live"))
    live(make_bundle("equity_research", "morning-note", body="morning"))
    cand = make_bundle("equity_research", "earnings-preview", body="candidate")
    scopes = stage(reg.cfg, reg.uc, reg.journal, "pr1_ab", [cand])
    assert len(scopes) == 1 and scopes[0].dept == "equity_research"
    by_name = {s.name: s for s in scopes[0].skills}
    assert by_name["earnings-preview"].origin == "candidate"
    assert by_name["morning-note"].origin == "live"
    assert reg.uc.read_files(by_name["earnings-preview"].staged_fqn)["SKILL.md"] == cand.files["SKILL.md"]
    assert by_name["morning-note"].production_fqn == "skill_hub.dept_equity_research.morning-note"


def test_department_scope_includes_common_and_common_scope_stays_common(reg, live, make_bundle):
    live(make_bundle("common", "audit-xls", body="a"))
    live(make_bundle("equity_research", "morning-note", body="m"))
    cand = make_bundle("operations", "ops-thing", body="o")
    scopes = stage(reg.cfg, reg.uc, reg.journal, "pr3_ef", [cand], scope_depts=["equity_research", "common"])
    by_dept = {s.dept: sorted(x.name for x in s.skills) for s in scopes}
    assert by_dept["equity_research"] == ["audit-xls", "morning-note"]
    assert by_dept["common"] == ["audit-xls"]
    assert by_dept["operations"] == ["audit-xls", "ops-thing"]


def test_versions_are_staged_from_the_registered_copy(reg, live, make_bundle):
    live(make_bundle("equity_research", "earnings-preview", body="live v1"))
    v2 = make_bundle("equity_research", "earnings-preview", body="registered v2")
    publish(reg.cfg, reg.uc, reg.journal, v2, git_sha="a" * 40, eval_run_id="r", actor="ci", job_run_id=None)
    scopes = stage(
        reg.cfg, reg.uc, reg.journal, "pr5_vv", [], versions={"equity_research/earnings-preview": 2}
    )
    s = {x.name: x for x in scopes[0].skills}["earnings-preview"]
    assert s.origin == "version"  # a version beats the live copy
    assert reg.uc.read_files(s.staged_fqn)["SKILL.md"] == v2.files["SKILL.md"]


def test_candidate_beats_version(reg, live, make_bundle):
    live(make_bundle("equity_research", "earnings-preview", body="live v1"))
    publish(
        reg.cfg,
        reg.uc,
        reg.journal,
        make_bundle("equity_research", "earnings-preview", body="v2"),
        git_sha="a" * 40,
        eval_run_id="r",
        actor="ci",
        job_run_id=None,
    )
    cand = make_bundle("equity_research", "earnings-preview", body="candidate")
    scopes = stage(
        reg.cfg, reg.uc, reg.journal, "pr5_cv", [cand], versions={"equity_research/earnings-preview": 2}
    )
    assert {x.name: x.origin for x in scopes[0].skills}["earnings-preview"] == "candidate"


def test_version_must_be_published_before_anything_is_written(reg):
    with pytest.raises(StageError):
        stage(reg.cfg, reg.uc, reg.journal, "pr5_vx", [], versions={"equity_research/earnings-preview": 9})
    assert reg.uc.schemas == set()


def test_cli_stage_passes_the_run_key_through_make_run_key(reg, make_bundle, monkeypatch, tmp_path, cfg):
    monkeypatch.chdir(tmp_path)
    make_bundle("common", "audit-xls")
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    r = CliRunner().invoke(
        cli.app, ["--config", str(CONFIG), "stage", "--run-key", "PR12-ab12cd3", "--skills", "audit-xls"]
    )
    assert r.exit_code == 0, r.output
    assert "skill_hub.run_pr12_ab12cd3_common" in r.output


def test_stage_refuses_invalid_bundle_before_any_write(reg, make_bundle):
    bad = make_bundle("common", "Bad_Name")
    with pytest.raises(LintRefused):
        stage(reg.cfg, reg.uc, reg.journal, "pr4_gh", [bad])
    assert reg.uc.schemas == set()


def test_drop_run_schemas_names_every_scope(cfg):
    w = MagicMock()
    drop_run_schemas(cfg, w, "pr1_ab")
    calls = w.schemas.delete.call_args_list
    names = {c.args[0] for c in calls}
    assert {"skill_hub.run_pr1_ab_common", "skill_hub.run_pr1_ab_dept_equity_research"} <= names
    assert all(c.kwargs == {"force": True} for c in calls)


def test_cleanup_drops_only_old_run_schemas(cfg):
    now_ms = int(time.time() * 1000)
    old = SimpleNamespace(name="run_pr1_ab_common", created_at=now_ms - 25 * 3600 * 1000, owner="me")
    new = SimpleNamespace(name="run_pr2_cd_common", created_at=now_ms, owner="me")
    other = SimpleNamespace(name="common", created_at=0, owner="me")
    w = MagicMock()
    w.current_user.me.return_value = SimpleNamespace(user_name="me")
    w.schemas.list.return_value = [old, new, other]
    assert cleanup_stale(cfg, w, 24) == ["skill_hub.run_pr1_ab_common"]


def test_restage_with_same_run_key_deletes_with_a_fresh_etag(reg, make_bundle):
    cand = make_bundle("common", "audit-xls", body="one")
    stage(reg.cfg, reg.uc, reg.journal, "pr9_aa", [cand])
    seen = []
    real = reg.uc.delete

    def spy(fqn, etag=None):
        seen.append(etag)
        real(fqn, etag=etag)

    reg.uc.delete = spy
    stage(reg.cfg, reg.uc, reg.journal, "pr9_aa", [make_bundle("common", "audit-xls", body="two")])
    assert seen and all(e for e in seen)


def test_evals_are_never_staged(reg, live, make_bundle):
    b = make_bundle("common", "audit-xls", body="x")
    live(b)
    version_fqn_ = "skill_hub.common_versions.audit-xls-v1"
    reg.uc.upload_file(version_fqn_, "evals/cases.json", b"secret")  # a stray file in the registered copy
    scopes = stage(reg.cfg, reg.uc, reg.journal, "pr9_ev", [], scope_depts=["common"])
    staged = reg.uc.read_files(scopes[0].skills[0].staged_fqn)
    assert not any(p.startswith("evals/") for p in staged)


def test_candidate_with_evals_is_staged_without_them(reg, make_bundle):
    cand = make_bundle("common", "audit-xls", files={"evals/c.json": "{}", "ref/a.md": "a"})
    scopes = stage(reg.cfg, reg.uc, reg.journal, "pr9_ec", [cand])
    assert sorted(reg.uc.read_files(scopes[0].skills[0].staged_fqn)) == ["SKILL.md", "ref/a.md"]


def test_unsafe_rel_path_is_refused(reg, make_bundle):
    from dataclasses import replace

    from skillhub.registry.uc_skills import UnsafePath

    cand = make_bundle("common", "audit-xls")
    bad = replace(cand, files={**cand.files, "../x": b"y"})
    with pytest.raises(UnsafePath):
        stage(reg.cfg, reg.uc, reg.journal, "pr9_up", [bad])


def test_drop_refuses_non_run_schema(cfg):
    from skillhub.registry.stage import _drop_schema

    w = MagicMock()
    for bad in ("skill_hub.common", "skill_hub.dept_equity_research_versions", "other.run_a_common"):
        with pytest.raises(StageError):
            _drop_schema(cfg, w, bad)
    w.schemas.delete.assert_not_called()


def test_cleanup_never_drops_production_even_if_old(cfg):
    w = MagicMock()
    w.schemas.list.return_value = [
        SimpleNamespace(name="common", created_at=1),
        SimpleNamespace(name="dept_equity_research_versions", created_at=1),
        SimpleNamespace(name="run_x_common", created_at=None),
    ]
    assert cleanup_stale(cfg, w, 24) == []
    w.schemas.delete.assert_not_called()


ROOT = "/Workspace/Shared/skill-hub/skill_hub/candidates"  # <workspace_root>/<catalog>/candidates
ME = "ci-eval-app"


def _ago_ms(hours):
    return int(time.time() * 1000) - int(hours * 3600 * 1000)


def _dir(name, root=ROOT, otype=None):
    """A Workspace API entry. Directories carry no timestamps (the SDK fills them only for files)."""
    from databricks.sdk.service.workspace import ObjectType

    return SimpleNamespace(
        path=f"{root}/{name}", object_type=otype or ObjectType.DIRECTORY, modified_at=None, created_at=None
    )


class _Workspace:
    """Candidate folders with an optional .uploaded_at marker (Unix seconds) and dated files."""

    def __init__(self, entries, markers=None, files=None):
        self.entries, self.markers, self.files = entries, markers or {}, files or {}
        self.deleted, self.downloads = [], []

    def list(self, path, recursive=False, **kw):
        from databricks.sdk.service.workspace import ObjectType

        if path == ROOT and not recursive:
            return iter(self.entries)
        name = path.rsplit("/", 1)[1]
        return iter(
            SimpleNamespace(path=f"{path}/skills/f{i}", object_type=ObjectType.FILE, modified_at=m)
            for i, m in enumerate(self.files.get(name, []))
        )

    def download(self, path, **kw):
        import io

        from databricks.sdk.errors import NotFound

        self.downloads.append(path)
        folder = path.rsplit("/", 2)[1]
        if folder not in self.markers:
            raise NotFound(path)
        return io.BytesIO(self.markers[folder].encode())

    def delete(self, path, recursive=False):
        assert recursive
        self.deleted.append(path)


def _cleanup_w(ws, schemas=()):
    w = MagicMock()
    w.current_user.me.return_value = SimpleNamespace(user_name=ME)
    w.schemas.list.return_value = list(schemas)
    w.workspace = ws
    return w


def test_cleanup_dates_candidate_folders_by_marker_and_only_under_prefix(cfg):
    from databricks.sdk.service.workspace import ObjectType

    entries = [
        _dir("pr1_old"),
        _dir("pr2_new"),
        _dir("a_file", otype=ObjectType.FILE),
        _dir(".."),
        _dir("sneaky", root="/Workspace/Shared/skill-hub/skill_hub"),
        _dir("pr3_nested/x"),
    ]
    old, new = str(_ago_ms(169) // 1000), str(_ago_ms(1) // 1000)
    markers = {
        "pr1_old": old + "\n",
        "pr2_new": new,
        "a_file": old,
        "..": old,
        "sneaky": old,
        "pr3_nested": old,
    }
    ws = _Workspace(entries, markers)
    assert cleanup_stale(cfg, _cleanup_w(ws), 24) == [f"{ROOT}/pr1_old"]
    assert ws.deleted == [f"{ROOT}/pr1_old"]
    assert ws.downloads == [f"{ROOT}/pr1_old/.uploaded_at", f"{ROOT}/pr2_new/.uploaded_at"]


def test_a_folder_without_a_marker_is_dated_by_its_newest_file(cfg):
    ws = _Workspace(
        [_dir("old_files"), _dir("one_new_file"), _dir("undated")],
        files={"old_files": [_ago_ms(400), _ago_ms(200)], "one_new_file": [_ago_ms(400), _ago_ms(2)]},
    )
    assert cleanup_stale(cfg, _cleanup_w(ws), 24) == [f"{ROOT}/old_files"]  # an undated folder is kept


@pytest.mark.parametrize("marker", ["soon", "-5", str(int(time.time()) + 10 * 86400)])
def test_a_malformed_or_future_marker_falls_back_to_the_files(cfg, marker):
    ws = _Workspace([_dir("pr1")], markers={"pr1": marker}, files={"pr1": [_ago_ms(200)]})
    assert cleanup_stale(cfg, _cleanup_w(ws), 24) == [f"{ROOT}/pr1"]
    ws = _Workspace([_dir("pr1")], markers={"pr1": marker}, files={"pr1": [_ago_ms(1)]})
    assert cleanup_stale(cfg, _cleanup_w(ws), 24) == []


def test_cleanup_drops_only_run_schemas_this_principal_owns(cfg):
    w = _cleanup_w(
        _Workspace([]),
        [
            SimpleNamespace(name="run_a_common", created_at=1, owner=ME),
            SimpleNamespace(name="run_b_common", created_at=1, owner="someone-else"),
            SimpleNamespace(name="run_c_common\n", created_at=1, owner=ME),
        ],
    )
    assert cleanup_stale(cfg, w, 24) == ["skill_hub.run_a_common"]
    w.schemas.delete.assert_called_once_with("skill_hub.run_a_common", force=True)


def test_a_refused_drop_does_not_stop_the_cleanup(cfg):
    from databricks.sdk.errors import PermissionDenied

    w = _cleanup_w(
        _Workspace([_dir("pr1")], markers={"pr1": str(_ago_ms(200) // 1000)}),
        [SimpleNamespace(name=f"run_{k}_common", created_at=1, owner=ME) for k in "ab"],
    )
    w.schemas.delete.side_effect = [PermissionDenied("no"), None]
    assert cleanup_stale(cfg, w, 24) == ["skill_hub.run_b_common", f"{ROOT}/pr1"]


def test_cleanup_without_candidates_folder_is_fine(cfg):
    from databricks.sdk.errors import NotFound

    w = MagicMock()
    w.schemas.list.return_value = []
    w.workspace.list.side_effect = NotFound("none")
    assert cleanup_stale(cfg, w, 24) == []


def test_cli_stage_and_cleanup_accept_empty_strings(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    make_bundle("common", "audit-xls")
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    r = CliRunner().invoke(
        cli.app,
        ["--config", str(CONFIG), "stage", "--run-key", "pr1-ab", "--skills", "audit-xls", "--versions", ""],
    )
    assert r.exit_code == 0, r.output
    w = MagicMock()
    w.schemas.list.return_value = []
    w.workspace.list.return_value = []
    monkeypatch.setattr(cli, "workspace_client", lambda: w)
    r = CliRunner().invoke(cli.app, ["--config", str(CONFIG), "cleanup", "--older-than-hours", ""])
    assert r.exit_code == 0, r.output


def test_cli_stage_versions_option(reg, live, make_bundle, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    live(make_bundle("common", "audit-xls", body="v1"))
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    r = CliRunner().invoke(
        cli.app,
        [
            "--config",
            str(CONFIG),
            "stage",
            "--run-key",
            "pr1-ab",
            "--skills",
            "",
            "--versions",
            "audit-xls=1",
        ],
    )
    assert r.exit_code == 0, r.output
    assert '"origin": "version"' in r.output
    r = CliRunner().invoke(
        cli.app, ["--config", str(CONFIG), "stage", "--run-key", "pr1-ab", "--skills", "", "--versions", ""]
    )
    assert r.exit_code == 2
    for bad in ("audit-xls", "audit-xls=0", "audit-xls=x", "=3"):
        r = CliRunner().invoke(
            cli.app, ["--config", str(CONFIG), "stage", "--run-key", "pr1-ab", "--versions", bad]
        )
        assert r.exit_code == 2, bad


def test_cli_cleanup_rejects_non_numeric_hours(monkeypatch):
    monkeypatch.setattr(cli, "workspace_client", lambda: MagicMock())
    for bad in ("abc", "0", "-3", "1.5"):
        r = CliRunner().invoke(cli.app, ["--config", str(CONFIG), "cleanup", "--older-than-hours", bad])
        assert r.exit_code == 2, bad


def test_cleanup_continues_when_one_drop_races(cfg):
    from databricks.sdk.errors import NotFound

    old = str(_ago_ms(200) // 1000)
    ws = _Workspace([_dir("pr1_old"), _dir("pr2_old")], markers={"pr1_old": old, "pr2_old": old})
    gone = iter([True, False])

    def delete(path, recursive=False):
        if next(gone):
            raise NotFound("gone")
        ws.deleted.append(path)

    ws.delete = delete
    w = _cleanup_w(ws, [SimpleNamespace(name=f"run_{k}_common", created_at=1, owner=ME) for k in "ab"])
    w.schemas.delete.side_effect = [NotFound("gone"), None]
    out = cleanup_stale(cfg, w, 24)
    assert out == ["skill_hub.run_b_common", f"{ROOT}/pr2_old"]


def test_stage_rejects_unknown_scope_dept(reg):
    with pytest.raises(StageError, match="unknown department"):
        stage(reg.cfg, reg.uc, reg.journal, "pr9_sd", [], scope_depts=["nope"])


# --- pins resolve by dept/name only ---


def _pub(reg, bundle):
    publish(
        reg.cfg, reg.uc, reg.journal, bundle, git_sha="a" * 40, eval_run_id="r", actor="ci", job_run_id=None
    )


def test_a_pin_never_stages_another_departments_version(reg, live, make_bundle):
    """foo moved from operations (v1..v3) to equity_research (v1, live). equity_research/foo=2 does not
    exist, so staging refuses instead of evaluating operations' v2 under equity_research's name."""
    for body in ("o1", "o2", "o3"):
        _pub(reg, make_bundle("operations", "foo", body=body))
    live(make_bundle("equity_research", "foo", body="e1"))
    with pytest.raises(
        StageError, match="equity_research/foo version 2 is not a published version of equity_research"
    ):
        stage(reg.cfg, reg.uc, reg.journal, "pr7_mv", [], versions={"equity_research/foo": 2})
    assert reg.uc.schemas == set()


def test_a_pin_stages_its_own_departments_copy_when_both_have_the_number(reg, make_bundle):
    _pub(reg, make_bundle("operations", "foo", body="o1"))
    _pub(reg, ops2 := make_bundle("operations", "foo", body="o2"))
    _pub(reg, make_bundle("equity_research", "foo", body="e1"))
    _pub(reg, make_bundle("equity_research", "foo", body="e2"))
    scopes = stage(reg.cfg, reg.uc, reg.journal, "pr7_bo", [], versions={("operations", "foo"): 2})
    (scope,) = scopes
    (staged,) = scope.skills
    assert (scope.dept, staged.dept, staged.origin) == ("operations", "operations", "version")
    assert reg.uc.read_files(staged.staged_fqn)["SKILL.md"] == ops2.files["SKILL.md"]


@pytest.mark.parametrize(
    ("pin", "match"),
    [
        ("foo", "must name its department"),
        ("nope/foo", "unknown department"),
        ("common/Bad/x", "invalid skill name"),
    ],
)
def test_bad_pins_are_refused_before_anything_is_written(reg, pin, match):
    with pytest.raises(StageError, match=match):
        stage(reg.cfg, reg.uc, reg.journal, "pr7_bp", [], versions={pin: 1})
    assert reg.uc.schemas == set()


def test_a_candidate_from_an_unknown_department_is_refused(reg, make_bundle):
    with pytest.raises(StageError, match="unknown department"):
        stage(reg.cfg, reg.uc, reg.journal, "pr7_ud", [make_bundle("rogue", "foo")])
    assert reg.uc.schemas == set()


def test_cli_stage_resolves_a_bare_pin_through_the_checkout_or_asks_for_dept(
    reg, live, make_bundle, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    live(make_bundle("common", "audit-xls", body="v1"))
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))

    def run(versions):
        return CliRunner().invoke(
            cli.app, ["--config", str(CONFIG), "stage", "--run-key", "pr1-ab", "--versions", versions]
        )

    assert run("common/audit-xls=1").exit_code == 0
    r = run("absent-skill=1")  # no folder names its department
    assert r.exit_code == 2 and "use dept/name" in r.output
    r = run("rogue/audit-xls=1")
    assert r.exit_code == 2 and "unknown department" in r.output


def test_cleanup_reads_only_this_catalogs_candidates_under_the_workspace_root(monkeypatch):
    """Candidates live in <workspace_root>/<catalog>/candidates, so dev and prod never delete each other's."""
    from tests.fakes import make_cfg

    monkeypatch.setenv("SKILLHUB_WORKSPACE_ROOT", "/Users/ci-app/skill-hub")
    cfg = make_cfg()
    listed = []

    class Ws(_Workspace):
        def list(self, path, recursive=False, **kw):
            listed.append(path)
            return iter([])

    cleanup_stale(cfg, _cleanup_w(Ws([])), 24)
    assert listed == [
        "/Workspace/Users/ci-app/skill-hub/skill_hub/candidates",
        "/Workspace/Users/ci-app/skill-hub/skill_hub/drafts",
    ]


DRAFTS = "/Workspace/Shared/skill-hub/skill_hub/drafts"  # <workspace_root>/<catalog>/drafts


class _TwoRoots:
    """The candidates and drafts folders, each with dated child folders (marker Unix seconds, or None)."""

    def __init__(self, folders):
        self.folders = folders  # root -> {name: marker or None}
        self.deleted = []

    def list(self, path, recursive=False, **kw):
        from databricks.sdk.errors import NotFound

        root, _, name = path.rpartition("/")
        if name in self.folders.get(root, {}):
            return iter([])  # a draft or candidate folder with no dated files
        if path not in self.folders:
            raise NotFound(path)
        return iter(_dir(name, root=path) for name in self.folders[path])

    def download(self, path, **kw):
        import io

        from databricks.sdk.errors import NotFound

        folder, _, _ = path.rpartition("/")
        root, _, name = folder.rpartition("/")
        marker = self.folders.get(root, {}).get(name)
        if marker is None:
            raise NotFound(path)
        return io.BytesIO(marker.encode())

    def delete(self, path, recursive=False):
        assert recursive
        self.deleted.append(path)


def test_cleanup_removes_stale_drafts_by_the_same_age_rule(cfg):
    old, new = str(_ago_ms(169) // 1000), str(_ago_ms(1) // 1000)
    ws = _TwoRoots(
        {
            ROOT: {"pr1_old": old, "pr2_new": new},
            DRAFTS: {"draft_old": old, "draft_new": new, "undated": None},
        }
    )
    removed = cleanup_stale(cfg, _cleanup_w(ws), 24)
    assert removed == [f"{ROOT}/pr1_old", f"{DRAFTS}/draft_old"]
    assert ws.deleted == removed


def test_cleanup_without_a_drafts_folder_still_cleans_the_candidates(cfg):
    ws = _TwoRoots({ROOT: {"pr1_old": str(_ago_ms(200) // 1000)}})
    assert cleanup_stale(cfg, _cleanup_w(ws), 24) == [f"{ROOT}/pr1_old"]


# --- Try: a candidate staged into its own run schema ---------------------------------------------------------------

TRY_SCHEMA = "skill_hub.run_try_0123456789_common"


def test_stage_candidate_stages_the_upload_without_its_evals(reg):
    reg.uc.ensure_schema(TRY_SCHEMA)
    files = {"SKILL.md": b"---\nname: new-skill\ndescription: d\n---\nbody\n", "evals/evals.json": b"{}"}
    staged = stage_candidate(reg.cfg, reg.uc, TRY_SCHEMA, "new-skill", "common", files)
    assert (staged.staged_fqn, staged.production_fqn, staged.origin) == (
        f"{TRY_SCHEMA}.new-skill",
        "skill_hub.common.new-skill",
        "candidate",
    )
    assert sorted(reg.uc.read_files(staged.staged_fqn)) == ["SKILL.md"]


def test_stage_candidate_checks_before_each_upload_and_stops_when_the_check_raises(reg):
    reg.uc.ensure_schema(TRY_SCHEMA)
    files = {"SKILL.md": b"---\nname: new-skill\ndescription: d\n---\nbody\n", "a.md": b"a", "b.md": b"b"}
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:
            raise TimeoutError("the request timed out")

    with pytest.raises(TimeoutError):
        stage_candidate(reg.cfg, reg.uc, TRY_SCHEMA, "new-skill", "common", files, check=check)
    assert len(reg.uc.read_files(f"{TRY_SCHEMA}.new-skill")) == 1  # one upload, then the check stopped it


def test_stage_candidate_refuses_a_schema_that_is_not_a_run_schema(reg):
    files = {"SKILL.md": b"x"}
    with pytest.raises(StageError):
        stage_candidate(reg.cfg, reg.uc, "skill_hub.common", "new-skill", "common", files)
    with pytest.raises(StageError):
        stage_candidate(
            reg.cfg, reg.uc, "other_catalog.run_try_0123456789_common", "new-skill", "common", files
        )
    assert reg.uc.schemas == set()


def test_drop_run_schema_drops_a_run_schema_and_refuses_anything_else(cfg):
    w = MagicMock()
    drop_run_schema(cfg, w, TRY_SCHEMA)
    w.schemas.delete.assert_called_once_with(TRY_SCHEMA, force=True)
    with pytest.raises(StageError):
        drop_run_schema(cfg, w, "skill_hub.common")
    with pytest.raises(StageError):
        drop_run_schema(cfg, w, "skill_hub.dept_equity_research")
    assert w.schemas.delete.call_count == 1


def test_cleanup_try_schemas_drops_only_own_try_schemas_older_than_an_hour(cfg):
    w = _cleanup_w(
        _Workspace([]),
        [
            SimpleNamespace(name="run_try_aaaaaaaaaa_common", created_at=_ago_ms(2), owner=ME),
            SimpleNamespace(name="run_try_bbbbbbbbbb_common", created_at=_ago_ms(0.5), owner=ME),  # too young
            SimpleNamespace(name="run_try_cccccccccc_common", created_at=_ago_ms(2), owner="someone-else"),
            SimpleNamespace(name="run_pr1_ab_common", created_at=_ago_ms(2), owner=ME),  # not a try run
            SimpleNamespace(name="run_try_dddddddddd_common\n", created_at=_ago_ms(2), owner=ME),
            SimpleNamespace(name="common", created_at=_ago_ms(2), owner=ME),
            SimpleNamespace(name="run_try_eeeeeeeeee_common", created_at=None, owner=ME),  # no age: kept
        ],
    )
    assert cleanup_try_schemas(cfg, w) == ["skill_hub.run_try_aaaaaaaaaa_common"]
    w.schemas.delete.assert_called_once_with("skill_hub.run_try_aaaaaaaaaa_common", force=True)


def test_cleanup_try_schemas_skips_a_schema_that_is_gone_or_refused(cfg):
    from databricks.sdk.errors import PermissionDenied

    w = _cleanup_w(
        _Workspace([]),
        [SimpleNamespace(name=f"run_try_{k}_common", created_at=_ago_ms(2), owner=ME) for k in "abc"],
    )
    w.schemas.delete.side_effect = [NotFound("gone"), PermissionDenied("no"), None]
    assert cleanup_try_schemas(cfg, w) == ["skill_hub.run_try_c_common"]
