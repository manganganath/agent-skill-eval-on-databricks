# src/skillhub/results/label.py
from __future__ import annotations

import mlflow
import mlflow.genai.label_schemas as label_schemas
import mlflow.genai.labeling as labeling
from mlflow.exceptions import MlflowException
from mlflow.genai.label_schemas import InputCategorical

from skillhub.config import HubConfig
from skillhub.results.experiments import configure_mlflow
from skillhub.results.sync import child_runs


def judge_assessment_names(traces: list) -> list[str]:
    names: set[str] = set()
    for t in traces:
        for a in t.info.assessments or []:
            if a.source is not None and a.source.source_type == "LLM_JUDGE":
                names.add(a.name)
    return sorted(names)


_CHUNK = 100


def _is_not_found(e: MlflowException) -> bool:
    msg = str(e)
    return "RESOURCE_DOES_NOT_EXIST" in msg or "not found" in msg.lower()


def _ensure_label_schema(name: str) -> None:
    try:
        label_schemas.get_label_schema(name)
        return
    except MlflowException as e:
        if not _is_not_found(e):
            raise
    label_schemas.create_label_schema(
        name,
        type="feedback",
        input=InputCategorical(options=["pass", "fail"]),
        instruction=f"Does the response meet the criterion behind '{name}'?",
        enable_comment=True,
        title=name,
    )


def create_labeling_session(cfg: HubConfig, run_key: str, *, assessment_names: list[str]) -> dict[str, str]:
    """Create Review App sessions over a run's traces.

    One session per skill experiment holding the run's child-run traces. Returns
    {experiment path: session URL}. Re-running reuses an existing `<run_key>-labels` session.
    """
    configure_mlflow(cfg)
    runs = child_runs(cfg, run_key)
    if not runs:
        raise ValueError(f"no child runs for run_key {run_key!r}")
    by_exp: dict[str, list] = {}
    for r in runs:
        by_exp.setdefault(r.info.experiment_id, []).append(r)

    # Same-experiment traces are linked, not copied, so spans are not needed to add them.
    found: dict[str, list] = {}
    for exp_id, exp_runs in by_exp.items():
        traces: list = []
        for r in exp_runs:
            traces += mlflow.search_traces(
                locations=[exp_id], run_id=r.info.run_id, return_type="list", include_spans=False
            )
        if traces:
            found[exp_id] = traces
    if not found:
        raise ValueError(f"no traces found for run_key {run_key!r}")
    names = list(assessment_names) or judge_assessment_names([t for ts in found.values() for t in ts])
    if not names:
        raise ValueError(f"no judge assessment names found on the traces of run_key {run_key!r}")

    session_name = f"{run_key}-labels"
    urls: dict[str, str] = {}
    for exp_id, traces in found.items():
        mlflow.set_experiment(experiment_id=exp_id)  # Review App calls resolve the active experiment
        path = mlflow.get_experiment(exp_id).name
        existing = next((s for s in labeling.get_labeling_sessions() if s.name == session_name), None)
        if existing is not None:
            urls[path] = existing.url
            continue
        for name in names:
            _ensure_label_schema(name)
        session = labeling.create_labeling_session(name=session_name, label_schemas=names)
        for i in range(0, len(traces), _CHUNK):
            session.add_traces(traces[i : i + _CHUNK])
        urls[path] = session.url
    return urls
