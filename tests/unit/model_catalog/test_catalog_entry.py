import pytest

from skillhub.config import ModelProfile
from skillhub.model_catalog.entry import CatalogEntry, ConnectionTest, Target, clashes, problems


def ug(**over):
    row = {"name": "llama-maverick", "kind": "ug_model", "route": "serving",
           "endpoint": "databricks-llama-4-maverick", "model_class": "fast"}  # fmt: skip
    return CatalogEntry(**{**row, **over})


def nova(**over):
    row = {"name": "nova-lite", "kind": "external_model", "route": "bedrock", "model_id": "amazon.nova-lite-v1:0",
           "region": "us-east-1", "model_class": "fast"}  # fmt: skip
    return CatalogEntry(**{**row, **over})


def test_a_model_on_unity_gateway_and_a_bedrock_model_can_be_written():
    assert problems(ug()) == []
    assert problems(nova()) == []
    assert problems(nova(model_id="us.anthropic.claude-sonnet-5-5", model_class="frontier")) == []


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        (ug(name="Llama"), "name:"),
        (ug(name="x" * 41), "name:"),
        (ug(name="default"), "reserved"),
        (ug(route="ug-anthropic"), "ug-anthropic route is not supported yet"),
        (ug(endpoint=None), "endpoint:"),
        (ug(region="us-east-1"), "for external models only"),
        (nova(region=None), "region:"),
        (nova(region="Virginia"), "region:"),
        (nova(model_id="nova lite"), "model_id:"),
        (nova(route="serving"), "route: an external model uses the bedrock route"),
        (nova(harness="claude-code"), "harness:"),
        (nova(price_in=-1.0), "price_in:"),
        (nova(price_out=float("nan")), "price_out:"),
    ],
)
def test_problems_name_the_field_in_plain_words(entry, says):
    assert any(says in p for p in problems(entry)), problems(entry)


def test_an_external_agent_row_is_kept_but_never_runs_or_written_by_this_release():
    agent = CatalogEntry(name="skill-agent", kind="external_agent", harness="agentcore", model_class="frontier",
                         agent='{"runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x"}')  # fmt: skip
    assert not agent.runnable
    assert problems(agent) == ["kind: external_agent rows cannot be added in this release"]
    with pytest.raises(ValueError, match="not a model profile"):
        agent.to_profile(3)


def test_a_row_becomes_a_profile_that_records_its_kind_and_catalog_version():
    assert nova().to_profile(7) == ModelProfile(
        route="bedrock", model_id="amazon.nova-lite-v1:0", region="us-east-1", kind="external_model",
        catalog_version=7, **{"class": "fast"},
    )  # fmt: skip
    profile = ug().to_profile(None)
    assert (profile.route, profile.endpoint, profile.kind, profile.provider) == (
        "serving", "databricks-llama-4-maverick", "ug_model", "other",
    )  # fmt: skip


def test_target_round_trips_through_job_parameters():
    t = nova().target()
    assert t == Target("bedrock", "", "amazon.nova-lite-v1:0", "us-east-1")
    assert Target.from_params({**t.params(), "name": "nova-lite", "endpoint": None}) == t


def test_test_result_reads_back_and_ignores_anything_else():
    r = ConnectionTest("failed", "AccessDeniedException: not authorized", 42)
    assert ConnectionTest.from_json(r.to_json()) == r
    assert nova(test_result=r.to_json()).last_test == r
    for junk in (None, "", "not json", "[]", '{"status": "maybe"}'):
        assert ConnectionTest.from_json(junk) is None


def test_stored_timestamps_read_as_text_and_unknown_columns_are_ignored():
    from datetime import UTC, datetime

    row = nova(tested_at=datetime(2026, 10, 31, tzinfo=UTC), surprise="x")
    assert row.tested_at == "2026-10-31 00:00:00+00:00"


def test_a_hub_yaml_name_clashes():
    assert clashes("gpt", ["claude-sonnet", "gpt"])
    assert not clashes("nova-lite", ["claude-sonnet", "gpt"])
