import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillhub import cli
from skillhub.naming import live_fqn, live_schema
from skillhub.registry.journal import new_row
from skillhub.registry.reconcile import reconcile_skill
from skillhub.registry.switch import DriftError, SwitchError, retire, switch
from skillhub.registry.versions import publish
from tests.unit.eval_rows import DATASET, INPUT_ARGS, RUBRIC, ensure_eval_runs, evaluated, trust_ci_eval

SHA = "a" * 40
DEPT, SKILL = "equity_research", "earnings-preview"
CONFIG = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"


CI = "ci-app"
JOB_RUN = "77"
_REAL_JOB_RUN_CREATOR = cli._job_run_creator  # the fixture below replaces it


@pytest.fixture(autouse=True)
def _gate_readers(reg, monkeypatch):
    """The publish gate runs the merge gate's real readers over FakeSql eval_runs tables. Publishes run in a
    skill-switch Job run that ci started, unless a test says otherwise."""
    ensure_eval_runs(reg.sql, reg.cfg)
    trust_ci_eval(monkeypatch)
    monkeypatch.setenv("SKILLHUB_CI_PRINCIPAL", CI)
    monkeypatch.setenv("SKILLHUB_JOB_RUN_ID", JOB_RUN)
    monkeypatch.setattr(cli, "_job_run_creator", lambda w, run_id: CI)


def _live(reg):
    return live_fqn(reg.cfg, DEPT, SKILL)


def _publish(reg, bundle):
    return publish(
        reg.cfg, reg.uc, reg.journal, bundle, git_sha=SHA, eval_run_id="r", actor="ci", job_run_id=None
    ).version


def _switch(reg, to):
    return switch(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, to, actor="ci", job_run_id="job-1")


def test_first_switch_creates_the_live_skill_and_journals_it(reg, live, make_bundle):
    b1 = make_bundle(DEPT, SKILL, body="one")
    assert live(b1) == 1
    assert reg.uc.read_files(_live(reg))["SKILL.md"] == b1.files["SKILL.md"]
    state = reg.journal.live_state(DEPT, SKILL)
    assert (state.action, state.to_version) == ("switched", 1)


def test_switch_replaces_changed_files_and_removes_extras(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one", files={"references/a.md": "A", "references/old.md": "O"}))
    b2 = make_bundle(DEPT, SKILL, body="two", files={"references/a.md": "A2"})
    _publish(reg, b2)
    res = _switch(reg, 2)
    assert res.status == "switched" and res.from_version == 1
    assert reg.uc.read_files(_live(reg)) == b2.files


def test_rerunning_the_same_switch_is_a_noop(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    before = len(reg.journal.history(DEPT, SKILL))
    assert _switch(reg, 1).status == "noop"
    assert len(reg.journal.history(DEPT, SKILL)) == before


def test_rerun_after_crash_mid_switch_converges(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    b2 = make_bundle(DEPT, SKILL, body="two")
    _publish(reg, b2)
    reg.uc.fail_once("finalize")  # dies after SKILL.md is already live
    with pytest.raises(RuntimeError):
        _switch(reg, 2)
    res = _switch(reg, 2)
    assert res.status == "switched" and res.from_version == 1
    assert reg.uc.read_files(_live(reg))["SKILL.md"] == b2.files["SKILL.md"]
    actions = [h.action for h in reg.journal.history(DEPT, SKILL)]
    assert actions[-3:] == ["switch_started", "switch_started", "switched"]


def test_transient_read_back_mismatch_is_retried(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    _publish(reg, make_bundle(DEPT, SKILL, body="two"))
    reg.uc.tamper_after_finalize(_live(reg), 3)  # attempts 1 to 3 read back stale; attempt 4 is clean
    assert _switch(reg, 2).status == "switched"
    assert "switch_failed" not in [h.action for h in reg.journal.history(DEPT, SKILL)]


def test_persistent_read_back_mismatch_journals_switch_failed(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    _publish(reg, make_bundle(DEPT, SKILL, body="two"))
    reg.uc.tamper_after_finalize(_live(reg), 4)
    res = _switch(reg, 2)
    assert res.status == "switch_failed"
    assert reg.journal.history(DEPT, SKILL)[-1].action == "switch_failed"
    assert reg.journal.live_state(DEPT, SKILL).to_version == 1  # the last good switch still stands


def test_publishing_a_new_version_is_not_drift(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    _publish(reg, make_bundle(DEPT, SKILL, body="two"))  # publishing alone does not go live
    assert _switch(reg, 2).status == "switched"


def test_external_edit_is_drift_and_blocks_switch(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    _publish(reg, make_bundle(DEPT, SKILL, body="two"))
    reg.uc.external_edit(_live(reg), "SKILL.md", b"hand edit")
    res = _switch(reg, 2)
    assert res.status == "drift"
    assert reg.journal.history(DEPT, SKILL)[-1].action == "drift"
    assert reg.uc.read_files(_live(reg))["SKILL.md"] == b"hand edit"  # untouched


def test_switch_to_an_unpublished_version_is_refused(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    with pytest.raises(SwitchError):
        _switch(reg, 9)


def test_retire_removes_the_live_skill_and_keeps_versions(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None)
    assert reg.uc.get(_live(reg)) is None
    assert reg.uc.get(f"skill_hub.dept_equity_research_versions.{SKILL}-v1") is not None
    assert reg.journal.live_state(DEPT, SKILL).action == "retired"


def test_retire_refuses_when_drifted(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    reg.uc.external_edit(_live(reg), "SKILL.md", b"hand edit")
    with pytest.raises(DriftError):
        retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None)


def test_cli_switch_reports_drift_with_exit_1(reg, live, make_bundle, monkeypatch, cfg):
    live(make_bundle(DEPT, SKILL, body="one"))
    reg.uc.external_edit(_live(reg), "SKILL.md", b"hand edit")
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    r = CliRunner().invoke(
        cli.app,
        ["--config", str(CONFIG), "switch", "--dept", DEPT, "--skill", SKILL, "--to", "1", "--actor", "ci"],
    )
    assert r.exit_code == 1
    assert '"drift"' in r.output


def test_job_switch_dispatches_publish_switch_and_retire(reg, make_bundle, monkeypatch, tmp_path, cfg):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL, body="one"), run_key="run_1")
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    base = ["--config", str(CONFIG), "job-switch", "--dept", DEPT, "--skill", SKILL, "--actor", "ci"]
    base += ["--job-run-id", "9"]
    r = CliRunner().invoke(
        cli.app, base + ["--action", "publish", "--git-sha", SHA, "--eval-run-id", "run-1", *INPUT_ARGS]
    )
    assert r.exit_code == 0, r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 1
    r = CliRunner().invoke(cli.app, base + ["--action", "switch", "--version", "1"])
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).job_run_id == "9"
    r = CliRunner().invoke(cli.app, base + ["--action", "retire"])
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).action == "retired"


def _cli(reg, monkeypatch, *args):
    """job-switch as the Job runs it. A publish gets the merge plan's eval inputs unless the test gives them."""
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    if "publish" in args and "--expect-rubric-hash" not in args:
        args = (*args, *INPUT_ARGS)
    elif "publish" in args and "--expect-cells" not in args:
        args = (*args, *INPUT_ARGS[-2:])
    return CliRunner().invoke(
        cli.app,
        ["--config", str(CONFIG), "job-switch", "--dept", DEPT, "--skill", SKILL, "--actor", "ci", *args],
    )


def test_job_switch_accepts_empty_strings_for_unset_params(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL, body="one"))
    r = _cli(
        reg,
        monkeypatch,
        "--action",
        "publish",
        "--git-sha",
        SHA,
        "--eval-run-id",
        "r",
        "--version",
        "",
    )
    assert r.exit_code == 0, r.output
    r = _cli(
        reg,
        monkeypatch,
        "--action",
        "switch",
        "--version",
        "1",
        "--git-sha",
        "",
        "--eval-run-id",
        "",
        "--job-run-id",
        "",
    )
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).job_run_id is None
    r = _cli(
        reg,
        monkeypatch,
        "--action",
        "retire",
        "--version",
        "",
        "--git-sha",
        "",
        "--eval-run-id",
        "",
        "--job-run-id",
        "",
    )
    assert r.exit_code == 0, r.output


def test_job_switch_publish_without_provenance_fails(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    make_bundle(DEPT, SKILL, body="one")
    r = _cli(reg, monkeypatch, "--action", "publish", "--git-sha", "", "--eval-run-id", "")
    assert r.exit_code == 1
    assert reg.journal.latest_version(DEPT, SKILL) == 0


def test_job_switch_switch_needs_a_version(reg, monkeypatch):
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "")
    assert r.exit_code == 2


def test_job_switch_latest_matches_the_bundle_by_hash(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL, body="one"), run_key="r1")
    assert (
        _cli(reg, monkeypatch, "--action", "publish", "--git-sha", SHA, "--eval-run-id", "r1").exit_code == 0
    )
    evaluated(reg, make_bundle(DEPT, SKILL, body="two"), run_key="r2")
    assert (
        _cli(reg, monkeypatch, "--action", "publish", "--git-sha", SHA, "--eval-run-id", "r2").exit_code == 0
    )
    evaluated(reg, make_bundle(DEPT, SKILL, body="one"), run_key="r3")  # revert to v1's content
    r = _cli(reg, monkeypatch, "--action", "publish", "--git-sha", SHA, "--eval-run-id", "r3")
    assert r.exit_code == 0 and reg.journal.latest_version(DEPT, SKILL) == 2
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "latest")
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).to_version == 1


def test_job_switch_latest_falls_back_when_the_bundle_is_absent(
    reg, live, make_bundle, monkeypatch, tmp_path
):
    live(make_bundle(DEPT, SKILL, body="one"))
    _publish(reg, make_bundle(DEPT, SKILL, body="two"))
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "nowhere"))
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "latest")
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).to_version == 2


def test_switch_recreates_a_live_skill_deleted_by_hand(reg, live, make_bundle):
    b1 = make_bundle(DEPT, SKILL, body="one")
    live(b1)
    reg.uc.delete(_live(reg))
    res = _switch(reg, 1)
    assert res.status == "switched"
    assert reg.uc.read_files(_live(reg)) == b1.files


def test_retire_treats_a_missing_live_skill_as_already_gone(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    reg.uc.delete(_live(reg))
    retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None)
    assert reg.journal.live_state(DEPT, SKILL).action == "retired"


def test_rerun_converges_after_a_later_published_row(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one"))
    _publish(reg, make_bundle(DEPT, SKILL, body="two"))
    reg.uc.fail_once("upload_file")  # dies before any file changes
    with pytest.raises(RuntimeError):
        _switch(reg, 2)
    _publish(reg, make_bundle(DEPT, SKILL, body="three"))
    assert _switch(reg, 1).status == "switched"
    assert reconcile_skill(reg.cfg, reg.uc, reg.journal, DEPT, SKILL) == []


def _mid_switch(reg, live, make_bundle):
    """v1 is live, v2 is published, and a switch to v2 died after journaling its start."""
    live(make_bundle(DEPT, SKILL, body="one", files={"references/a.md": "A", "references/b.md": "B"}))
    b2 = make_bundle(DEPT, SKILL, body="two", files={"references/a.md": "A2", "references/b.md": "B2"})
    v2 = _publish(reg, b2)
    row = [h for h in reg.journal.history(DEPT, SKILL) if h.action == "published" and h.version == v2][0]
    reg.journal.append(
        new_row(DEPT, SKILL, "switch_started", actor="ci", to_version=2, versioned_fqn=row.versioned_fqn)
    )
    return b2


def _kinds(reg):
    return [i.kind for i in reconcile_skill(reg.cfg, reg.uc, reg.journal, DEPT, SKILL)]


def test_mixed_from_and_to_files_are_only_an_unfinished_switch(reg, live, make_bundle):
    b2 = _mid_switch(reg, live, make_bundle)
    reg.uc.upload_file(_live(reg), "references/a.md", b2.files["references/a.md"])
    assert _kinds(reg) == ["unfinished_switch"]
    assert _switch(reg, 2).status == "switched"
    assert reg.uc.read_files(_live(reg)) == b2.files


def test_hand_edit_during_an_unfinished_switch_is_drift(reg, live, make_bundle):
    _mid_switch(reg, live, make_bundle)
    reg.uc.external_edit(_live(reg), "references/a.md", b"hand edit")
    assert _kinds(reg) == ["unfinished_switch", "hash_drift"]
    assert _switch(reg, 2).status == "drift"
    assert reg.uc.read_files(_live(reg))["references/a.md"] == b"hand edit"


def test_switch_after_retire_recreates_the_live_skill(reg, live, make_bundle):
    b1 = make_bundle(DEPT, SKILL, body="one")
    live(b1)
    retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None)
    res = _switch(reg, 1)
    assert res.status == "switched" and res.from_version is None
    assert reg.uc.read_files(_live(reg)) == b1.files


def test_retire_with_nothing_live_reports_it(reg, make_bundle):
    _publish(reg, make_bundle(DEPT, SKILL, body="one"))
    before = len(reg.journal.history(DEPT, SKILL))
    status = retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None)
    assert status == "nothing_to_retire"
    assert len(reg.journal.history(DEPT, SKILL)) == before


def test_retire_closes_an_unfinished_switch_that_never_created_the_skill(reg, make_bundle):
    _publish(reg, make_bundle(DEPT, SKILL, body="one"))
    reg.journal.append(new_row(DEPT, SKILL, "switch_started", actor="ci", to_version=1))
    assert retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None) == "retired"
    assert reg.journal.history(DEPT, SKILL)[-1].action == "retired"


def test_cli_retire_prints_nothing_to_retire(reg, make_bundle, monkeypatch):
    _publish(reg, make_bundle(DEPT, SKILL, body="one"))
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    r = CliRunner().invoke(
        cli.app, ["--config", str(CONFIG), "retire", "--dept", DEPT, "--skill", SKILL, "--actor", "ci"]
    )
    assert r.exit_code == 0 and "nothing to retire" in r.output


def test_evals_are_not_copied_to_the_live_skill(reg, make_bundle):
    b1 = make_bundle(DEPT, SKILL, body="one")
    v1 = _publish(reg, b1)
    reg.uc.upload_file(f"skill_hub.dept_equity_research_versions.{SKILL}-v{v1}", "evals/evals.json", b"{}")
    assert _switch(reg, v1).status == "switched"
    assert "evals/evals.json" not in reg.uc.read_files(_live(reg))


def test_job_switch_latest_refuses_unpublished_content(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    for body in ("one", "two"):
        evaluated(reg, make_bundle(DEPT, SKILL, body=body), run_key=body)
        assert (
            _cli(reg, monkeypatch, "--action", "publish", "--git-sha", SHA, "--eval-run-id", body).exit_code
            == 0
        )
    make_bundle(DEPT, SKILL, body="three")  # never published
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "latest")
    assert r.exit_code == 2
    assert "is not published" in r.output
    assert reg.journal.live_state(DEPT, SKILL) is None


@pytest.mark.parametrize("bad", ["0", "abc", "-1"])
def test_job_switch_rejects_a_bad_version(reg, monkeypatch, bad):
    assert _cli(reg, monkeypatch, "--action", "switch", "--version", bad).exit_code == 2


def test_job_switch_rejects_an_invalid_action(reg, monkeypatch):
    assert _cli(reg, monkeypatch, "--action", "explode").exit_code == 2


def test_job_switch_exits_1_for_an_unpublished_version_number(reg, monkeypatch):
    assert _cli(reg, monkeypatch, "--action", "switch", "--version", "9").exit_code == 1


def test_job_switch_publish_reports_status(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL, body="one"))
    args = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "r")
    assert '"published"' in _cli(reg, monkeypatch, *args).output
    assert '"unchanged"' in _cli(reg, monkeypatch, *args).output


def _row_of(reg, version):
    return [h for h in reg.journal.history(DEPT, SKILL) if h.action == "published" and h.version == version][
        0
    ]


def test_double_interruption_rollback_converges(reg, live, make_bundle):
    files = {"references/a.md": "A", "references/b.md": "B"}
    live(make_bundle(DEPT, SKILL, body="one", files=files))
    _publish(
        reg, make_bundle(DEPT, SKILL, body="two", files={"references/a.md": "A2", "references/b.md": "B2"})
    )
    reg.uc.fail_once("finalize")  # v2 is fully uploaded, then the switch dies
    with pytest.raises(RuntimeError):
        _switch(reg, 2)
    calls = {"n": 0}
    real = reg.uc.upload_file

    def flaky(fqn, rel, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected")
        real(fqn, rel, data)

    reg.uc.upload_file = flaky
    with pytest.raises(RuntimeError):  # the rollback to v1 dies on its second upload
        _switch(reg, 1)
    reg.uc.upload_file = real
    assert _switch(reg, 1).status == "switched"
    assert reconcile_skill(reg.cfg, reg.uc, reg.journal, DEPT, SKILL) == []


def test_double_interruption_forward_converges(reg, make_bundle):
    v1 = _publish(
        reg, make_bundle(DEPT, SKILL, body="one", files={"references/a.md": "A", "references/b.md": "B"})
    )
    _publish(
        reg, make_bundle(DEPT, SKILL, body="two", files={"references/a.md": "A2", "references/b.md": "B2"})
    )
    reg.uc.fail_once("finalize")
    with pytest.raises(RuntimeError):
        _switch(reg, v1)
    calls = {"n": 0}
    real = reg.uc.upload_file

    def flaky(fqn, rel, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected")
        real(fqn, rel, data)

    reg.uc.upload_file = flaky
    with pytest.raises(RuntimeError):
        _switch(reg, 2)
    reg.uc.upload_file = real
    assert _switch(reg, 2).status == "switched"
    assert reconcile_skill(reg.cfg, reg.uc, reg.journal, DEPT, SKILL) == []


def test_old_version_is_not_trusted_after_a_retire(reg, live, make_bundle):
    live(make_bundle(DEPT, SKILL, body="one", files={"references/a.md": "A"}))
    v2 = _publish(reg, make_bundle(DEPT, SKILL, body="two", files={"references/b.md": "B"}))
    retire(reg.cfg, reg.uc, reg.journal, DEPT, SKILL, actor="ci", job_run_id=None)
    reg.uc.create(live_schema(reg.cfg, DEPT), SKILL)
    reg.uc.upload_file(_live(reg), "references/a.md", b"A")  # a stray v1 file
    reg.journal.append(
        new_row(
            DEPT,
            SKILL,
            "switch_started",
            actor="ci",
            to_version=v2,
            versioned_fqn=_row_of(reg, v2).versioned_fqn,
        )
    )
    assert _kinds(reg) == ["unfinished_switch", "hash_drift"]


def _hash_of(bundle):
    from skillhub.bundle import content_hash

    return content_hash(bundle.files)


def _writes(reg):
    """Everything a refused command must leave alone: UC skills, journal rows, non-SELECT statements."""
    non_select = [c for c in reg.sql.calls if not c[0].lstrip().upper().startswith("SELECT")]
    return (sorted(reg.uc._skills), len(reg.journal.history(DEPT, SKILL)), len(non_select))


def test_job_switch_skills_root_feeds_publish_and_latest(reg, make_bundle, monkeypatch, tmp_path):
    cand = evaluated(reg, make_bundle(DEPT, SKILL, body="candidate"))
    other = tmp_path / "candidate"
    shutil.move(tmp_path / "skills", other)  # the evaluated tree lives elsewhere
    make_bundle(DEPT, SKILL, body="env")  # $SKILLHUB_SKILLS holds different content
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    pub = (
        "--action",
        "publish",
        "--git-sha",
        SHA,
        "--eval-run-id",
        "r",
        "--skills-root",
        str(other),
        "--expect-hash",
        _hash_of(cand),
    )
    r = _cli(reg, monkeypatch, *pub)
    assert r.exit_code == 0, r.output
    assert _row_of(reg, 1).content_hash == _hash_of(cand)  # P was read, not $SKILLHUB_SKILLS
    r = _cli(
        reg,
        monkeypatch,
        "--action",
        "switch",
        "--version",
        "latest",
        "--skills-root",
        str(other),
        "--expect-hash",
        _hash_of(cand),
    )
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).to_version == 1
    # without the override, 'latest' hashes the env tree's content, which is not published
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "latest")
    assert r.exit_code == 2 and "is not published" in r.output
    assert os.environ["SKILLHUB_SKILLS"] == str(tmp_path / "skills")  # restored


def test_job_switch_skills_root_missing_directory_exits_2(reg, monkeypatch, tmp_path):
    r = _cli(reg, monkeypatch, "--action", "retire", "--skills-root", str(tmp_path / "nope"))
    assert r.exit_code == 2
    assert reg.journal.history(DEPT, SKILL) == []


def test_job_switch_empty_skills_root_and_expect_hash_mean_unset(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL, body="one"))
    args = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "r")
    assert _cli(reg, monkeypatch, *args, "--skills-root", "", "--expect-hash", "").exit_code == 0


def test_job_switch_publish_expect_hash_match_and_mismatch(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    bundle = evaluated(reg, make_bundle(DEPT, SKILL, body="one"))
    args = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "r")
    before = _writes(reg)
    r = _cli(reg, monkeypatch, *args, "--expect-hash", "sha256:" + "0" * 64)
    assert r.exit_code == 1, r.output
    assert r.output.startswith("error: refusing to publish") and len(r.output.strip().splitlines()) == 1
    assert _writes(reg) == before and before[1] == 0
    r = _cli(reg, monkeypatch, *args, "--expect-hash", _hash_of(bundle))
    assert r.exit_code == 0, r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 1


def test_job_switch_switch_expect_hash_match_and_mismatch(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    b1 = make_bundle(DEPT, SKILL, body="one")
    _publish(reg, b1)
    b2 = make_bundle(DEPT, SKILL, body="two")
    _publish(reg, b2)
    bad = "sha256:" + "0" * 64
    before = _writes(reg)
    for version in ("1", "latest"):
        r = _cli(reg, monkeypatch, "--action", "switch", "--version", version, "--expect-hash", bad)
        assert r.exit_code == 1, r.output
        assert r.output.startswith("error: refusing to switch") and len(r.output.strip().splitlines()) == 1
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "2", "--expect-hash", _hash_of(b1))
    assert r.exit_code == 1  # hash of v1 does not match v2
    assert _writes(reg) == before
    assert reg.journal.live_state(DEPT, SKILL) is None
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "1", "--expect-hash", _hash_of(b1))
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).to_version == 1
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "latest", "--expect-hash", _hash_of(b2))
    assert r.exit_code == 0, r.output
    assert reg.journal.live_state(DEPT, SKILL).to_version == 2


def test_job_switch_expect_hash_refuses_an_unpublished_version(reg, monkeypatch):
    before = _writes(reg)
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "9", "--expect-hash", "sha256:" + "a" * 64)
    assert r.exit_code == 1 and _writes(reg) == before


GOOD_HASH = "sha256:" + "a" * 64


def test_job_switch_skills_root_requires_expect_hash(reg, make_bundle, monkeypatch, tmp_path):
    make_bundle(DEPT, SKILL)
    r = _cli(reg, monkeypatch, "--action", "retire", "--skills-root", str(tmp_path / "skills"))
    assert r.exit_code == 2 and "--expect-hash" in r.output


@pytest.mark.parametrize("bad", ["abc", "sha256:x", "sha256:" + "A" * 64, "sha256:" + "a" * 63])
def test_job_switch_malformed_expect_hash_exits_2(reg, monkeypatch, bad):
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "1", "--expect-hash", bad)
    assert r.exit_code == 2


def test_job_switch_explicit_root_without_the_skill_does_not_fall_back(
    reg, make_bundle, monkeypatch, tmp_path
):
    _publish(reg, make_bundle(DEPT, SKILL, body="one"))
    empty = tmp_path / "empty"
    empty.mkdir()
    before = _writes(reg)
    r = _cli(
        reg, monkeypatch, "--action", "switch", "--version", "latest",
        "--skills-root", str(empty), "--expect-hash", GOOD_HASH,
    )  # fmt: skip
    assert r.exit_code == 2 and "no skill folder" in r.output
    assert _writes(reg) == before


def test_skills_root_env_restores_an_unset_variable_and_survives_errors(monkeypatch):
    monkeypatch.delenv("SKILLHUB_SKILLS", raising=False)
    with cli._skills_root_env("/x"):
        assert os.environ["SKILLHUB_SKILLS"] == "/x"
    assert "SKILLHUB_SKILLS" not in os.environ
    monkeypatch.setenv("SKILLHUB_SKILLS", "/before")
    with pytest.raises(RuntimeError), cli._skills_root_env("/x"):
        raise RuntimeError("boom")
    assert os.environ["SKILLHUB_SKILLS"] == "/before"
    monkeypatch.delenv("SKILLHUB_SKILLS")
    with pytest.raises(RuntimeError), cli._skills_root_env("/x"):
        raise RuntimeError("boom")
    assert "SKILLHUB_SKILLS" not in os.environ


def test_job_switch_publish_refuses_content_swapped_after_the_eval(reg, make_bundle, monkeypatch, tmp_path):
    """The folder held good content G during the eval and merged content M at publish time. The
    expected hash is M's (the merge), so only the eval rows can catch the swap."""
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL, body="good"), run_key="merge_abc1234_a1")
    merged = make_bundle(DEPT, SKILL, body="merged, never evaluated")
    args = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "merge_abc1234_a1")
    r = _cli(reg, monkeypatch, *args, "--expect-hash", _hash_of(merged), "--job-run-id", "3")
    assert r.exit_code == 2, r.output
    assert "ran on other content" in r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 0 and not reg.uc.list(f"skill_hub.dept_{DEPT}_versions")
    assert [h.action for h in reg.journal.history(DEPT, SKILL)] == ["publish_refused"]


def test_job_switch_publish_refuses_a_failing_eval_and_accepts_run_key(
    reg, make_bundle, monkeypatch, tmp_path
):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    b = evaluated(
        reg, make_bundle(DEPT, SKILL), run_key="k1", cells=(("trigger", "pass"), ("with_skill", "fail"))
    )
    base = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "k1")
    r = _cli(reg, monkeypatch, *base, "--run-key", "k1")
    assert r.exit_code == 2 and "with_skill=fail" in r.output
    evaluated(reg, b, run_key="k2")
    r = _cli(
        reg, monkeypatch, "--action", "publish", "--git-sha", SHA, "--eval-run-id", "k2", "--run-key", "k2"
    )
    assert r.exit_code == 0, r.output
    assert [h.action for h in reg.journal.history(DEPT, SKILL)] == ["publish_refused", "published"]


# --- Only the ci principal publishes ---


def _publish_args(*extra):
    return ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "r", *extra)


@pytest.mark.parametrize("creator", ["app-sp", "someone@example.com", None])
def test_job_switch_publish_refuses_a_job_run_ci_did_not_start(
    reg, make_bundle, monkeypatch, tmp_path, creator
):
    """The Skill Hub (the app principal) may start skill-switch runs to switch and retire, never to publish."""
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL))
    seen = []
    monkeypatch.setattr(cli, "_job_run_creator", lambda w, run_id: seen.append(run_id) or creator)
    r = _cli(reg, monkeypatch, *_publish_args("--job-run-id", "123"))
    assert r.exit_code == 2, r.output
    assert "only ci publishes" in r.output and seen == ["123"]
    assert reg.journal.latest_version(DEPT, SKILL) == 0
    assert [h.action for h in reg.journal.history(DEPT, SKILL)] == ["publish_refused"]


@pytest.mark.parametrize("job_run_id", ["", "job-3", "12; x"])
def test_job_switch_publish_outside_a_job_run_is_refused(reg, make_bundle, monkeypatch, tmp_path, job_run_id):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL))
    monkeypatch.setattr(cli, "_job_run_creator", lambda w, run_id: pytest.fail("no lookup without a run id"))
    r = _cli(reg, monkeypatch, *_publish_args("--job-run-id", job_run_id))
    assert r.exit_code == 2 and "skill-switch Job run" in r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 0


def test_job_switch_publish_fails_closed_without_the_ci_principal_or_the_run(
    reg, make_bundle, monkeypatch, tmp_path
):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL))
    monkeypatch.delenv("SKILLHUB_CI_PRINCIPAL")
    r = _cli(reg, monkeypatch, *_publish_args())
    assert r.exit_code == 2 and "SKILLHUB_CI_PRINCIPAL is not set" in r.output
    monkeypatch.setenv("SKILLHUB_CI_PRINCIPAL", CI)

    def boom(w, run_id):
        raise RuntimeError("jobs API down")

    monkeypatch.setattr(cli, "_job_run_creator", boom)
    r = _cli(reg, monkeypatch, *_publish_args())
    assert r.exit_code == 2 and "cannot read who started Job run 77" in r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 0


def test_job_switch_switch_and_retire_stay_open_to_the_app(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL))
    assert _cli(reg, monkeypatch, *_publish_args()).exit_code == 0
    monkeypatch.setattr(cli, "_job_run_creator", lambda w, run_id: "app-sp")
    assert _cli(reg, monkeypatch, "--action", "switch", "--version", "1").exit_code == 0
    assert _cli(reg, monkeypatch, "--action", "retire").exit_code == 0


def test_job_run_creator_reads_creator_user_name():
    from types import SimpleNamespace

    calls = []

    def get_run(run_id):
        calls.append(run_id)
        return SimpleNamespace(creator_user_name="ci-app")

    w = SimpleNamespace(jobs=SimpleNamespace(get_run=get_run))
    assert _REAL_JOB_RUN_CREATOR(w, "77") == "ci-app" and calls == [77]


# --- Publish binds the eval inputs from the merge plan ---


@pytest.mark.parametrize(
    ("rubric", "dataset", "message"),
    [
        (RUBRIC, "sha256:" + "e" * 64, "cases and fixtures dataset_digest="),
        ("none", DATASET, "rubric rubric_hash="),
    ],
)
def test_job_switch_publish_refuses_eval_inputs_swapped_during_the_eval(
    reg, make_bundle, monkeypatch, tmp_path, rubric, dataset, message
):
    """N2: an evals.json, rubric or fixture replaced in the candidate folder during the eval gives rows with
    other digests than the merge plan's, so publish refuses although the content hash matches."""
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL))
    args = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "r")
    r = _cli(reg, monkeypatch, *args, "--expect-rubric-hash", rubric, "--expect-dataset-digest", dataset)
    assert r.exit_code == 2, r.output
    assert "other eval inputs" in r.output and message in r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 0
    assert [h.action for h in reg.journal.history(DEPT, SKILL)] == ["publish_refused"]


def test_job_switch_publish_without_the_eval_inputs_is_refused(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL))
    args = ("--action", "publish", "--git-sha", SHA, "--eval-run-id", "r")
    r = _cli(reg, monkeypatch, *args, "--expect-rubric-hash", "", "--expect-dataset-digest", "")
    assert r.exit_code == 2 and "rubric_hash and dataset_digest" in r.output
    assert reg.journal.latest_version(DEPT, SKILL) == 0


@pytest.mark.parametrize(
    ("flag", "bad"), [("--expect-rubric-hash", "sha256:xyz"), ("--expect-dataset-digest", "none")]
)
def test_job_switch_malformed_eval_inputs_exit_2(reg, monkeypatch, flag, bad):
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "1", flag, bad)
    assert r.exit_code == 2 and flag in r.output


# --- Publish needs every planned cell ---


def test_job_switch_publish_refuses_a_matrix_with_a_missing_cell(reg, make_bundle, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    evaluated(reg, make_bundle(DEPT, SKILL), cells=(("trigger", "pass"), ("without_skill", "fail")))
    r = _cli(reg, monkeypatch, "--action", "publish", "--git-sha", SHA, "--eval-run-id", "r")
    assert r.exit_code == 2 and "planned cell(s) gpt/with_skill" in r.output
    assert [h.action for h in reg.journal.history(DEPT, SKILL)] == ["publish_refused"]


@pytest.mark.parametrize("bad", ["gpt", "gpt/without_skill", "gpt/trigger,x y/trigger"])
def test_job_switch_malformed_cells_exit_2(reg, monkeypatch, bad):
    r = _cli(reg, monkeypatch, "--action", "switch", "--version", "1", "--expect-cells", bad)
    assert r.exit_code == 2 and "--expect-cells" in r.output
