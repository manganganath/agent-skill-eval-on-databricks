from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import typer

from skillhub import SkillHubError
from skillhub.auth import workspace_client
from skillhub.config import HubConfig, load_config

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # a traceback must never print locals such as tokens
    help="Skill Hub: registry, versions, switch, reconcile, eval and bootstrap.",
)


@app.callback()
def _root(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", envvar="SKILLHUB_CONFIG", help="Path to hub.yaml (default: config/hub.yaml)."
    ),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@contextmanager
def _guard() -> Iterator[None]:
    """Turn an expected failure into one error line and exit code 1."""
    try:
        yield
    except SkillHubError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e


def _cfg(ctx: typer.Context) -> HubConfig:
    if "cfg" not in ctx.obj:
        with _guard():
            ctx.obj["cfg"] = load_config(ctx.obj["config_path"])
    return ctx.obj["cfg"]


@app.command("config-show")
def config_show(ctx: typer.Context) -> None:
    """Print the loaded configuration with placeholders expanded."""
    cfg = _cfg(ctx)
    typer.echo(cfg.model_dump_json(by_alias=True, indent=2))


@app.command("check-permissions")
def check_permissions_cmd(ctx: typer.Context) -> None:
    """Report experiment ACL drift against UC grants. Exit 1 on drift, except `users` inheriting
    access from above the workspace root, which is reported only."""
    from skillhub.auth import workspace_client
    from skillhub.results.experiments import check_permissions, fails_check

    cfg = _cfg(ctx)
    with _guard():
        drifts = check_permissions(cfg, workspace_client())
    for d in drifts:
        typer.echo(json.dumps(asdict(d)))
    if any(fails_check(d) for d in drifts):
        raise typer.Exit(code=1)
    if drifts:
        typer.echo(
            "warning: users inherit access to the experiment root from a parent folder; set workspace_root "
            "outside /Workspace/Shared (see docs/operations.md)",
            err=True,
        )
        return
    typer.echo("ok: experiment permissions match UC grants")


@app.command()
def sync(
    ctx: typer.Context,
    run_key: str = typer.Option(..., "--run-key", help="Run label or key (normalised with make_run_key)."),
    rebuild: bool = typer.Option(False, "--rebuild", help="re-read every child run from MLflow"),
) -> None:
    """Sync a run from MLflow into the department eval tables."""
    from skillhub.auth import workspace_client
    from skillhub.naming import make_run_key
    from skillhub.results.sync import SyncIncomplete, sync_run
    from skillhub.sql import SqlClient

    cfg = _cfg(ctx)
    try:
        with _guard():
            report = sync_run(
                cfg,
                SqlClient(workspace_client(), cfg.workspace.sql_warehouse_id),
                make_run_key(run_key),
                rebuild=rebuild,
            )
    except (SyncIncomplete, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(json.dumps(asdict(report)))


@app.command()
def label(
    ctx: typer.Context,
    run_key: str = typer.Option(..., "--run-key", help="Run label or key (normalised with make_run_key)."),
) -> None:
    """Create a Review App labeling session for a run's traces and print one URL per experiment."""
    from mlflow.exceptions import MlflowException

    from skillhub.naming import make_run_key
    from skillhub.results.label import create_labeling_session

    cfg = _cfg(ctx)
    try:
        with _guard():
            urls = create_labeling_session(cfg, make_run_key(run_key), assessment_names=[])
    except (ValueError, MlflowException, ImportError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    for path, url in urls.items():
        typer.echo(f"{path}\t{url}")


@app.command("lint")
def lint_cmd(ctx: typer.Context, paths: list[str] = typer.Argument(None)) -> None:
    """Lint skill bundles (no Databricks needed). Exit 1 on any issue.

    Department folders and per-skill thresholds are checked against hub.yaml when it is found. Its ${VARS}
    are not expanded, so no workspace settings are needed.
    """
    import os

    from skillhub.bundle import skills_root
    from skillhub.config import load_lint_settings
    from skillhub.lint import lint_repo

    explicit = ctx.obj.get("config_path") or os.environ.get("SKILLHUB_CONFIG")
    path = Path(explicit or "config/hub.yaml")
    settings = None
    if explicit or path.is_file():  # an explicit config must exist; the default one is optional
        with _guard():
            try:
                settings = load_lint_settings(path)
            except OSError as e:
                raise SkillHubError(f"cannot read {path}: {e.strerror}") from None
    else:
        typer.echo(f"note: {path} not found; department and threshold checks skipped", err=True)
    issues = lint_repo(
        skills_root(),
        paths or [],
        departments=settings.departments if settings else None,
        defaults=settings.defaults if settings else None,
    )
    for issue in issues:
        typer.echo(str(issue))
    typer.echo(f"{len(issues)} issue(s)")
    raise typer.Exit(1 if issues else 0)


@app.command("import")
def import_cmd(
    ctx: typer.Context,
    source_dir: Path | None = typer.Option(
        None, "--source-dir", help="Use this checkout instead of cloning."
    ),
    dest: Path | None = typer.Option(None, "--dest", help="Default: $SKILLHUB_SKILLS or ./skills."),
) -> None:
    """Import skills from the pinned source."""
    import json

    from skillhub.importer import clone_source, import_skills

    cfg = _cfg(ctx)
    with _guard():
        if source_dir is not None:
            report = import_skills(cfg.import_, source_dir, dest, cfg.departments)
        else:
            with clone_source(cfg.import_.source) as src:
                report = import_skills(cfg.import_, src, dest, cfg.departments)
    typer.echo(json.dumps(report.as_dict(), indent=2))


def _services(cfg: HubConfig):
    """(workspace client, UCSkills, SqlClient, Journal) for commands that touch Databricks."""
    from skillhub.registry.journal import Journal
    from skillhub.registry.uc_skills import UCSkills
    from skillhub.sql import SqlClient

    w = workspace_client()
    sql = SqlClient(w, cfg.workspace.sql_warehouse_id)
    return w, UCSkills(w), sql, Journal(cfg, sql)


def _bundle(dept: str, skill: str):
    from skillhub.bundle import BundleError, check_names, load_bundle, skills_root

    try:
        check_names(dept, skill)  # before either becomes a file path
    except BundleError as e:
        raise typer.BadParameter(str(e)) from None
    root = skills_root() / dept / skill
    if not (root / "SKILL.md").is_file():
        raise typer.BadParameter(f"no skill folder at {root}")
    return load_bundle(root, dept)


_RUN_KEY_HELP = (
    "Run key of the eval that must have evaluated this content. Empty means the key of --eval-run-id."
)


def _gate_run_key(run_key: str | None, eval_run_id: str) -> str:
    """The run key whose eval rows gate a publish. A different --run-key and --eval-run-id is refused, so the
    version's provenance always names the eval that let it through."""
    from skillhub.naming import make_run_key

    try:
        provenance = make_run_key(eval_run_id)
        key = make_run_key(run_key) if _opt(run_key) else provenance
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--run-key") from None
    if key != provenance:
        raise typer.BadParameter(
            f"--run-key {key} and --eval-run-id {provenance} name different eval runs", param_hint="--run-key"
        )
    return key


def _refuse_publish(journal, bundle, actual: str, message: str, *, git_sha, eval_run_id, actor, job_run_id):
    """Journal a `publish_refused` row, print one error line and exit 2."""
    from skillhub.registry.journal import new_row

    journal.append(
        new_row(
            bundle.dept,
            bundle.name,
            "publish_refused",
            actor=actor,
            job_run_id=job_run_id,
            content_hash=actual,
            git_sha=git_sha or None,
            eval_run_id=eval_run_id or None,
        )
    )
    typer.echo(f"error: refusing to publish {bundle.dept}/{bundle.name}: {message}", err=True)
    raise typer.Exit(2)


def _require_evaluated(
    cfg,
    w,
    sql,
    journal,
    bundle,
    actual: str,
    *,
    run_key,
    git_sha,
    eval_run_id,
    actor,
    job_run_id,
    rubric_hash: str | None,
    dataset_digest: str | None,
    cells: tuple[str, ...] | None,
    trigger_source: str = "merge",
) -> None:
    """Exit 2, with a `publish_refused` journal row, unless the eval for the run key passed this content with
    these eval inputs in every planned cell. Only rows with `trigger_source` (merge or pr) count."""
    from skillhub.lint import assert_clean
    from skillhub.registry.versions import PublishRefused, require_evaluated
    from skillhub.results.experiments import trusted_run_authors

    assert_clean(bundle)  # an invalid bundle is a lint refusal (exit 1), not a gate refusal
    key = _gate_run_key(run_key, eval_run_id)
    try:
        require_evaluated(
            cfg,
            sql,
            bundle.dept,
            bundle.name,
            run_key=key,
            content_hash=actual,
            trusted=trusted_run_authors(w),
            rubric_hash=rubric_hash,
            dataset_digest=dataset_digest,
            cells=cells,
            trigger_source=trigger_source,
        )
    except PublishRefused as e:
        _refuse_publish(
            journal, bundle, actual, str(e),
            git_sha=git_sha, eval_run_id=eval_run_id, actor=actor, job_run_id=job_run_id,
        )  # fmt: skip


def _job_run_creator(w, job_run_id: str) -> str | None:
    """Who started this Job run: `creator_user_name`, the application ID for a service principal."""
    return getattr(w.jobs.get_run(int(job_run_id)), "creator_user_name", None)


def _require_ci_publisher(
    w, journal, bundle, actual: str, *, git_sha, eval_run_id, actor, job_run_id
) -> None:
    """Only the ci principal publishes. The skill-switch Job also runs switches and retires that the
    Skill Hub (the app principal) starts, so `job-switch --action publish` checks who started this Job run.
    Exit 2, with a `publish_refused` journal row, unless that is the ci principal."""
    import os

    from skillhub.results.experiments import CI_ENV

    ci = (os.environ.get(CI_ENV) or "").strip()
    refuse = functools.partial(
        _refuse_publish, journal, bundle, actual,
        git_sha=git_sha, eval_run_id=eval_run_id, actor=actor, job_run_id=job_run_id,
    )  # fmt: skip
    if not ci:
        refuse(f"{CI_ENV} is not set, so the publisher cannot be checked")
    if not job_run_id or not job_run_id.isdecimal():
        refuse("publish runs only in a skill-switch Job run (--job-run-id {{job.run_id}}) started by ci")
    try:
        creator = _job_run_creator(w, job_run_id)
    except Exception as e:  # noqa: BLE001 - any failure to read the run fails closed
        refuse(f"cannot read who started Job run {job_run_id} ({type(e).__name__})")
    if creator != ci:
        refuse(f"Job run {job_run_id} was started by {creator or 'an unknown identity'}; only ci publishes")


def _local_eval_digests(bundle):
    """The eval inputs of a local checkout: the bundle's evals/ and $SKILLHUB_FIXTURES (or ./fixtures). None when
    they cannot be read; the publish gate then refuses."""
    import os

    from skillhub.evalspec import eval_digests

    try:
        return eval_digests(bundle.root, Path(os.environ.get("SKILLHUB_FIXTURES", "fixtures")))
    except (OSError, ValueError) as e:
        typer.echo(f"warning: cannot read the eval inputs of {bundle.dept}/{bundle.name}: {e}", err=True)
        return None


def _local_cells(cfg, bundle) -> tuple[str, ...] | None:
    """The gating cells a default CI eval of this checkout's evals.json plans. None when unreadable."""
    from skillhub.evalspec import planned_cells, read_evals

    try:
        ef, _ = read_evals(bundle.root / "evals" / "evals.json")
    except (OSError, ValueError):
        return None
    return tuple(planned_cells(ef, default_models=cfg.defaults.models))


def _refuse_quarantined(cfg, dept: str, skill: str) -> None:
    """A quarantined department is evaluated in pull requests but never released. Publish refuses it before it
    reads or writes anything; retire stays allowed. Moving a skill out of quarantine is an operator task."""
    if cfg.departments[dept].quarantine:
        raise SkillHubError(
            f"refusing to publish {dept}/{skill}: department {dept} is quarantined and is never released. "
            "To release the skill, an operator moves it out of quarantine (see docs/operations.md)."
        )


def _ensure_skill_experiment(cfg, dept: str, skill: str) -> None:
    """The published skill's own experiment and its registered judge, as bootstrap makes them. Idempotent.

    Until now its evals ran in the department's unreleased experiment, because the eval identities cannot create
    experiments; publish runs as ci, which can. From here on the nightly and later evals use this one. It runs after
    the gate and the publish, so only evaluated content gets one. A failure fails the Job with the version already
    journaled, and a re-run (the publish is then `unchanged`) converges."""
    from skillhub.eval.judges import register_judges
    from skillhub.results.experiments import ensure_experiment

    register_judges(cfg, ensure_experiment(cfg, dept, skill))


@app.command("publish")
def publish_cmd(
    ctx: typer.Context,
    dept: str = typer.Option(..., "--dept"),
    skill: str = typer.Option(..., "--skill"),
    git_sha: str = typer.Option(..., "--git-sha"),
    eval_run_id: str = typer.Option(..., "--eval-run-id"),
    actor: str = typer.Option(..., "--actor"),
    job_run_id: str | None = typer.Option(None, "--job-run-id", envvar="SKILLHUB_JOB_RUN_ID"),
    run_key: str = typer.Option("", "--run-key", help=_RUN_KEY_HELP),
) -> None:
    """Register the next version of <skills root>/<dept>/<skill>. The live skill is not touched.

    Run this only through the skill-switch Job, which serialises publish, switch and retire. Exit 2 when the
    eval for the run key did not evaluate this content with the eval inputs of this checkout (its evals/ and
    $SKILLHUB_FIXTURES), or blocks it. Exit 1 for a quarantined department.
    """
    import json
    from dataclasses import asdict

    from skillhub.bundle import content_hash
    from skillhub.registry.versions import check_provenance, publish

    cfg = _cfg(ctx)
    _check_skill_ref(cfg, dept, skill)
    with _guard():
        _refuse_quarantined(cfg, dept, skill)
        w, uc, sql, journal = _services(cfg)
        bundle = _bundle(dept, skill)
        check_provenance(git_sha, eval_run_id)
        digests = _local_eval_digests(bundle)
        _require_evaluated(
            cfg, w, sql, journal, bundle, content_hash(bundle.files),
            run_key=run_key, git_sha=git_sha, eval_run_id=eval_run_id, actor=actor, job_run_id=_opt(job_run_id),
            rubric_hash=digests.rubric_hash if digests else None,
            dataset_digest=digests.dataset_digest if digests else None,
            cells=_local_cells(cfg, bundle),
        )  # fmt: skip
        res = publish(
            cfg,
            uc,
            journal,
            bundle,
            git_sha=git_sha,
            eval_run_id=eval_run_id,
            actor=actor,
            job_run_id=job_run_id or None,  # Jobs pass "" for an unset parameter
        )
        _ensure_skill_experiment(cfg, dept, skill)
    typer.echo(json.dumps(asdict(res), indent=2))


@app.command("reconcile")
def reconcile_cmd(ctx: typer.Context) -> None:
    """Compare live skills with the journal. Exit 1 if anything needs a person."""
    from skillhub.registry.reconcile import reconcile

    cfg = _cfg(ctx)
    with _guard():
        _, uc, _, journal = _services(cfg)
        issues = reconcile(cfg, uc, journal)
    for i in issues:
        typer.echo(f"{i.dept}/{i.skill}: {i.kind}: {i.detail}")
    if not issues:
        typer.echo("no issues")
    raise typer.Exit(1 if issues else 0)


def _opt(value: str | None) -> str | None:
    """Jobs pass "" for an unset parameter."""
    value = (value or "").strip()
    return value or None


@app.command("switch")
def switch_cmd(
    ctx: typer.Context,
    dept: str = typer.Option(..., "--dept"),
    skill: str = typer.Option(..., "--skill"),
    to: int = typer.Option(..., "--to"),
    actor: str = typer.Option(..., "--actor"),
    job_run_id: str | None = typer.Option(None, "--job-run-id", envvar="SKILLHUB_JOB_RUN_ID"),
) -> None:
    """Switch the live skill to a published version. Exit 1 unless the result is switched or noop.

    Run only through the skill-switch Job (`max_concurrent_runs: 1`), which serialises changes.
    """
    import json
    from dataclasses import asdict

    from skillhub.registry.switch import switch

    cfg = _cfg(ctx)
    _check_skill_ref(cfg, dept, skill)
    with _guard():
        _, uc, _, journal = _services(cfg)
        res = switch(cfg, uc, journal, dept, skill, to, actor=actor, job_run_id=_opt(job_run_id))
    typer.echo(json.dumps(asdict(res), indent=2))
    if res.status not in ("switched", "noop"):
        raise typer.Exit(1)


@app.command("retire")
def retire_cmd(
    ctx: typer.Context,
    dept: str = typer.Option(..., "--dept"),
    skill: str = typer.Option(..., "--skill"),
    actor: str = typer.Option(..., "--actor"),
    job_run_id: str | None = typer.Option(None, "--job-run-id", envvar="SKILLHUB_JOB_RUN_ID"),
) -> None:
    """Retire a skill: delete the live copy and keep its versions.

    Run only through the skill-switch Job (`max_concurrent_runs: 1`), which serialises changes.
    """
    from skillhub.registry.switch import retire

    cfg = _cfg(ctx)
    _check_skill_ref(cfg, dept, skill)
    with _guard():
        _, uc, _, journal = _services(cfg)
        status = retire(cfg, uc, journal, dept, skill, actor=actor, job_run_id=_opt(job_run_id))
    typer.echo(f"retired {dept}/{skill}" if status == "retired" else "nothing to retire")


def _resolve_version(journal, dept: str, skill: str, version: str, *, strict: bool = False) -> int:
    if version == "latest":
        # A publish of unchanged content returns the old number, so match the bundle by hash first.
        from skillhub.bundle import content_hash, skills_root

        try:
            bundle = _bundle(dept, skill)
        except typer.BadParameter:
            if strict:  # an explicit --skills-root never falls back to the newest version
                raise
            bundle = None  # no bundle in this checkout: the only case that falls back
        if bundle is None:
            target = journal.latest_version(dept, skill)
            if not target:
                raise typer.BadParameter(f"{dept}/{skill} has no published version")
            return target
        found = journal.version_by_hash(dept, skill, content_hash(bundle.files))
        if found is None:
            raise typer.BadParameter(
                f"content at {skills_root() / dept / skill} is not published; "
                "publish it or pass a version number"
            )
        return found
    if version.isdecimal() and int(version) > 0:
        return int(version)
    raise typer.BadParameter("--version must be a positive number or 'latest'")


@app.command("job-switch")
def job_switch_cmd(
    ctx: typer.Context,
    action: str = typer.Option(..., "--action", help="publish, switch or retire."),
    dept: str = typer.Option(..., "--dept"),
    skill: str = typer.Option(..., "--skill"),
    version: str = typer.Option("", "--version", help="For switch: a number or 'latest'."),
    git_sha: str = typer.Option("", "--git-sha", help="Needed for publish."),
    eval_run_id: str = typer.Option("", "--eval-run-id", help="Needed for publish."),
    actor: str = typer.Option(..., "--actor"),
    job_run_id: str = typer.Option("", "--job-run-id", envvar="SKILLHUB_JOB_RUN_ID"),
    skills_root_opt: str = typer.Option(
        "",
        "--skills-root",
        help="Skills root for this process, overriding $SKILLHUB_SKILLS. Used by publish and 'latest'.",
    ),
    expect_hash: str = typer.Option(
        "",
        "--expect-hash",
        help="Refuse unless the bundle (publish) or the target version (switch) has this content_hash.",
    ),
    run_key: str = typer.Option("", "--run-key", help="For publish: " + _RUN_KEY_HELP),
    expect_rubric_hash: str = typer.Option(
        "",
        "--expect-rubric-hash",
        help="For publish: the merge plan's rubric_hash (sha256:<hex> or none).",
    ),
    expect_dataset_digest: str = typer.Option(
        "",
        "--expect-dataset-digest",
        help="For publish: the merge plan's dataset_digest (sha256:<hex>).",
    ),
    expect_cells: str = typer.Option(
        "", "--expect-cells", help="For publish: the merge plan's gating cells, model/mode,...."
    ),
    trigger_source: str = typer.Option(
        "merge",
        "--trigger-source",
        help="For publish: the trigger source of the eval for the run key, merge or pr (a reused pull request "
        'gate). "" means merge.',
    ),
) -> None:
    """Single entry point of the skill-switch Job. Exit 1 if a switch is refused or fails.

    Publish exits 2 unless the ci principal started this Job run and the eval for the run key evaluated
    exactly this content and did not block it. That eval is the merge eval, or the pull request gate when the
    release reuses it (--trigger-source pr); no other eval's rows count. Publish exits 1 for a quarantined
    department; retire there is allowed. Switch and retire are also started by the Skill Hub app. Jobs pass "" for
    an unset parameter, so every optional value accepts "".
    """
    root = _opt(skills_root_opt)
    expected = _opt(expect_hash)
    if expected is not None and not _HASH.fullmatch(expected):
        raise typer.BadParameter("--expect-hash must look like sha256:<64 lowercase hex digits>")
    if root is not None and expected is None:
        raise typer.BadParameter(
            "--skills-root needs --expect-hash, so the caller binds the content it checked"
        )
    from skillhub.ci.pr_comment import parse_cells

    try:
        cells = parse_cells(expect_cells or "") or None
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--expect-cells") from None
    inputs = (_opt(expect_rubric_hash), _opt(expect_dataset_digest), cells)
    if inputs[0] is not None and not (inputs[0] == "none" or _HASH.fullmatch(inputs[0])):
        raise typer.BadParameter("--expect-rubric-hash must be sha256:<64 lowercase hex digits> or none")
    if inputs[1] is not None and not _HASH.fullmatch(inputs[1]):
        raise typer.BadParameter("--expect-dataset-digest must look like sha256:<64 lowercase hex digits>")
    from skillhub.registry.versions import PUBLISH_TRIGGER_SOURCES

    source = _opt(trigger_source) or "merge"
    if source not in PUBLISH_TRIGGER_SOURCES:
        raise typer.BadParameter("use merge or pr", param_hint="--trigger-source")
    args = (
        ctx,
        action,
        dept,
        skill,
        version,
        git_sha,
        eval_run_id,
        actor,
        job_run_id,
        expected,
        run_key,
        inputs,
        source,
    )
    if root is None:
        _job_switch(*args)
        return
    if not Path(root).is_dir():
        raise typer.BadParameter(f"--skills-root {root} is not a directory")
    with _skills_root_env(root):
        _job_switch(*args, explicit_root=True)


@contextmanager
def _skills_root_env(root: str) -> Iterator[None]:
    """Point $SKILLHUB_SKILLS at `root` for the duration of one command, then restore it."""
    import os

    old = os.environ.get("SKILLHUB_SKILLS")
    os.environ["SKILLHUB_SKILLS"] = root
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SKILLHUB_SKILLS", None)
        else:
            os.environ["SKILLHUB_SKILLS"] = old


_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def _published_hash(journal, dept: str, skill: str, version: int) -> str | None:
    for row in journal.history(dept, skill):
        if row.action == "published" and row.version == version:
            return row.content_hash
    return None


def _job_switch(
    ctx,
    action,
    dept,
    skill,
    version,
    git_sha,
    eval_run_id,
    actor,
    job_run_id,
    expect_hash,
    run_key="",
    inputs=(None, None, None),
    trigger_source="merge",
    explicit_root=False,
) -> None:
    import json
    from dataclasses import asdict

    from skillhub.bundle import content_hash
    from skillhub.registry.switch import retire, switch
    from skillhub.registry.versions import check_provenance, publish

    if action not in ("publish", "switch", "retire"):
        raise typer.BadParameter("--action must be publish, switch or retire")
    cfg = _cfg(ctx)
    _check_skill_ref(cfg, dept, skill)  # the Job runs as ci: nothing unchecked reaches SQL or a path
    run_id = _opt(job_run_id)
    expected = _opt(expect_hash)
    with _guard():
        w, uc, sql, journal = _services(cfg)
        if action == "publish":
            _refuse_quarantined(cfg, dept, skill)  # before the bundle, the gate or any UC write
            bundle = _bundle(dept, skill)
            actual = content_hash(bundle.files)
            if expected and actual != expected:  # before any UC write
                raise SkillHubError(
                    f"refusing to publish {dept}/{skill}: content hash {actual} is not the expected {expected}"
                )
            check_provenance(git_sha or "", eval_run_id or "")
            _require_ci_publisher(
                w,
                journal,
                bundle,
                actual,
                git_sha=git_sha,
                eval_run_id=eval_run_id,
                actor=actor,
                job_run_id=run_id,
            )
            _require_evaluated(
                cfg, w, sql, journal, bundle, actual,
                run_key=run_key, git_sha=git_sha, eval_run_id=eval_run_id, actor=actor, job_run_id=run_id,
                rubric_hash=inputs[0], dataset_digest=inputs[1], cells=inputs[2],
                trigger_source=trigger_source,
            )  # fmt: skip
            res = publish(
                cfg,
                uc,
                journal,
                bundle,
                git_sha=git_sha or "",  # publish raises MissingProvenance when empty
                eval_run_id=eval_run_id or "",
                actor=actor,
                job_run_id=run_id,
            )
            _ensure_skill_experiment(cfg, dept, skill)
            out = {"status": "published" if res.created else "unchanged", **asdict(res)}
        elif action == "switch":
            if not _opt(version):
                raise typer.BadParameter("switch needs --version (a number or 'latest')")
            target = _resolve_version(journal, dept, skill, version.strip(), strict=explicit_root)
            if expected and _published_hash(journal, dept, skill, target) != expected:
                raise SkillHubError(
                    f"refusing to switch {dept}/{skill} to version {target}: "
                    f"its content hash is not the expected {expected}"
                )
            out = asdict(switch(cfg, uc, journal, dept, skill, target, actor=actor, job_run_id=run_id))
        else:
            status = retire(cfg, uc, journal, dept, skill, actor=actor, job_run_id=run_id)
            out = {"status": status, "dept": dept, "skill": skill}
    typer.echo(json.dumps(out, indent=2))
    if action == "switch" and out["status"] not in ("switched", "noop"):
        raise typer.Exit(1)


def _check_skill_ref(cfg, dept: str, skill: str, hint: str = "--skill") -> None:
    """A department from hub.yaml and a valid skill name, before either reaches SQL, a REST path or a file
    path. Exit 2 otherwise."""
    from skillhub.naming import check_skill_name

    if dept not in cfg.departments:
        raise typer.BadParameter(f"unknown department {dept!r}", param_hint="--dept")
    try:
        check_skill_name(skill)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint=hint) from None


def _parse_versions(value: str) -> dict[str, int]:
    """Parse 'skill=3,other=1'. "" means none."""
    out: dict[str, int] = {}
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, num = item.partition("=")
        if not sep or not name.strip() or not num.strip().isdecimal() or int(num) < 1:
            raise typer.BadParameter(f"--versions entries look like skill=3, got {item!r}")
        out[name.strip()] = int(num)
    return out


@app.command("stage")
def stage_cmd(
    ctx: typer.Context,
    run_key: str = typer.Option(..., "--run-key"),
    skills: str = typer.Option("", "--skills", help="Comma-separated skill folder names. Empty means none."),
    versions: str = typer.Option("", "--versions", help="Comma-separated skill=N pairs. Empty means none."),
) -> None:
    """Create run schemas holding the skills under test. Prints the scopes as JSON."""
    import json
    from dataclasses import asdict

    from skillhub.bundle import discover_bundles
    from skillhub.naming import make_run_key
    from skillhub.registry.stage import stage

    cfg = _cfg(ctx)
    wanted = [s.strip() for s in skills.split(",") if s.strip()]
    pinned = _parse_versions(versions)
    if not wanted and not pinned:
        raise typer.BadParameter("give --skills, --versions or both")
    found: dict[str, list] = {}
    for b in discover_bundles():  # honours SKILLHUB_SKILLS
        found.setdefault(b.name, []).append(b)
    for name in wanted:
        _check_skill_ref(cfg, "common", name, "--skills")  # the name only; folders give the department
    pins: dict[str, int] = {}
    for ref, n in pinned.items():  # staging resolves a pin only in its own department
        dept, _, name = ref.rpartition("/")
        if not dept:
            if len(found.get(name, [])) != 1:
                raise typer.BadParameter(
                    f"--versions {name}: no single skill folder names its department; use dept/name"
                )
            dept = found[name][0].dept
        _check_skill_ref(cfg, dept, name, "--versions")
        pins[f"{dept}/{name}"] = n
    missing = [s for s in wanted if s not in found]
    if missing:
        raise typer.BadParameter(f"no skill folder for: {', '.join(missing)}")
    ambiguous = [s for s in wanted if len(found[s]) > 1]
    if ambiguous:
        raise typer.BadParameter(f"skill name exists in several departments: {', '.join(ambiguous)}")
    with _guard():
        _, uc, _, journal = _services(cfg)
        scopes = stage(
            cfg,
            uc,
            journal,
            make_run_key(run_key),
            [found[s][0] for s in wanted],
            versions=pins or None,
        )
    typer.echo(json.dumps([asdict(s) for s in scopes], indent=2))


@app.command("cleanup")
def cleanup_cmd(
    ctx: typer.Context,
    older_than_hours: str = typer.Option("24", "--older-than-hours", help="Empty means 24."),
) -> None:
    """Drop run_* schemas and delete candidate folders past their age limit."""
    from skillhub.registry.stage import cleanup_stale

    hours = (older_than_hours or "").strip() or "24"
    if not hours.isdecimal() or int(hours) < 1:
        raise typer.BadParameter("--older-than-hours must be a positive whole number")
    cfg = _cfg(ctx)
    with _guard():
        w = workspace_client()
        for name in cleanup_stale(cfg, w, int(hours)):
            typer.echo(f"dropped {name}")


@app.command("bootstrap")
def bootstrap_cmd(
    ctx: typer.Context,
    create_identities: bool = typer.Option(False, "--create-identities/--no-create-identities"),
    secret_out: str = typer.Option(
        "",
        "--secret-out",
        help="File to write a new CI OAuth secret to (mode 0600, must not exist). Required when a secret "
        "is created. Then run `gh secret set DATABRICKS_CLIENT_SECRET < PATH` and delete the file.",
    ),
    eval_secret_out: str = typer.Option(
        "",
        "--eval-secret-out",
        help="File to write a new CI eval OAuth secret to (mode 0600, must not exist). Required when that "
        "secret is created. Then run `gh secret set DATABRICKS_CLIENT_SECRET --env ci-eval < PATH` and "
        "delete the file.",
    ),
    list_identities: bool = typer.Option(
        False, "--print-identities", help="List the groups and service principals to create, then exit."
    ),
) -> None:
    """Create schemas, grants, results artefacts, and (with the flag) account groups and the CI principals.

    The secret is never printed: the operator moves it into GitHub with
    `gh secret set DATABRICKS_CLIENT_SECRET < PATH` (and the eval secret into the `ci-eval`
    environment) and then deletes the file.
    """
    from skillhub.registry.bootstrap import SecretOutError, bootstrap, print_identities

    cfg = _cfg(ctx)
    if list_identities:
        typer.echo(print_identities(cfg))
        return
    out = _opt(secret_out)  # "" means unset
    eval_out = _opt(eval_secret_out)
    with _guard():
        try:
            w, _, sql, _ = _services(cfg)
            bootstrap(
                cfg,
                w,
                sql,
                create_identities=create_identities,
                secret_out=Path(out) if out else None,
                eval_secret_out=Path(eval_out) if eval_out else None,
                emit=typer.echo,  # each line as it happens, so a later failure cannot hide a written secret file
            )
        except SecretOutError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(2) from e
    typer.echo("bootstrap complete")


@app.command("generate")
def generate_cmd(
    ctx: typer.Context,
    skills: str = typer.Option(
        "",
        "--skills",
        help="Comma-separated `dept/name` or bare `name`. Empty means unset. Without --force, "
        "each run adds a new batch of trigger cases.",
    ),
    missing: bool = typer.Option(False, "--missing", help="Skills without trigger cases or rubric.json."),
    force: bool = typer.Option(
        False, "--force", help="Replace generated trigger cases and the rubric. Human cases are kept."
    ),
    rubric_only: bool = typer.Option(
        False, "--rubric-only", help="Write only a missing (or forced) rubric.json; never touch evals.json."
    ),
) -> None:
    """Generate evals/evals.json trigger cases and evals/rubric.json."""
    from skillhub.bundle import discover_bundles
    from skillhub.evalspec import Criteria, EvalFile, load_evals, save_evals, save_rubric
    from skillhub.generate.rubric import generate_rubric
    from skillhub.generate.tests import GenerationError, generate_trigger_cases, sibling_bundles

    EFFECTIVENESS_FIRST_ID = 101  # trigger ids stay below this

    wanted = _opt(skills)
    if bool(wanted) == missing:
        raise typer.BadParameter("pass exactly one of --skills or --missing")
    cfg = _cfg(ctx)
    all_bundles = discover_bundles()
    if wanted:
        targets = _select_skills(all_bundles, wanted)
    else:
        targets = [
            b
            for b in all_bundles
            if (not rubric_only and _lacks_trigger_cases(b))
            or not (b.root / "evals" / "rubric.json").exists()
        ]
    failures: list[str] = []
    with _guard():
        model = _generation_model(cfg)
        for b in targets:
            evals_path = b.root / "evals" / "evals.json"
            rubric_path = b.root / "evals" / "rubric.json"
            label = f"{b.dept}/{b.name}"
            try:
                existing = load_evals(evals_path) if evals_path.exists() else None
                do_cases = not rubric_only and (not missing or _lacks_trigger_cases(b))
                do_rubric = force or not rubric_path.exists()
                # Generate everything for this skill first, so a failure writes nothing for it.
                new_cases, kept = [], []
                if do_cases:
                    kept = [
                        c
                        for c in (existing.evals if existing else [])
                        if not (force and c.kind == "trigger" and c.origin == "generated")
                    ]
                    start = max((c.id for c in kept if c.kind == "trigger"), default=0) + 1
                    new_cases = generate_trigger_cases(
                        cfg, b, sibling_bundles(all_bundles, b), model, start_id=start
                    )
                    kept_ids = {c.id for c in kept}
                    clash = sorted(
                        c.id for c in new_cases if c.id in kept_ids or c.id >= EFFECTIVENESS_FIRST_ID
                    )
                    if clash:
                        raise GenerationError(
                            f"new trigger case ids {clash} collide with existing ids or reach the "
                            f"effectiveness range ({EFFECTIVENESS_FIRST_ID}+)"
                        )
                rubric = generate_rubric(cfg, b, model) if do_rubric else None
            except Exception as e:  # one bad skill (output, evals.json, transport) must not stop the run
                typer.echo(f"error: {label}: {e}", err=True)
                failures.append(label)
                continue
            if do_cases:
                evals_path.parent.mkdir(parents=True, exist_ok=True)
                save_evals(
                    evals_path,
                    EvalFile(
                        skill_name=b.name,
                        evals=kept + new_cases,
                        criteria=existing.criteria if existing else Criteria(),
                        models=existing.models if existing else None,
                    ),
                )
            if rubric is not None:
                rubric_path.parent.mkdir(parents=True, exist_ok=True)
                save_rubric(rubric_path, rubric)
            typer.echo(
                f"{label}: {len(new_cases)} generated trigger cases, rubric {'written' if rubric else 'kept'}"
            )
    if failures:
        typer.echo(f"{len(failures)} skill(s) failed: {', '.join(failures)}", err=True)
        raise typer.Exit(1)


def _select_skills(bundles, spec: str):
    """Resolve `dept/name` and bare `name` entries. Unknown or ambiguous (several departments) exits 2."""
    from skillhub.bundle import SkillRefError, select_bundles

    refs = [e.strip() for e in spec.split(",") if e.strip()]
    if not refs:
        raise typer.BadParameter("--skills names no skill")
    try:
        return select_bundles(bundles, refs)
    except SkillRefError as e:
        raise typer.BadParameter(str(e)) from None


def _lacks_trigger_cases(bundle) -> bool:
    from skillhub.evalspec import load_evals

    path = bundle.root / "evals" / "evals.json"
    if not path.exists():
        return True
    return not any(c.kind == "trigger" for c in load_evals(path).evals)


def _generation_model(cfg):
    from skillhub.auth import auth_headers
    from skillhub.gateway.models import model_client

    return model_client(cfg, cfg.generator, auth_headers)


# --- Track commands are added below. Each command wraps domain calls in `with _guard():`. ---


def _csv(value: str) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


@app.command("eval")
def eval_cmd(
    ctx: typer.Context,
    run_key: str = typer.Option(..., "--run-key", help="Run label, normalised by make_run_key."),
    skills: str = typer.Option(
        ..., "--skills", help="`all`, or comma-separated `dept/name` or bare `name` entries."
    ),
    models: str = typer.Option(
        ...,
        "--models",
        help="`default` (a skill's evals.json `models`, else defaults.models) or comma-separated profiles.",
    ),
    modes: str = typer.Option(..., "--modes", help="Comma-separated: trigger, effectiveness."),
    source: str = typer.Option(..., "--source", help="git:<sha>, draft:<id> or live. Empty means live."),
    trigger_source: str = typer.Option(
        ..., "--trigger-source", help="pr, merge, nightly, hub, hub-try or manual."
    ),
    requested_by: str = typer.Option("", "--requested-by", help="Empty means unset."),
    versions: str = typer.Option(
        "", "--versions", help="Comma-separated skill=N, skill as `dept/name` or `name`. Empty: none."
    ),
    skills_root: str = typer.Option(
        "", "--skills-root", help="Candidate skills folder. Empty means $SKILLHUB_SKILLS or ./skills."
    ),
    fixtures_root: str = typer.Option(
        "", "--fixtures-root", help="Fixtures folder. Empty means $SKILLHUB_FIXTURES or ./fixtures."
    ),
) -> None:
    """Evaluate skills x models x modes and print the RunSummary as JSON.

    Jobs pass "" for every unset parameter. Bad values exit 2 before anything is staged. Exit 1 when
    any skill or child run could not be evaluated (`errors` in the JSON); its child runs are `inconclusive`.
    """
    import dataclasses
    import json
    import os
    import sys
    from contextlib import redirect_stdout

    from skillhub.bundle import discover_bundles
    from skillhub.eval.runner import REQUEST_MODES, TRIGGER_SOURCES, RunRequest, run_eval
    from skillhub.naming import make_run_key

    try:
        key = make_run_key(run_key)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--run-key") from e
    mode_list = _csv(modes)
    if not mode_list or any(m not in REQUEST_MODES for m in mode_list):
        raise typer.BadParameter("use trigger and/or effectiveness", param_hint="--modes")
    root = _opt(skills_root)
    if root and not _opt(source):
        raise typer.BadParameter("--skills-root holds candidates: give --source git:<sha> or draft:<id>")
    src = _opt(source) or "live"
    if not (src == "live" or src.startswith(("git:", "draft:"))):
        raise typer.BadParameter("use git:<sha>, draft:<id> or live", param_hint="--source")
    if trigger_source not in TRIGGER_SOURCES:
        raise typer.BadParameter(f"use one of {', '.join(TRIGGER_SOURCES)}", param_hint="--trigger-source")
    cfg = _cfg(ctx)
    # [] lets each skill's evals.json `models` override defaults.models
    model_list = [] if (_opt(models) or "default") == "default" else _csv(models)
    unknown = [m for m in model_list if m not in cfg.models]
    if unknown:
        raise typer.BadParameter(f"unknown model profile(s): {', '.join(unknown)}", param_hint="--models")
    bundles = discover_bundles(Path(root) if root else None)
    wanted = _opt(skills)
    if wanted == "all":
        chosen = bundles
        if not chosen:
            raise typer.BadParameter("no skills found", param_hint="--skills")
    else:
        chosen = _select_skills(bundles, wanted or "")  # unknown or ambiguous names exit 2
    pinned: dict[str, int] = {}
    for ref, n in _parse_versions(versions).items():
        try:
            [b] = _select_skills(chosen, ref)
        except typer.BadParameter:
            raise typer.BadParameter(
                f"{ref!r} is not one of the selected skills", param_hint="--versions"
            ) from None
        pinned[f"{b.dept}/{b.name}"] = n  # staging resolves each pin in its own department
    req = RunRequest(
        run_key=key,
        skills=[f"{b.dept}/{b.name}" for b in chosen],  # dept/name: never another department's copy
        models=list(model_list),
        modes=mode_list,
        source=src,
        trigger_source=trigger_source,
        requested_by=_opt(requested_by),
        versions=pinned or None,
        skills_root=root,
        fixtures_root=_opt(fixtures_root),
    )
    os.environ.setdefault("MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT", "true")  # stdout is the JSON only
    with _guard(), redirect_stdout(sys.stderr):
        summary = run_eval(cfg, req)
    typer.echo(json.dumps(dataclasses.asdict(summary), default=str))
    for e in summary.errors:
        where = " ".join(x for x in (e.model_profile, e.mode) if x)
        typer.echo(f"error: {e.dept}/{e.skill}{' ' + where if where else ''}: {e.message}", err=True)
    if summary.errors:
        raise typer.Exit(1)


@app.command("changed-skills")
def changed_skills_cmd(
    base: str = typer.Option("", "--base", help="Base ref. Give it or --empty-base."),
    head: str = typer.Option(..., "--head"),
    empty_base: bool = typer.Option(
        False, "--empty-base", help="Diff from nothing (no release yet): every skill at --head is added."
    ),
) -> None:
    """Print changed skills as JSON: [{dept, skill, status, needs_cases, needs_rubric, moved_from_quarantine,
    quarantined, needs_generation}]."""
    import json

    from skillhub.ci.changes import changed_skills

    if bool(_opt(base)) == empty_base:
        raise typer.BadParameter("pass exactly one of --base or --empty-base")
    with _guard():
        rows = [c.as_dict() for c in changed_skills(None if empty_base else base, head)]
    typer.echo(json.dumps(rows, indent=2))


@app.command("pr-comment")
def pr_comment_cmd(
    ctx: typer.Context,
    run_key: str = typer.Option(..., "--run-key", help="Run label, for example pr12-ab12cd3-a1."),
    pr: int = typer.Option(..., "--pr"),
) -> None:
    """Upsert the sticky PR comment for a run. Exit 1 on fail, inconclusive or no results.

    The comment holds no workspace host, run or experiment ID and no Skill Hub URL unless
    SKILLHUB_PR_LINKS=true: the repository can be public.
    """
    import os

    import httpx

    from skillhub.auth import workspace_client
    from skillhub.ci.pr_comment import (
        add_run_links,
        blocking_verdicts,
        exit_code,
        load_matrix_rows,
        render_matrix_markdown,
        upsert_pr_comment,
    )
    from skillhub.naming import make_run_key
    from skillhub.results.experiments import trusted_run_authors
    from skillhub.sql import SqlClient

    cfg = _cfg(ctx)
    links = os.environ.get("SKILLHUB_PR_LINKS", "").strip() == "true"
    with _guard():
        # Read the environment first so a KeyError below can only mean a missing env var.
        try:
            repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
        except KeyError as e:
            raise SkillHubError(f"environment variable {e.args[0]} is not set") from None
        try:
            key = make_run_key(run_key)
            w = workspace_client()
            sql = SqlClient(w, cfg.workspace.sql_warehouse_id)
            rows = load_matrix_rows(sql.execute, cfg, key, trusted=trusted_run_authors(w))
            if links:
                add_run_links(rows, cfg.workspace.host)
            hub_url = (os.environ.get("SKILL_HUB_URL") or None) if links else None
            body = render_matrix_markdown(
                rows, run_key=key, links=links, hub_url=hub_url, gating_modes=cfg.defaults.gating_modes
            )
            action = upsert_pr_comment(repo=repo, pr=pr, body=body, token=token)
        except ValueError as e:
            raise SkillHubError(str(e)) from None
        except httpx.HTTPError as e:
            # Report the status only: never echo request headers (they carry the token).
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
            raise SkillHubError(f"GitHub API call failed ({status})") from None
    typer.echo(f"pr comment {action}")
    raise typer.Exit(exit_code(blocking_verdicts(rows, cfg.defaults.gating_modes)))


@app.command("check-verdict")
def check_verdict_cmd(
    ctx: typer.Context,
    run_key: str = typer.Option(..., "--run-key", help="Run label or run key."),
    skills: str = typer.Option(
        "", "--skills", help="dept/skill list that must each have a result. Empty: no check."
    ),
    expect_hash: list[str] = typer.Option(
        [],
        "--expect-hash",
        help="dept/skill=sha256:<hex>, repeatable. Every gating row of that skill must have evaluated this "
        "content, and the skill must have a result.",
    ),
    expect_file: str = typer.Option(
        "",
        "--expect-file",
        help="JSON from `skillhub eval-digests`. Every planned gating cell of each listed skill needs a row "
        ", and every gating row must have evaluated its content, rubric and cases with fixtures. "
        "Empty: none.",
    ),
) -> None:
    """Merge gate. Exit 1 when the verdict is fail or inconclusive, a listed
    skill or a planned gating cell has no result, or a gating row evaluated other content or other eval inputs
    than expected. Reads only rows by trusted run authors."""
    from skillhub.auth import workspace_client
    from skillhub.ci.pr_comment import (
        Expected,
        blocking_verdicts,
        gate_failures,
        load_matrix_rows,
        parse_expect_hashes,
        parse_expectations,
    )
    from skillhub.naming import make_run_key
    from skillhub.results.experiments import trusted_run_authors
    from skillhub.sql import SqlClient

    try:
        expected = {ref: Expected(h) for ref, h in parse_expect_hashes(expect_hash).items()}
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--expect-hash") from None
    if _opt(expect_file):
        try:
            full = parse_expectations(Path(expect_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise typer.BadParameter(str(e), param_hint="--expect-file") from None
        clash = sorted(r for r in full if r in expected and expected[r].content_hash != full[r].content_hash)
        if clash:
            raise typer.BadParameter(f"--expect-hash and --expect-file disagree for {', '.join(clash)}")
        expected |= full
    cfg = _cfg(ctx)
    with _guard():
        try:
            key = make_run_key(run_key)
            w = workspace_client()
            sql = SqlClient(w, cfg.workspace.sql_warehouse_id)
            rows = load_matrix_rows(sql.execute, cfg, key, trusted=trusted_run_authors(w))
        except ValueError as e:
            raise SkillHubError(str(e)) from None
    verdicts = blocking_verdicts(rows, cfg.defaults.gating_modes)
    typer.echo(f"verdicts: {sorted(set(verdicts)) or 'none'}")
    failures = gate_failures(rows, expected, _csv(skills), cfg.defaults.gating_modes)
    for line in failures:
        typer.echo(line, err=True)
    raise typer.Exit(1 if failures else 0)


@app.command("reuse-pr-gate")
def reuse_pr_gate_cmd(
    ctx: typer.Context,
    merge_sha: str = typer.Option(..., "--merge-sha", help="The commit pushed to main."),
    skills: str = typer.Option(..., "--skills", help="The merge plan's dept/skill list to evaluate."),
    expect_file: str = typer.Option(
        ..., "--expect-file", help="The merge plan's `eval-digests` JSON, one entry per listed skill."
    ),
) -> None:
    """Decide whether a release reuses the pull request gate's eval instead of evaluating the merged content again.
    Prints JSON: {reuse, run_key, reason}.

    Reuse needs all of: the commit is a two-parent merge whose tree is its second parent's (the pull request head);
    every path the pull request changed is under skills/; defaults.gating_modes holds trigger only; and a trusted
    pull request gate run for that head (trigger source pr) passes check-verdict's check against the merge plan.
    The newest such run is reused. Otherwise `reason` names the first rule that failed. Exit 0 with a decision;
    any error exits non-zero, and CI then evaluates the merged content."""
    from skillhub.auth import workspace_client
    from skillhub.ci import reuse
    from skillhub.ci.pr_comment import gate_failures, load_matrix_rows, parse_expectations
    from skillhub.results.experiments import trusted_run_authors
    from skillhub.sql import SqlClient

    try:
        expected = parse_expectations(Path(expect_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise typer.BadParameter(str(e), param_hint="--expect-file") from None
    listed = _csv(skills)
    if not listed or set(listed) != set(expected) or not all(e.cells for e in expected.values()):
        raise typer.BadParameter(
            "the plan must list each skill once, with its planned cells", param_hint="--expect-file"
        )
    cfg = _cfg(ctx)
    db: dict = {}

    def connect():  # only once the git and config rules pass
        if not db:
            w = workspace_client()
            db["fetch"] = SqlClient(w, cfg.workspace.sql_warehouse_id).execute
            db["trusted"] = trusted_run_authors(w)
        return db["fetch"], db["trusted"]

    def run_keys(head: str) -> list[str]:
        fetch, trusted = connect()
        return reuse.gate_run_keys(fetch, cfg, head, trusted=trusted)

    def failures(key: str) -> list[str]:
        fetch, trusted = connect()
        rows = load_matrix_rows(fetch, cfg, key, trusted=trusted, trigger_source=reuse.PR_TRIGGER_SOURCE)
        return gate_failures(rows, expected, listed, cfg.defaults.gating_modes)

    with _guard():
        facts = reuse.merge_facts(merge_sha)
    try:
        decision = reuse.decide(facts, cfg.defaults.gating_modes, run_keys, failures)
    except Exception as e:  # noqa: BLE001 - SQL or auth failure: no decision, so CI re-evaluates
        typer.echo(f"error: cannot check the pull request gate ({type(e).__name__})", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(asdict(decision)))


@app.command("eval-digests")
def eval_digests_cmd(
    ctx: typer.Context,
    skills: str = typer.Option(..., "--skills", help="Comma-separated dept/skill folders. Empty: none."),
    skills_root: str = typer.Option("", "--skills-root", help="Empty means $SKILLHUB_SKILLS or ./skills."),
    fixtures_root: str = typer.Option(
        "", "--fixtures-root", help="Empty means $SKILLHUB_FIXTURES or ./fixtures."
    ),
    models: str = typer.Option(
        "default", "--models", help="As for `eval`: default or comma-separated profiles."
    ),
    modes: str = typer.Option("trigger,effectiveness", "--modes", help="As for `eval`."),
) -> None:
    """Print what the gates expect of each skill's eval, as JSON: [{dept, skill, hash, rubric_hash,
    dataset_digest, cells}]. `cells` lists every gating model/mode the eval must produce, resolved
    as the runner resolves them (evals.json `models`, else hub.yaml defaults.models). Needs no workspace: CI
    runs it on the checkout, and the eval runner computes the same values from what it loads."""
    import os

    from skillhub.bundle import BundleError, check_names, content_hash, load_bundle
    from skillhub.bundle import skills_root as default_skills_root
    from skillhub.config import load_lint_settings
    from skillhub.evalspec import REQUEST_MODES, eval_digests, planned_cells, read_evals

    mode_list = _csv(modes)
    if not mode_list or any(m not in REQUEST_MODES for m in mode_list):
        raise typer.BadParameter("use trigger and/or effectiveness", param_hint="--modes")
    model_list = [] if (_opt(models) or "default") == "default" else _csv(models)
    with _guard():
        try:
            defaults = load_lint_settings(ctx.obj.get("config_path")).defaults  # no ${VARS} needed
        except OSError as e:
            raise SkillHubError(f"cannot read hub.yaml: {e.strerror}") from None

    root = Path(_opt(skills_root) or default_skills_root())
    fixtures = Path(_opt(fixtures_root) or os.environ.get("SKILLHUB_FIXTURES", "fixtures"))
    out = []
    for ref in _csv(skills):
        dept, sep, name = ref.partition("/")
        if not sep:
            raise typer.BadParameter(f"{ref!r}: use dept/skill", param_hint="--skills")
        try:
            check_names(dept, name)
            bundle = load_bundle(root / dept / name, dept)
            digests = eval_digests(bundle.root, fixtures)
            ef, _ = read_evals(bundle.root / "evals" / "evals.json")
        except (BundleError, OSError, ValueError) as e:
            typer.echo(f"error: {ref}: {e}", err=True)
            raise typer.Exit(1) from None
        out.append(
            {
                "dept": dept,
                "skill": name,
                "hash": content_hash(bundle.files),
                "rubric_hash": digests.rubric_hash,
                "dataset_digest": digests.dataset_digest,
                "cells": planned_cells(
                    ef, request_models=model_list, default_models=defaults.models, modes=mode_list
                ),
            }
        )
    typer.echo(json.dumps(out, separators=(",", ":")))


def job_main() -> None:
    """Entry point for Databricks Jobs (`skillhub-job`).

    Serverless wheel tasks run under IPython and report a SystemExit as a failed task, even SystemExit(0). So this
    returns normally on success and raises SystemExit with a non-zero code only when the command fails.
    """
    try:
        code = app(standalone_mode=False)
    except SystemExit as e:  # --help and click's own exits
        code = e.code
    except typer.Exit as e:
        code = e.exit_code
    except typer.Abort:
        code = 1
    except Exception as e:  # click usage errors: Typer bundles its own click, so match by behaviour
        if not (callable(getattr(e, "show", None)) and isinstance(getattr(e, "exit_code", None), int)):
            raise
        e.show()
        code = e.exit_code
    if code not in (None, 0):
        raise SystemExit(code)
