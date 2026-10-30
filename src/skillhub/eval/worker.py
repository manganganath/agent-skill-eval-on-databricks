"""Evaluate one skill in its own process: `python -m skillhub.eval.worker <spec.json> <out.json>`.

`run_eval` starts one worker per skill when `defaults.skill_workers` is above 1 (`runner._run_in_workers`).
The spec holds what the parent planned; the worker rebuilds that skill's `_run_skill` call and writes
`{"children": [...], "errors": [...]}` to out.json. The skill's errors are data, so the worker still exits 0.
An exception exits non-zero, and the parent records the end of the worker's stderr as the skill's error.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import signal
import sys
from contextlib import redirect_stdout
from pathlib import Path

import mlflow
import mlflow.openai

from skillhub.agent.fixtures import FixtureStore
from skillhub.config import HubConfig
from skillhub.eval import runner
from skillhub.registry.stage import RunScope, StagedSkill


def _rebuild(spec: dict) -> tuple[tuple, dict]:
    """The positional and keyword arguments of the parent's `_run_skill` call for this skill."""
    cfg = HubConfig.model_validate(spec["config"])
    req = runner.RunRequest(**spec["request"])
    dept, name = spec["dept"], spec["skill"]
    selected = runner._select(
        runner.discover_bundles(Path(req.skills_root) if req.skills_root else None), req.skills
    )
    plans, clients, _ = runner._plan(cfg, req, selected)  # the parent recorded the plan's errors
    plan = plans[0] if plans else None
    if (
        plan is None
        or plan.models != spec["models"]
        or hashlib.sha256(plan.evals_bytes).hexdigest() != spec["evals_sha256"]
    ):
        raise runner.EvalRunError(f"{dept}/{name}: evals.json changed while the run was in progress")
    plan.dataset_digest = spec["dataset_digest"]  # of the parent's fixture snapshot, which the agent reads
    scope = RunScope(**{**spec["scope"], "skills": [StagedSkill(**s) for s in spec["scope"]["skills"]]})
    skill = next(s for s in scope.skills if s.dept == dept and s.name == name)
    system_prompt, prompt_versions = spec["prompts"]
    kwargs = {
        "clients": clients,
        "judges": runner._judges(cfg, req),
        "exp_id": spec["exp_id"],
        "parent_id": spec["parent_id"],
        "prompts": (system_prompt, prompt_versions),
        "version": spec["version"],
        "fixtures": FixtureStore(spec["fixtures"]),
        "sql": runner.SqlClient(runner.workspace_client(), cfg.workspace.sql_warehouse_id),
        "index": spec["index"],
    }
    return (cfg, req, plan, scope, skill), kwargs


def main(argv: list[str] | None = None) -> int:
    spec_path, out_path = argv if argv is not None else sys.argv[1:]
    with runner._sigterm_raises(), redirect_stdout(sys.stderr):  # the parent never reads stdout
        args, kwargs = _rebuild(json.loads(Path(spec_path).read_text(encoding="utf-8")))
        runner.configure_mlflow(args[0])  # the parent's eval_experiment did this in its own process
        mlflow.openai.autolog()  # LLM spans in every trace
        children, errors = runner._run_skill(*args, **kwargs)
    result = {
        "children": [dataclasses.asdict(c) for c in children],
        "errors": [dataclasses.asdict(e) for e in errors],
    }
    Path(out_path).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent turns a Ctrl-C into one SIGTERM per worker
    sys.exit(main())
