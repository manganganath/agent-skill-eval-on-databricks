import json
import re

from skillhub.results.dashboard import build
from skillhub.results.tables import MATRIX_COLUMNS, table_columns

USAGE_COLUMNS = {
    "workspace_id",
    "account_id",
    "client_request_id",
    "databricks_request_id",
    "requester",
    "status_code",
    "request_time",
    "input_token_count",
    "output_token_count",
    "input_character_count",
    "output_character_count",
    "usage_context",
    "request_streaming",
    "served_entity_id",
}
ENTITY_COLUMNS = {
    "served_entity_id", "endpoint_name", "endpoint_id", "served_entity_name", "entity_name", "change_time",
    "endpoint_delete_time",
}  # fmt: skip
SQL_WORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "AND", "OR", "NOT", "NULL", "IS", "AS", "ON",
    "JOIN", "IN", "CASE", "WHEN", "THEN", "ELSE", "END", "DESC", "ASC", "DATE", "ALL", "DISTINCT",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "LIMIT", "INTERVAL", "CAST", "DOUBLE", "INT", "STRING", "WITH", "LEFT", "QUALIFY", "OVER", "PARTITION",
}  # fmt: skip


def allowed() -> dict[str, set[str]]:
    cols = table_columns()
    return {
        "eval.matrix_all": set(MATRIX_COLUMNS),
        "eval.runs_all": set(cols["eval_runs"]),
        "eval.cases_all": set(cols["eval_cases"]) | {"dept"},
        "system.serving.endpoint_usage": USAGE_COLUMNS,
        "system.serving.served_entities": ENTITY_COLUMNS,
    }


def identifiers(sql: str) -> set[str]:
    no_strings = re.sub(r"'[^']*'", "''", sql)
    no_ticks = no_strings.replace("`", " ")
    return set(re.findall(r"\b([A-Za-z_]\w*)\b(?!\s*\()", no_ticks))


def sql_of(ds: dict) -> str:
    return "".join(ds["queryLines"])


def load() -> dict:
    return json.loads(build.OUT.read_text(encoding="utf-8"))


def test_committed_json_matches_generator():
    assert build.OUT.read_text(encoding="utf-8") == build.render()
    assert load() == build.build()


def test_every_dataset_query_references_only_existing_relations_and_columns():
    rel_cols = allowed()
    doc = load()
    assert doc["datasets"]
    for ds in doc["datasets"]:
        q = sql_of(ds)
        ctes = set(re.findall(r"\b(\w+) AS \(", q))
        rels = [r for r in re.findall(r"(?:FROM|JOIN)\s+([\w.]+)", q, re.I) if r not in ctes]
        assert rels, f"{ds['name']} reads no relation"
        assert all(r in rel_cols for r in rels), f"{ds['name']} reads unknown relation {rels}"
        cols = set().union(*(rel_cols[r] for r in rels))
        aliases = set(re.findall(r"\bAS\s+(\w+)", q, re.I))
        known = {part for r in rels for part in r.split(".")} | ctes
        unknown = identifiers(q) - cols - aliases - SQL_WORDS - known
        assert not unknown, f"{ds['name']} references unknown names {sorted(unknown)}"


def test_cost_dataset_takes_the_latest_entity_row_per_served_entity():
    q = sql_of(next(d for d in load()["datasets"] if d["name"] == "cost"))
    assert "ROW_NUMBER() OVER (PARTITION BY served_entity_id ORDER BY change_time DESC) = 1" in q
    assert "u.served_entity_id = l.served_entity_id" in q


def output_columns(ds: dict) -> set[str]:
    """Output column names of a dataset's top-level SELECT (the last one, after any CTE)."""
    q = sql_of(ds)
    select = re.findall(r"SELECT (.*?) FROM", q, re.S)[-1]
    parts = re.split(r",\s*(?![^()]*\))", select)
    return {p.split(" AS ")[-1].strip().split(".")[-1] for p in parts}


def test_widgets_point_at_declared_datasets_and_output_columns():
    doc = load()
    outputs = {d["name"]: output_columns(d) for d in doc["datasets"]}
    for page in doc["pages"]:
        assert page["layout"], page["name"]
        for item in page["layout"]:
            widget = item["widget"]
            spec = widget["spec"]
            for q in widget["queries"]:
                query = q["query"]
                assert query["datasetName"] in outputs
                names = {f["name"] for f in query["fields"]}
                for f in query["fields"]:
                    refs = set(re.findall(r"`(\w+)`", f["expression"])) - {
                        "associative_filter_predicate_group"
                    }
                    assert refs <= outputs[query["datasetName"]], (widget["name"], f)
                enc = spec["encodings"]
                used = {v["fieldName"] for k, v in enc.items() if isinstance(v, dict) and "fieldName" in v}
                used |= {c["fieldName"] for c in enc.get("columns", [])}
                used |= {c["fieldName"] for c in enc.get("fields", [])}
                assert used <= names, widget["name"]
                if spec["widgetType"] == "table":
                    assert query["disaggregated"] is True
                elif spec["widgetType"] in ("bar", "line"):
                    assert query["disaggregated"] is False


def test_trend_charts_split_trigger_accuracy_and_with_skill_pass_rate():
    doc = load()
    sets = {d["name"]: sql_of(d) for d in doc["datasets"]}
    assert "mode = 'trigger'" in sets["trend_trigger"] and "accuracy" in sets["trend_trigger"]
    assert "mode = 'with_skill'" in sets["trend_skill"] and "pass_rate" in sets["trend_skill"]
    trends = next(p for p in doc["pages"] if p["name"] == "trends")
    charts = [i["widget"] for i in trends["layout"] if i["widget"]["spec"]["widgetType"] == "line"]
    assert len(charts) == 2
    for c in charts:
        assert c["spec"]["encodings"]["color"]["fieldName"] == "skill"
        assert c["spec"]["encodings"]["x"]["scale"]["type"] == "temporal"


def test_matrix_table_shows_experiment_link_column():
    assert "experiment_id" in MATRIX_COLUMNS
    assert "experiment_id" in sql_of(load()["datasets"][0])


def test_pages_cover_the_spec_list():
    titles = [p["displayName"] for p in build.build()["pages"]]
    assert titles == ["Verdicts", "Uplift", "Trigger", "Trends", "Health", "Cost"]


def test_cost_page_can_be_left_out_when_the_usage_table_is_missing():
    doc = build.build(include_cost=False)
    assert "Cost" not in [p["displayName"] for p in doc["pages"]]
    assert all("system." not in sql_of(d) for d in doc["datasets"])


def test_json_has_no_workspace_specific_values():
    text = build.OUT.read_text(encoding="utf-8")
    for pattern in (r"https?://", r"cloud\.databricks", r"warehouse", r"@", r"\bmain\.", r"/Users/"):
        assert not re.search(pattern, text), pattern
    assert all(re.search(r"FROM (eval|system)\.", sql_of(d)) for d in load()["datasets"])


def test_matrix_dataset_selects_harness():
    assert "harness" in MATRIX_COLUMNS
    assert "harness" in sql_of(load()["datasets"][0])


def test_matrix_dataset_selects_the_department_first():
    """A skill name used in two departments must show as two distinguishable rows."""
    assert build.MATRIX_FIELDS[0] == "dept"
    assert sql_of(load()["datasets"][0]).startswith("SELECT dept, skill,")
