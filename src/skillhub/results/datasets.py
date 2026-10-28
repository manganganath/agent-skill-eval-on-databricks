from __future__ import annotations

from typing import Any

import mlflow.genai.datasets as ds_api
from mlflow.exceptions import MlflowException

from skillhub.config import HubConfig
from skillhub.evalspec import EvalCase, EvalFile
from skillhub.naming import dataset_table

# `expected_skill` when the case expects no skill. A null would be rejected, and leaving the key out
# would keep a stale value: merge_records merges expectations as {**existing, **new}.
NO_EXPECTED_SKILL = ""


def case_record(case: EvalCase, skill: str) -> dict[str, Any]:
    """One evaluation record in the eval row shape, plus fixtures and bug_scope.

    Null or empty expectation keys are left out (nulls are rejected), except `expected_skill`, which is
    always written (NO_EXPECTED_SKILL when no skill is expected) so that a merge replaces a stale label. The runner
    never takes labels from the dataset row; it rebuilds each record from evals.json with this function.
    """
    expected = case.resolved_expected(skill)
    values: dict[str, Any] = {
        "should_trigger": case.should_trigger,
        "expected_skill": NO_EXPECTED_SKILL if expected is None else expected,
        "guidelines": list(case.assertions) or None,
        "expected_tool_calls": case.expected_tool_calls,
        "fixtures": list(case.fixtures) or None,
        "bug_scope": case.bug_scope,
        "reviewed": case.reviewed,  # False stays in; only None is dropped
    }
    return {
        "inputs": {"prompt": case.prompt, "case_id": case.id},
        "expectations": {k: v for k, v in values.items() if v is not None},
        "tags": {"kind": case.kind, "origin": case.origin},
    }


def _get_or_create(name: str, experiment_id: str):
    try:
        return ds_api.get_dataset(name=name)
    except MlflowException as exc:
        if "RESOURCE_DOES_NOT_EXIST" not in str(exc) and "not found" not in str(exc).lower():
            raise
    return ds_api.create_dataset(name=name, experiment_id=experiment_id)


def sync_dataset(
    cfg: HubConfig, dept: str, skill: str, ef: EvalFile, experiment_id: str
) -> tuple[object, str]:
    """Write the cases to the UC-backed dataset and return (fresh dataset, digest).

    merge_records returns a stale handle, so the digest comes from a fresh get_dataset.
    """
    name = dataset_table(cfg, dept, skill)
    records = [case_record(c, skill) for c in ef.evals]
    handle = _get_or_create(name, experiment_id)
    if records:
        handle.merge_records(records)
    fresh = ds_api.get_dataset(name=name)
    return fresh, fresh.digest
