import os

import mlflow
import pytest

from skillhub.config import load_config
from skillhub.evalspec import EvalCase, EvalFile
from skillhub.naming import dataset_table
from skillhub.results import datasets
from skillhub.results.experiments import configure_mlflow, ensure_experiment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


@pytest.fixture
def probe():
    cfg = load_config()
    configure_mlflow(cfg)
    dept = next(iter(cfg.departments))
    exp_id = ensure_experiment(cfg, dept, "it-dataset-probe")
    yield cfg, dept, exp_id
    try:
        from mlflow.genai.datasets import delete_dataset

        delete_dataset(name=dataset_table(cfg, dept, "it-dataset-probe"))
    finally:
        mlflow.delete_experiment(exp_id)


def test_sync_dataset_digest_changes_with_cases(probe):
    cfg, dept, exp_id = probe
    base = EvalFile(
        skill_name="it-dataset-probe",
        evals=[EvalCase(id=1, prompt="p1", should_trigger=True, expected_skill="it-dataset-probe")],
    )
    _, d1 = datasets.sync_dataset(cfg, dept, "it-dataset-probe", base, exp_id)
    more = base.model_copy(update={"evals": base.evals + [EvalCase(id=2, prompt="p2", should_trigger=False)]})
    _, d2 = datasets.sync_dataset(cfg, dept, "it-dataset-probe", more, exp_id)
    assert d1 != d2
    _, d3 = datasets.sync_dataset(cfg, dept, "it-dataset-probe", more, exp_id)
    assert d3 == d2  # merging the same records again doesn't move the digest
