import copy
from types import SimpleNamespace as NS

from skillhub.evalspec import EvalCase, EvalFile
from skillhub.naming import dataset_table
from skillhub.results import datasets as ds_mod
from tests.fakes import make_cfg

TABLE = dataset_table(make_cfg(), "common", "earnings-preview")


class FakeDataset:
    """Shared server-side state; each handle snapshots the digest when it is made."""

    def __init__(self, name):
        self.name = name
        self.records = []
        self.digest = "d0"

    def merge_records(self, records):
        stale = copy.copy(self)  # like the real API: the returned handle predates the merge (D6)
        self.records += records
        self.digest = f"d{len(self.records)}"
        return stale


def fake_api(store, created_kwargs):
    def create_dataset(**kwargs):
        created_kwargs.append(kwargs)
        store[kwargs["name"]] = FakeDataset(kwargs["name"])
        return store[kwargs["name"]]

    def get_dataset(name=None, **_):
        if name not in store:
            from mlflow.exceptions import MlflowException

            raise MlflowException("RESOURCE_DOES_NOT_EXIST: no dataset")
        return store[name]

    return NS(create_dataset=create_dataset, get_dataset=get_dataset)


def ef_fixture():
    return EvalFile(
        skill_name="earnings-preview",
        evals=[
            EvalCase(id=1, prompt="Preview ACME Q3", should_trigger=True, origin="generated", reviewed=False),
            EvalCase(id=2, prompt="Weather in Colombo", should_trigger=False),
            EvalCase(
                id=3,
                prompt="Preview ACME",
                should_trigger=True,
                assertions=["mentions ACME"],
                kind="effectiveness",
                reviewed=True,
                origin="human",
            ),
            EvalCase(id=4, prompt="Preview Globex", should_trigger=False, expected_skill="model-update"),
        ],
    )


def test_null_expectation_keys_are_omitted():
    rec1 = ds_mod.case_record(ef_fixture().evals[0], "earnings-preview")
    assert rec1["expectations"] == {
        "should_trigger": True,
        "expected_skill": "earnings-preview",
        "reviewed": False,
    }
    rec2 = ds_mod.case_record(ef_fixture().evals[1], "earnings-preview")
    assert rec2["expectations"] == {"should_trigger": False, "expected_skill": "", "reviewed": True}
    rec3 = ds_mod.case_record(ef_fixture().evals[2], "earnings-preview")
    assert rec3["expectations"]["guidelines"] == ["mentions ACME"]
    assert rec3["expectations"]["reviewed"] is True
    rec4 = ds_mod.case_record(ef_fixture().evals[3], "earnings-preview")
    assert rec4["expectations"]["expected_skill"] == "model-update"  # near-miss sibling


def test_fixtures_and_bug_scope_carried_when_set_and_omitted_otherwise():
    case = EvalCase(
        id=5,
        prompt="Audit it",
        should_trigger=True,
        kind="effectiveness",
        fixtures=["model.xlsx"],
        bug_scope=["b1", "b2"],
    )
    exp = ds_mod.case_record(case, "audit-xls")["expectations"]
    assert exp["fixtures"] == ["model.xlsx"]
    assert exp["bug_scope"] == ["b1", "b2"]
    plain = ds_mod.case_record(EvalCase(id=6, prompt="x", should_trigger=True), "audit-xls")["expectations"]
    assert "fixtures" not in plain and "bug_scope" not in plain


def test_expected_tool_calls_carried_when_set():
    case = EvalCase(id=7, prompt="x", should_trigger=True, expected_tool_calls=[{"name": "Read"}])
    assert ds_mod.case_record(case, "s")["expectations"]["expected_tool_calls"] == [{"name": "Read"}]


def test_record_fields_and_tags():
    rec = ds_mod.case_record(ef_fixture().evals[2], "earnings-preview")
    assert set(rec) == {"inputs", "expectations", "tags"}
    assert rec["inputs"] == {"prompt": "Preview ACME", "case_id": 3}
    assert rec["tags"] == {"kind": "effectiveness", "origin": "human"}


def test_creates_without_tags_argument_when_missing(monkeypatch):
    store, created = {}, []
    monkeypatch.setattr(ds_mod, "ds_api", fake_api(store, created))
    ds_mod.sync_dataset(make_cfg(), "common", "earnings-preview", ef_fixture(), "42")
    assert created == [{"name": TABLE, "experiment_id": "42"}]


def test_returns_digest_of_refetched_dataset(monkeypatch):
    store = {}
    api = fake_api(store, [])
    monkeypatch.setattr(ds_mod, "ds_api", api)
    # Prove the fake reproduces the stale-handle bug: the handle merge_records returns says d0.
    probe = FakeDataset("probe")
    assert probe.merge_records([{}] * 4).digest == "d0" and probe.digest == "d4"
    ds, digest = ds_mod.sync_dataset(make_cfg(), "common", "earnings-preview", ef_fixture(), "42")
    assert digest == "d4"
    assert ds is store[TABLE]


def test_reuses_existing_dataset(monkeypatch):
    store, created = {}, []
    monkeypatch.setattr(ds_mod, "ds_api", fake_api(store, created))
    store[TABLE] = FakeDataset(TABLE)
    ds_mod.sync_dataset(make_cfg(), "common", "earnings-preview", ef_fixture(), "42")
    assert created == []


def test_expected_skill_is_always_written_so_a_merge_clears_a_stale_label():
    """Merge_records merges expectations as {**existing, **new}, so a key left out keeps its old value."""
    stale = {"should_trigger": True, "expected_skill": "x", "reviewed": True}
    negative = EvalCase(id=7, prompt="Summarise this news article", should_trigger=False)
    new = ds_mod.case_record(negative, "x")["expectations"]
    assert None not in new.values()  # nulls are rejected
    assert {**stale, **new}["expected_skill"] == ds_mod.NO_EXPECTED_SKILL
