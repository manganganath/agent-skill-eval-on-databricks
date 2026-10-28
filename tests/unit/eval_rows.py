"""Synced `eval_runs` child rows for tests of the publish gate. Not a test module.

The publish gate reuses the merge gate's readers: `ci.pr_comment.load_matrix_rows(..., trusted=)`,
`hash_mismatches` and `blocking_verdicts`, and `results.experiments.trusted_run_authors`. The tests run the
real ones. Only the SQL is faked: FakeSql holds `eval_runs` tables built from the real tables.sql and runs the
reader's real SELECT (it supports `SELECT * EXCEPT`).
"""

from __future__ import annotations

from types import SimpleNamespace

from skillhub.bundle import content_hash
from skillhub.naming import evals_schema
from skillhub.results.tables import TABLES_SQL, split_statements

TRUSTED = "ci-eval-app"  # the ci_eval application ID that ran the evals
CALLER = "deployer@example.com"  # the identity the CLI runs as in these tests
PASSING = (("trigger", "pass"), ("with_skill", "pass"), ("without_skill", "fail"))
RUBRIC = "sha256:" + "b" * 64  # the eval inputs the rows record unless a test says otherwise
DATASET = "sha256:" + "d" * 64
CELLS = ("gpt/trigger", "gpt/with_skill")  # the gating cells of PASSING

_EVAL_RUNS_DDL = next(s for s in split_statements(TABLES_SQL) if ".eval_runs (" in s)


def ensure_eval_runs(sql, cfg) -> None:
    """The real eval_runs table in every department: the reader's SELECT reads all of them."""
    for dept in cfg.departments:
        sql.execute(_EVAL_RUNS_DDL.format(evals=evals_schema(cfg, dept)))


def trust_ci_eval(monkeypatch, caller: str = CALLER) -> None:
    """Inputs of the real `trusted_run_authors`: the ci_eval principal from the environment, and the caller's
    identity. The CLI tests pass no workspace client, so the identity comes from a stub client."""
    from skillhub import auth
    from skillhub.results import experiments

    monkeypatch.setenv(experiments.CI_EVAL_ENV, TRUSTED)
    monkeypatch.delenv(experiments.APP_ENV, raising=False)
    monkeypatch.delenv(experiments.DEPLOYER_ENV, raising=False)
    me = SimpleNamespace(user_name=caller)
    monkeypatch.setattr(
        auth, "workspace_client", lambda: SimpleNamespace(current_user=SimpleNamespace(me=lambda: me))
    )


def add_rows(
    sql,
    cfg,
    dept: str,
    skill: str,
    run_key: str,
    hash_: str,
    cells=PASSING,
    *,
    model: str = "gpt",
    started_at: str = "2026-10-28T10:00:00",
    author: str = TRUSTED,
    rubric: str = RUBRIC,
    dataset: str = DATASET,
) -> None:
    """One child row per (mode, verdict) cell, plus the parent row the runner writes (mode NULL)."""
    ensure_eval_runs(sql, cfg)
    t = f"{evals_schema(cfg, dept)}.eval_runs"
    rows = [(None, None, None)] + [(f"parent-{run_key}", m, v) for m, v in cells]
    for i, (parent, mode, verdict) in enumerate(rows):
        sql.execute(
            f"INSERT INTO {t} (run_id, parent_run_id, run_key, dept, skill, content_hash, rubric_hash, "
            "dataset_digest, model_profile, harness, mode, verdict, started_at, synced_at, run_author) VALUES "
            "(:rid, :parent, :rk, :dept, :skill, :h, :rubric, :dataset, :model, 'reference', :mode, :verdict, "
            ":started, :started, :author)",
            {
                "rid": f"{run_key}-{model}-{started_at}-{author}-{i}",
                "parent": parent,
                "rk": run_key,
                "dept": dept,
                "skill": skill,
                "h": hash_,
                "rubric": rubric,
                "dataset": dataset,
                "model": model,
                "mode": mode,
                "verdict": verdict,
                "started": started_at,
                "author": author,
            },
        )


def evaluated(reg, bundle, run_key: str = "r", cells=PASSING, **kw):
    """Record a synced eval of exactly this bundle under `run_key`. Returns the bundle."""
    add_rows(reg.sql, reg.cfg, bundle.dept, bundle.name, run_key, content_hash(bundle.files), cells, **kw)
    return bundle


def expected_inputs(monkeypatch) -> None:
    """`skillhub publish` reads the eval inputs of the local checkout; here they are the rows' defaults."""
    from skillhub import cli
    from skillhub.evalspec import EvalDigests

    monkeypatch.setattr(cli, "_local_eval_digests", lambda bundle: EvalDigests(RUBRIC, DATASET))
    monkeypatch.setattr(cli, "_local_cells", lambda cfg, bundle: CELLS)


# job-switch publish: the merge plan's eval inputs and planned cells
INPUT_ARGS = (
    "--expect-rubric-hash",
    RUBRIC,
    "--expect-dataset-digest",
    DATASET,
    "--expect-cells",
    ",".join(CELLS),
)
