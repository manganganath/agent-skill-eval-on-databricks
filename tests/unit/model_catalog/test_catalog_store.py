import re
from datetime import UTC, datetime

import pytest

from skillhub.model_catalog import store
from skillhub.model_catalog.entry import CatalogEntry, ConnectionTest, Target
from tests.fakes import FakeSql, make_cfg

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
NOVA = CatalogEntry(
    name="nova-lite", kind="external_model", route="bedrock", model_id="amazon.nova-lite-v1:0",
    region="us-east-1", model_class="fast",
)  # fmt: skip


@pytest.fixture
def cat():
    cfg, sql = make_cfg(), FakeSql()
    store.ensure_table(cfg, sql)
    return cfg, sql


def test_the_table_lives_in_the_eval_schema_with_every_column_of_a_row(cat):
    cfg, sql = cat
    assert store.table_name(cfg) == "skill_hub.eval.model_profiles"
    assert sql.calls[0][0].startswith("CREATE TABLE IF NOT EXISTS skill_hub.eval.model_profiles (name STRING")
    assert set(store.NAMES) == set(CatalogEntry.model_fields)


def test_a_new_row_is_disabled_untested_and_records_who_added_it(cat):
    cfg, sql = cat
    assert store.insert_entry(sql, cfg, NOVA.model_copy(update={"enabled": True}), "rita@example.com", NOW)
    [row], version = store.read_entries(sql, cfg)
    assert version is None  # the fake has no Delta history
    assert row.name == "nova-lite" and row.enabled is True  # the API decides enabled; the store writes it
    assert (row.test_result, row.tested_at) == (None, None)
    assert row.created_by == row.updated_by == "rita@example.com"


def test_a_taken_name_is_not_added_again(cat):
    cfg, sql = cat
    assert store.insert_entry(sql, cfg, NOVA, "rita@example.com", NOW)
    assert not store.insert_entry(sql, cfg, NOVA.model_copy(update={"model_class": "frontier"}), "x", NOW)
    [row], _ = store.read_entries(sql, cfg)
    assert row.model_class == "fast"


def test_an_update_writes_only_the_editable_columns_and_can_clear_the_test(cat):
    cfg, sql = cat
    store.insert_entry(sql, cfg, NOVA, "rita@example.com", NOW)
    store.record_test(sql, cfg, "nova-lite", NOVA.target(), ConnectionTest("passed", "ok", 5).to_json(), NOW)
    changed = NOVA.model_copy(update={"region": "us-west-2", "price_in": 0.06, "kind": "ug_model"})
    store.update_entry(sql, cfg, changed, "ravi@example.com", NOW, clear_test=True)
    [row], _ = store.read_entries(sql, cfg)
    assert (row.region, row.price_in, row.kind) == ("us-west-2", 0.06, "external_model")  # kind is fixed
    assert (row.test_result, row.tested_at, row.updated_by) == (None, None, "ravi@example.com")


def test_a_test_is_recorded_only_on_the_target_it_tested(cat):
    cfg, sql = cat
    store.insert_entry(sql, cfg, NOVA, "rita@example.com", NOW)
    passed = ConnectionTest("passed", "answered in 812 ms", 9).to_json()
    store.record_test(
        sql, cfg, "nova-lite", Target("bedrock", "", "amazon.nova-lite-v1:0", "us-west-2"), passed, NOW
    )
    assert store.read_entries(sql, cfg)[0][0].last_test is None
    store.record_test(sql, cfg, "nova-lite", NOVA.target(), passed, NOW)
    assert store.read_entries(sql, cfg)[0][0].last_test == ConnectionTest("passed", "answered in 812 ms", 9)


def test_the_rows_are_read_at_the_version_the_history_names():
    class Versioned(FakeSql):
        """Answers DESCRIBE HISTORY with version 12, and runs a time-travel read as a plain one."""

        def __init__(self):
            super().__init__()
            self.sent = []

        def execute(self, statement, params=None):
            self.sent.append(statement)
            if statement.startswith("DESCRIBE HISTORY"):
                return [{"version": 12}]
            return super().execute(statement.replace(" VERSION AS OF 12", ""), params)

    cfg, sql = make_cfg(), Versioned()
    store.ensure_table(cfg, sql)
    _, version = store.read_entries(sql, cfg)
    assert version == 12
    assert any(" FROM skill_hub.eval.model_profiles VERSION AS OF 12 " in s for s in sql.sent)


def test_a_row_that_does_not_parse_is_skipped_and_the_oldest_row_of_a_name_wins(cat):
    cfg, sql = cat
    t = store.table_name(cfg)
    sql.execute(
        f"INSERT INTO {t} (name, kind, model_class, created_at) VALUES ('odd', 'unknown_kind', 'fast', '1')"
    )
    sql.execute(f"INSERT INTO {t} (name, kind, model_class, route, endpoint, created_at) "
                "VALUES ('twin', 'ug_model', 'fast', 'serving', 'ep-new', '2026-10-31T10:00:00')")  # fmt: skip
    sql.execute(f"INSERT INTO {t} (name, kind, model_class, route, endpoint, created_at) "
                "VALUES ('twin', 'ug_model', 'fast', 'serving', 'ep-old', '2026-10-31T09:00:00')")  # fmt: skip
    entries, _ = store.read_entries(sql, cfg)
    assert [(e.name, e.endpoint) for e in entries] == [("twin", "ep-old")]


def test_no_statement_ever_deletes_a_row(cat):
    cfg, sql = cat
    store.insert_entry(sql, cfg, NOVA, "rita@example.com", NOW)
    store.update_entry(sql, cfg, NOVA, "rita@example.com", NOW, clear_test=True)
    store.record_test(sql, cfg, "nova-lite", NOVA.target(), "{}", NOW)
    store.read_entries(sql, cfg)
    assert not [s for s, _ in sql.calls if re.search(r"\b(DELETE|TRUNCATE|DROP)\b", s, re.I)]
