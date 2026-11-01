import json
import shutil

import pytest
from typer.testing import CliRunner

from skillhub import cli
from skillhub.bundle import content_hash
from skillhub.cli import app
from skillhub.lint import LintRefused
from skillhub.registry.versions import MissingProvenance, PublishRefused, publish, require_evaluated
from tests.unit.eval_rows import (
    CELLS,
    DATASET,
    RUBRIC,
    TRUSTED,
    add_rows,
    ensure_eval_runs,
    evaluated,
    expected_inputs,
    trust_ci_eval,
)

_REAL_LOCAL_EVAL_DIGESTS = cli._local_eval_digests  # cli_reg replaces it
_REAL_LOCAL_CELLS = cli._local_cells

SHA = "a" * 40


@pytest.fixture(autouse=True)
def _gate_readers(reg, monkeypatch):
    """The publish gate runs the merge gate's real readers over FakeSql eval_runs tables."""
    ensure_eval_runs(reg.sql, reg.cfg)
    trust_ci_eval(monkeypatch)


def _publish(reg, bundle):
    return publish(
        reg.cfg, reg.uc, reg.journal, bundle, git_sha=SHA, eval_run_id="run-1", actor="ci", job_run_id="job-7"
    )


def test_publish_creates_version_one_and_journals_it(reg, make_bundle):
    b = make_bundle("equity_research", "earnings-preview", body="v1 body")
    r = _publish(reg, b)
    assert (r.version, r.created) == (1, True)
    assert r.versioned_fqn == "skill_hub.dept_equity_research_versions.earnings-preview-v1"
    assert reg.uc.read_files(r.versioned_fqn) == b.files
    tags = reg.uc.get_tags(r.versioned_fqn)
    assert tags["version"] == "1" and tags["content_hash"] == content_hash(b.files) and tags["git_sha"] == SHA
    assert [(h.action, h.version) for h in reg.journal.history("equity_research", "earnings-preview")] == [
        ("published", 1)
    ]


def test_identical_content_is_not_republished(reg, make_bundle):
    b = make_bundle("common", "audit-xls")
    first = _publish(reg, b)
    again = _publish(reg, b)
    assert again.created is False and again.version == first.version
    assert len(reg.journal.history("common", "audit-xls")) == 1


def test_new_content_gets_the_next_number(reg, make_bundle):
    _publish(reg, make_bundle("common", "audit-xls", body="one"))
    assert _publish(reg, make_bundle("common", "audit-xls", body="two")).version == 2


def test_publish_refuses_invalid_name_before_any_write(reg, make_bundle):
    b = make_bundle("common", "Bad_Name")
    with pytest.raises(LintRefused):
        _publish(reg, b)
    assert reg.uc.list("skill_hub.common_versions") == []
    assert reg.journal.history("common", "Bad_Name") == []


def test_publish_after_crash_before_journal_replaces_the_partial_version(reg, make_bundle):
    b = make_bundle("common", "audit-xls")
    reg.uc.fail_once("finalize")
    with pytest.raises(RuntimeError):
        _publish(reg, b)
    assert reg.journal.latest_version("common", "audit-xls") == 0
    r = _publish(reg, b)
    assert (r.version, r.created) == (1, True)


def test_failed_upload_leaves_no_version_and_no_journal_row(reg, make_bundle):
    reg.uc.fail_once("upload_file")
    with pytest.raises(RuntimeError):
        _publish(reg, make_bundle("common", "audit-xls"))
    assert reg.uc.list("skill_hub.common_versions") == []
    assert reg.journal.history("common", "audit-xls") == []


def test_cleanup_passes_the_current_etag_to_delete(reg, make_bundle, monkeypatch):
    seen = []
    real = reg.uc.delete
    monkeypatch.setattr(reg.uc, "delete", lambda fqn, etag=None: (seen.append(etag), real(fqn, etag=etag))[1])
    reg.uc.fail_once("finalize")
    with pytest.raises(RuntimeError):
        _publish(reg, make_bundle("common", "audit-xls"))
    assert len(seen) == 1 and seen[0]  # a real etag, and the stale-etag-raising fake accepted it
    assert reg.uc.list("skill_hub.common_versions") == []


def test_leftover_partial_version_is_deleted_with_its_etag_before_republish(reg, make_bundle, monkeypatch):
    reg.uc.create("skill_hub.common_versions", "audit-xls-v1")
    etag = reg.uc.get("skill_hub.common_versions.audit-xls-v1").etag
    seen = []
    real = reg.uc.delete
    monkeypatch.setattr(reg.uc, "delete", lambda fqn, etag=None: (seen.append(etag), real(fqn, etag=etag))[1])
    assert _publish(reg, make_bundle("common", "audit-xls")).created is True
    assert seen == [etag]


@pytest.mark.parametrize("field", ["git_sha", "eval_run_id"])
@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_provenance_is_refused_before_any_write(reg, make_bundle, field, empty):
    kw = {"git_sha": SHA, "eval_run_id": "run-1", field: empty}
    with pytest.raises(MissingProvenance):
        publish(
            reg.cfg,
            reg.uc,
            reg.journal,
            make_bundle("common", "audit-xls"),
            actor="ci",
            job_run_id=None,
            **kw,
        )
    assert reg.uc.list("skill_hub.common_versions") == []
    assert reg.journal.history("common", "audit-xls") == []


# --- CLI ---


@pytest.fixture
def cli_reg(reg, make_bundle, tmp_path, monkeypatch):
    """publish wired to the fakes; skills/ lives under tmp_path (the CLI reads ./skills)."""
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "elsewhere"  # not the cwd, so only $SKILLHUB_SKILLS can find the bundle
    monkeypatch.setenv("SKILLHUB_SKILLS", str(elsewhere))
    monkeypatch.setattr(cli, "_services", lambda cfg: (None, reg.uc, reg.sql, reg.journal))
    monkeypatch.setattr(cli, "load_config", lambda path: reg.cfg)
    expected_inputs(monkeypatch)
    b = evaluated(reg, make_bundle("common", "audit-xls"), run_key="run_1")
    shutil.move(str(tmp_path / "skills"), str(elsewhere))
    assert not (tmp_path / "skills").exists() and b.root != elsewhere
    return reg


def _invoke(*extra):
    args = ["publish", "--dept", "common", "--skill", "audit-xls", "--git-sha", SHA]
    args += ["--eval-run-id", "run-1", "--actor", "ci", *extra]
    return CliRunner().invoke(app, args)


def test_cli_publish_creates_a_version(cli_reg, published_experiments):
    r = _invoke("--job-run-id", "job-7")
    assert r.exit_code == 0, r.output
    assert published_experiments == [("common", "audit-xls")]  # its own experiment, from now on
    assert json.loads(r.output) == {
        "version": 1,
        "versioned_fqn": "skill_hub.common_versions.audit-xls-v1",
        "created": True,
    }
    (row,) = cli_reg.journal.history("common", "audit-xls")
    assert (row.action, row.job_run_id) == ("published", "job-7")


def test_cli_republish_of_identical_content_reports_created_false(cli_reg):
    assert json.loads(_invoke().output)["created"] is True
    r = _invoke()
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {
        "version": 1,
        "versioned_fqn": "skill_hub.common_versions.audit-xls-v1",
        "created": False,
    }
    assert len(cli_reg.journal.history("common", "audit-xls")) == 1


def test_cli_empty_string_job_run_id_is_unset(cli_reg):
    r = _invoke("--job-run-id", "")
    assert r.exit_code == 0, r.output
    (row,) = cli_reg.journal.history("common", "audit-xls")
    assert row.job_run_id is None


def test_cli_missing_skill_folder_is_a_usage_error(cli_reg, tmp_path):
    shutil.rmtree(tmp_path / "elsewhere" / "common" / "audit-xls")
    assert _invoke().exit_code == 2


def test_cli_empty_git_sha_exits_1_with_one_line(cli_reg):
    args = ["publish", "--dept", "common", "--skill", "audit-xls", "--git-sha", "", "--eval-run-id", "r"]
    r = CliRunner().invoke(app, [*args, "--actor", "ci"])
    assert r.exit_code == 1
    assert r.output.strip().count("\n") == 0 and "git_sha" in r.output
    assert cli_reg.journal.history("common", "audit-xls") == []


def test_cli_lint_refusal_exits_1_with_one_line(cli_reg, make_bundle, tmp_path):
    shutil.rmtree(tmp_path / "elsewhere" / "common" / "audit-xls")
    make_bundle("common", "bad_name")  # a safe name that lint refuses (no underscores)
    shutil.move(str(tmp_path / "skills"), str(tmp_path / "elsewhere2"))
    shutil.rmtree(tmp_path / "elsewhere")
    shutil.move(str(tmp_path / "elsewhere2"), str(tmp_path / "elsewhere"))
    args = ["publish", "--dept", "common", "--skill", "bad_name", "--git-sha", SHA]
    r = CliRunner().invoke(app, [*args, "--eval-run-id", "r", "--actor", "ci"])
    assert r.exit_code == 1
    assert r.output.strip().count("\n") == 0
    assert cli_reg.uc.list("skill_hub.common_versions") == []


# --- publish gate: published content must equal evaluated content ---


def _gate(reg, bundle, run_key="r", trusted=(TRUSTED,), rubric=RUBRIC, dataset=DATASET, cells=CELLS, **kw):
    require_evaluated(
        reg.cfg,
        reg.sql,
        bundle.dept,
        bundle.name,
        run_key=run_key,
        content_hash=content_hash(bundle.files),
        trusted=trusted,
        rubric_hash=rubric,
        dataset_digest=dataset,
        cells=cells,
        **kw,
    )


@pytest.mark.parametrize("verdict", ["pass", "warn", "advisory"])
def test_gate_accepts_non_blocking_verdicts_on_the_same_content(reg, make_bundle, verdict):
    b = evaluated(
        reg, make_bundle("common", "audit-xls"), cells=(("trigger", verdict), ("with_skill", "pass"))
    )
    _gate(reg, b)


def test_gate_refuses_content_with_no_eval_rows(reg, make_bundle):
    b = make_bundle("common", "audit-xls")
    add_rows(reg.sql, reg.cfg, "common", "other-skill", "r", content_hash(b.files))  # another skill only
    with pytest.raises(PublishRefused, match="no gating eval result for common/audit-xls"):
        _gate(reg, b)


def test_gate_refuses_content_that_changed_after_the_eval(reg, make_bundle):
    """The eval ran on good content G; the folder was swapped back to M before publish."""
    evaluated(reg, make_bundle("common", "audit-xls", body="good"))
    swapped = make_bundle("common", "audit-xls", body="never evaluated")
    with pytest.raises(PublishRefused, match="ran on other content"):
        _gate(reg, swapped)


@pytest.mark.parametrize("verdict", ["fail", "inconclusive", None, "bogus"])
@pytest.mark.parametrize("mode", ["trigger", "with_skill"])
def test_gate_refuses_a_blocking_latest_verdict(reg, make_bundle, verdict, mode):
    reg.cfg.defaults.gating_modes = ["trigger", "effectiveness"]
    cells = {"trigger": "pass", "with_skill": "pass"} | {mode: verdict}
    b = evaluated(reg, make_bundle("common", "audit-xls"), cells=tuple(cells.items()))
    with pytest.raises(PublishRefused, match=f"gpt/{mode}={verdict or 'none'}"):
        _gate(reg, b)


def test_gate_ignores_the_without_skill_baseline(reg, make_bundle):
    b = evaluated(
        reg, make_bundle("common", "audit-xls"), cells=(("trigger", "pass"), ("without_skill", "fail"))
    )
    _gate(reg, b, cells=("gpt/trigger",))
    only_baseline = evaluated(reg, make_bundle("common", "comps"), cells=(("without_skill", "pass"),))
    with pytest.raises(PublishRefused, match="no gating eval result"):
        _gate(reg, only_baseline)


def test_gate_uses_the_latest_row_of_each_cell(reg, make_bundle):
    b = make_bundle("common", "audit-xls")
    evaluated(reg, b, cells=(("trigger", "fail"),), started_at="2026-10-28T09:00:00")
    evaluated(reg, b, cells=(("trigger", "pass"),), started_at="2026-10-28T10:00:00")
    _gate(reg, b, cells=("gpt/trigger",))  # an older failure was re-run and passed
    evaluated(reg, b, cells=(("trigger", "inconclusive"),), started_at="2026-10-28T11:00:00")
    with pytest.raises(PublishRefused, match="trigger=inconclusive"):
        _gate(reg, b, cells=("gpt/trigger",))


def test_gate_checks_every_model(reg, make_bundle):
    b = evaluated(reg, make_bundle("common", "audit-xls"))
    evaluated(reg, b, cells=(("trigger", "fail"),), model="claude-sonnet")
    with pytest.raises(PublishRefused, match="claude-sonnet/trigger=fail"):
        _gate(reg, b)


def test_gate_reads_only_the_given_run_key(reg, make_bundle):
    b = evaluated(reg, make_bundle("common", "audit-xls"), run_key="pr1_abc")
    with pytest.raises(PublishRefused, match="run key merge_abc"):
        _gate(reg, b, run_key="merge_abc")


def _refused(reg, dept="common", skill="audit-xls"):
    return [h for h in reg.journal.history(dept, skill) if h.action == "publish_refused"]


def test_cli_publish_refuses_unevaluated_content_with_exit_2_and_a_journal_row(cli_reg, tmp_path):
    (tmp_path / "elsewhere" / "common" / "audit-xls" / "SKILL.md").write_text(
        "---\nname: audit-xls\ndescription: Swapped.\n---\nnot what was evaluated\n"
    )
    r = _invoke("--job-run-id", "job-7")
    assert r.exit_code == 2, r.output
    assert (
        r.output.startswith("error: refusing to publish common/audit-xls")
        and "ran on other content" in r.output
    )
    assert cli_reg.uc.list("skill_hub.common_versions") == []
    (row,) = cli_reg.journal.history("common", "audit-xls")
    assert (row.action, row.job_run_id, row.eval_run_id, row.git_sha) == (
        "publish_refused",
        "job-7",
        "run-1",
        SHA,
    )
    assert row.content_hash.startswith("sha256:")


def test_cli_publish_refuses_a_quarantined_department(cli_reg, make_bundle, tmp_path):
    """The operator command refuses a quarantined department as the Job does, even for evaluated content."""
    b = evaluated(cli_reg, make_bundle("external", "vendor"), run_key="run_1")
    (tmp_path / "elsewhere" / "external").mkdir()
    shutil.move(str(b.root), str(tmp_path / "elsewhere" / "external" / "vendor"))
    args = ["publish", "--dept", "external", "--skill", "vendor", "--git-sha", SHA]
    r = CliRunner().invoke(app, [*args, "--eval-run-id", "run-1", "--actor", "ci"])
    assert r.exit_code == 1, r.output
    assert r.output.startswith(
        "error: refusing to publish external/vendor: department external is quarantined"
    )
    assert cli_reg.journal.history("external", "vendor") == []
    assert cli_reg.uc.list("skill_hub.external_versions") == []


def test_cli_publish_refuses_a_critical_scan_finding_with_exit_2_and_a_journal_row(cli_reg, tmp_path):
    """The last check before Unity Catalog: a credential in the bundle is never published, and the error does not
    quote it."""
    key = "AKIA" + "Q" * 16  # assembled here, so no file holds one
    skill_md = tmp_path / "elsewhere" / "common" / "audit-xls" / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + f"\nUse the key {key}.\n", encoding="utf-8")
    r = _invoke()
    assert r.exit_code == 2, r.output
    assert "refusing to publish common/audit-xls: the security scan found 1 critical finding(s)" in r.output
    assert "secret: an AWS access key ID (AKIA" in r.output and key not in r.output
    (row,) = cli_reg.journal.history("common", "audit-xls")
    assert row.action == "publish_refused"
    assert cli_reg.uc.list("skill_hub.common_versions") == []


def test_cli_publish_run_key_must_name_the_same_eval_as_eval_run_id(cli_reg):
    r = _invoke("--run-key", "pr9-other")
    assert r.exit_code == 2 and "different eval runs" in r.output
    assert cli_reg.journal.history("common", "audit-xls") == []
    r = _invoke("--run-key", "RUN-1")  # the same key once normalised
    assert r.exit_code == 0, r.output


def test_gate_ignores_rows_from_untrusted_run_authors(reg, make_bundle):
    """Any workspace user can log MLflow runs; only the eval principals' runs count."""
    swapped = evaluated(
        reg, make_bundle("common", "audit-xls", body="forged pass"), author="someone@example.com"
    )
    with pytest.raises(PublishRefused, match="no gating eval result"):
        _gate(reg, swapped)
    evaluated(reg, swapped)  # the same content, now evaluated by ci_eval
    _gate(reg, swapped)


@pytest.mark.parametrize("source", ["manual", "hub", "hub-try", "pr", "nightly", None])
def test_gate_accepts_only_rows_of_the_merge_eval(reg, make_bundle, source):
    """A Skill Hub run (manual or hub), a pull request eval or a nightly run evaluates the same content with a
    trusted author, but by default only the merge eval releases: its rows never satisfy publish, even under the
    gate's key."""
    b = evaluated(reg, make_bundle("common", "audit-xls"), trigger_source=source)
    with pytest.raises(PublishRefused, match="no gating eval result"):
        _gate(reg, b)


def test_gate_accepts_pull_request_gate_rows_when_the_release_reuses_them(reg, make_bundle):
    b = evaluated(reg, make_bundle("common", "audit-xls"), run_key="pr12_abc1234_a1", trigger_source="pr")
    _gate(reg, b, run_key="pr12_abc1234_a1", trigger_source="pr")
    with pytest.raises(PublishRefused, match="no gating eval result"):
        _gate(reg, b, run_key="pr12_abc1234_a1")  # a release that cites the merge eval never counts them


@pytest.mark.parametrize("source", ["manual", "hub", "hub-try", "nightly", "merge", None])
def test_a_reused_pull_request_gate_counts_only_its_own_rows(reg, make_bundle, source):
    """With trigger source pr, rows of any other eval under the same key do not count, the merge eval's included."""
    b = evaluated(reg, make_bundle("common", "audit-xls"), trigger_source=source)
    with pytest.raises(PublishRefused, match="no gating eval result"):
        _gate(reg, b, trigger_source="pr")


@pytest.mark.parametrize("source", ["manual", "hub", "hub-try", "nightly", "", "PR", "merge "])
def test_no_other_trigger_source_can_authorize_a_publish(reg, make_bundle, source):
    """Asking for another eval's rows is refused before any row is read, even when such rows exist and pass."""
    b = evaluated(reg, make_bundle("common", "audit-xls"), trigger_source=source)
    reg.sql.calls.clear()
    with pytest.raises(PublishRefused, match="cannot authorize a publish"):
        _gate(reg, b, trigger_source=source)
    assert reg.sql.calls == []


def test_the_gate_takes_its_trigger_source_from_the_merge_eval():
    """The value the publish gate accepts is the one the merge eval in skills-publish.yml sets."""
    from pathlib import Path

    from skillhub.registry.versions import MERGE_TRIGGER_SOURCE, PR_TRIGGER_SOURCE, PUBLISH_TRIGGER_SOURCES

    text = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "skills-publish.yml").read_text()
    assert f'trigger_source: "{MERGE_TRIGGER_SOURCE}"' in text
    pr = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "skills-pr.yml").read_text()
    assert f'trigger_source: "{PR_TRIGGER_SOURCE}"' in pr  # the gate a release may reuse
    assert PUBLISH_TRIGGER_SOURCES == {"merge", "pr"}


def test_gate_fails_closed_without_trusted_authors(reg, make_bundle):
    b = evaluated(reg, make_bundle("common", "audit-xls"))
    with pytest.raises(PublishRefused, match="cannot check the eval .* no trusted run authors"):
        _gate(reg, b, trusted=())


def test_gate_runs_the_merge_gates_reader_over_every_department(reg, make_bundle):
    """No stand-in: the SELECT that reaches SQL is ci.pr_comment's, and the rows it gives have that SELECT's
    columns, which are the ones the gate reads (dept, skill, content_hash, mode, verdict)."""
    from skillhub.ci.pr_comment import _matrix_sql, load_matrix_rows

    b = evaluated(reg, make_bundle("common", "audit-xls"))
    reg.sql.calls.clear()
    _gate(reg, b)
    [(statement, params)] = reg.sql.calls
    assert statement == _matrix_sql(reg.cfg, 1, merge_only=True)
    assert params == {"run_key": "r", "author_0": TRUSTED, "trigger_source": "merge"}
    assert statement.count("SELECT * EXCEPT (rn)") == len(reg.cfg.departments)
    rows = load_matrix_rows(reg.sql.execute, reg.cfg, "r", trusted={TRUSTED})
    assert {r["mode"] for r in rows} == {"trigger", "with_skill", "without_skill"}  # no parent row
    assert all(set(r) == set(rows[0]) and "rn" not in r for r in rows)
    assert {"dept", "skill", "content_hash", "model_profile", "mode", "verdict"} <= set(rows[0])


def test_cli_publish_trusts_only_the_ci_eval_principal_and_the_caller(cli_reg, make_bundle):
    """The CLI gate gets its trusted set from the real trusted_run_authors: $SKILLHUB_CI_EVAL_PRINCIPAL plus
    the caller's identity. A run logged by anyone else does not let the content through."""
    from skillhub.naming import evals_schema
    from tests.unit.eval_rows import CALLER

    cli_reg.sql.execute(f"DELETE FROM {evals_schema(cli_reg.cfg, 'common')}.eval_runs")  # the fixture's rows
    b = make_bundle("common", "audit-xls")  # the same content as the folder the CLI reads
    evaluated(cli_reg, b, run_key="run_1", author="someone@example.com")
    r = _invoke()
    assert r.exit_code == 2 and "no gating eval result" in r.output, r.output
    evaluated(cli_reg, b, run_key="run_1", author=CALLER)
    r = _invoke()
    assert r.exit_code == 0, r.output


# --- The publish gate binds the eval inputs ---


@pytest.mark.parametrize(
    ("rubric", "dataset"),
    [(RUBRIC, "sha256:" + "e" * 64), ("sha256:" + "c" * 64, DATASET), ("none", DATASET)],
)
def test_gate_refuses_rows_that_evaluated_other_eval_inputs(reg, make_bundle, rubric, dataset):
    b = evaluated(reg, make_bundle("common", "audit-xls"))
    with pytest.raises(PublishRefused, match="other eval inputs"):
        _gate(reg, b, rubric=rubric, dataset=dataset)


@pytest.mark.parametrize(("rubric", "dataset"), [(None, DATASET), (RUBRIC, None), ("", "")])
def test_gate_fails_closed_without_the_expected_eval_inputs(reg, make_bundle, rubric, dataset):
    b = evaluated(reg, make_bundle("common", "audit-xls"))
    with pytest.raises(PublishRefused, match="rubric_hash and dataset_digest"):
        _gate(reg, b, rubric=rubric, dataset=dataset)


def test_a_row_with_no_recorded_eval_inputs_never_passes(reg, make_bundle):
    b = make_bundle("common", "audit-xls")
    add_rows(reg.sql, reg.cfg, "common", "audit-xls", "r", content_hash(b.files), rubric=None, dataset=None)
    with pytest.raises(PublishRefused, match="other eval inputs"):
        _gate(reg, b)


def test_cli_publish_reads_the_eval_inputs_of_the_local_checkout(cli_reg, tmp_path, monkeypatch):
    """The direct publish binds the evals/ and fixtures of the checkout it publishes from."""
    from types import SimpleNamespace

    from skillhub.evalspec import eval_digests

    real = _REAL_LOCAL_EVAL_DIGESTS
    root = tmp_path / "elsewhere" / "common" / "audit-xls"
    (root / "evals").mkdir()
    (root / "evals" / "evals.json").write_text('{"skill_name": "audit-xls", "evals": []}')
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    monkeypatch.setenv("SKILLHUB_FIXTURES", str(fixtures))
    bundle = SimpleNamespace(root=root, dept="common", name="audit-xls")  # the parts it reads
    assert real(bundle) == eval_digests(root, fixtures)
    (root / "evals" / "evals.json").unlink()
    assert real(bundle) is None


# --- The publish gate needs every planned cell ---


def test_gate_refuses_when_a_failing_child_run_was_deleted_before_sync(reg, make_bundle):
    """The failing with_skill row is gone, the passing trigger row stays: the gate still refuses."""
    b = evaluated(reg, make_bundle("common", "audit-xls"), cells=(("trigger", "pass"),))
    with pytest.raises(PublishRefused, match="no trusted result for planned cell.*gpt/with_skill"):
        _gate(reg, b)


def test_gate_needs_every_planned_model(reg, make_bundle):
    b = evaluated(reg, make_bundle("common", "audit-xls"))
    with pytest.raises(PublishRefused, match="claude-sonnet/trigger, claude-sonnet/with_skill"):
        _gate(reg, b, cells=(*CELLS, "claude-sonnet/trigger", "claude-sonnet/with_skill"))
    _gate(reg, b)


@pytest.mark.parametrize("cells", [None, ()])
def test_gate_fails_closed_without_the_planned_cells(reg, make_bundle, cells):
    b = evaluated(reg, make_bundle("common", "audit-xls"))
    with pytest.raises(PublishRefused, match="planned gating cells"):
        _gate(reg, b, cells=cells)


def test_cli_publish_plans_the_cells_of_the_local_checkout(cli_reg, tmp_path):
    from types import SimpleNamespace

    root = tmp_path / "elsewhere" / "common" / "audit-xls"
    (root / "evals").mkdir()
    (root / "evals" / "evals.json").write_text(
        '{"skill_name": "audit-xls", "models": ["gpt"], "evals": [{"id": 1, "prompt": "p", "should_trigger": true}]}'
    )
    bundle = SimpleNamespace(root=root)
    assert _REAL_LOCAL_CELLS(cli_reg.cfg, bundle) == ("gpt/trigger",)
    (root / "evals" / "evals.json").unlink()
    assert _REAL_LOCAL_CELLS(cli_reg.cfg, bundle) is None


@pytest.mark.parametrize("verdict", ["fail", "inconclusive"])
def test_gate_reports_but_does_not_block_on_a_mode_outside_gating_modes(reg, make_bundle, verdict):
    reg.cfg.defaults.gating_modes = ["trigger"]
    b = evaluated(
        reg, make_bundle("common", "audit-xls"), cells=(("trigger", "pass"), ("with_skill", verdict))
    )
    _gate(reg, b)  # effectiveness is advisory: published on the trigger verdict alone
    blocked = evaluated(
        reg, make_bundle("common", "comps"), cells=(("trigger", "fail"), ("with_skill", "pass"))
    )
    with pytest.raises(PublishRefused, match="gpt/trigger=fail"):
        _gate(reg, blocked)
