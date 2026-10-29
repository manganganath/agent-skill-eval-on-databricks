"""Generate skill_eval.lvdash.json. Run: python -m skillhub.results.dashboard.build

Dataset SQL uses schema-relative names (eval.matrix_all, ...). The catalog comes from the dashboard's
dataset_catalog setting at deploy time, so the file carries no catalog, host or warehouse.
The Lakeview JSON shape follows the vetted example export (queryLines, aggregate field names).
Reviewers need SELECT on system.serving.* for the Cost page.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("skill_eval.lvdash.json")

MATRIX_FIELDS = [
    "dept",  # the same skill name can exist in two departments
    "skill",
    "skill_version",
    "model_profile",
    "harness",
    "model_class",
    "mode",
    "verdict",
    "pass_rate",
    "spread",
    "error_rate",
    "unreviewed_cases",
    "experiment_id",
]
UPLIFT_FIELDS = [
    "skill",
    "skill_version",
    "model_profile",
    "pass_rate_without",
    "pass_rate",
    "uplift_task_quality",
    "uplift_skill_format",
]

DATASETS: dict[str, list[str]] = {
    "matrix": [f"SELECT {', '.join(MATRIX_FIELDS)} FROM eval.matrix_all"],
    "verdicts": [
        "SELECT model_profile, verdict, COUNT(*) AS n FROM eval.matrix_all GROUP BY model_profile, verdict"
    ],
    "uplift": [f"SELECT {', '.join(UPLIFT_FIELDS)} FROM eval.matrix_all WHERE mode = 'with_skill'"],
    "confusion": [
        "SELECT expected_skill, invoked_skill, trigger_outcome, COUNT(*) AS n FROM eval.cases_all ",
        "WHERE assessment_name = 'skill_trigger' GROUP BY expected_skill, invoked_skill, trigger_outcome",
    ],
    "trend_trigger": [
        "SELECT skill, skill_version, model_profile, started_at, accuracy FROM eval.runs_all ",
        "WHERE parent_run_id IS NOT NULL AND mode = 'trigger'",
    ],
    "trend_skill": [
        "SELECT skill, skill_version, model_profile, started_at, pass_rate FROM eval.runs_all ",
        "WHERE parent_run_id IS NOT NULL AND mode = 'with_skill'",
    ],
    "health": [
        "SELECT model_profile, AVG(error_rate) AS error_rate, ",
        "AVG(CASE WHEN verdict = 'inconclusive' THEN 1.0 ELSE 0.0 END) AS inconclusive_rate, ",
        "COUNT(*) AS runs FROM eval.matrix_all GROUP BY model_profile",
    ],
    "cost": [
        "WITH latest AS (SELECT served_entity_id, endpoint_name FROM system.serving.served_entities ",
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY served_entity_id ORDER BY change_time DESC) = 1) ",
        "SELECT l.endpoint_name, CAST(u.request_time AS DATE) AS day, ",
        "SUM(u.input_token_count) AS input_tokens, SUM(u.output_token_count) AS output_tokens ",
        "FROM system.serving.endpoint_usage AS u LEFT JOIN latest AS l ",
        "ON u.served_entity_id = l.served_entity_id GROUP BY l.endpoint_name, CAST(u.request_time AS DATE)",
    ],
}

COLUMN_TYPES = {
    "pass_rate": "float",
    "pass_rate_without": "float",
    "spread": "float",
    "error_rate": "float",
    "inconclusive_rate": "float",
    "uplift_task_quality": "float",
    "uplift_skill_format": "float",
    "skill_version": "integer",
    "unreviewed_cases": "integer",
    "n": "integer",
    "runs": "integer",
    "input_tokens": "integer",
    "output_tokens": "integer",
    "day": "datetime",
}
MEASURE_AGG = {"SUM": "sum", "AVG": "avg"}


def _field(name: str, expression: str | None = None) -> dict[str, str]:
    return {"name": name, "expression": expression or f"`{name}`"}


def _query(dataset: str, fields: list[dict[str, str]], disaggregated: bool, name: str = "main_query") -> dict:
    return {"name": name, "query": {"datasetName": dataset, "fields": fields, "disaggregated": disaggregated}}


def _frame(title: str) -> dict[str, Any]:
    return {"showTitle": True, "title": title}


def _table(name: str, dataset: str, fields: list[str], title: str) -> dict[str, Any]:
    columns = []
    for i, f in enumerate(fields):
        kind = COLUMN_TYPES.get(f, "datetime" if f.endswith("_at") else "string")
        numeric = kind in ("integer", "float")
        columns.append(
            {
                "fieldName": f,
                "type": kind,
                "displayAs": "number" if numeric else ("datetime" if kind == "datetime" else "string"),
                "title": f,
                "displayName": f,
                "order": 100000 + i,
                "alignContent": "right" if kind != "string" else "left",
            }
        )
    return {
        "name": name,
        "queries": [_query(dataset, [_field(f) for f in fields], True)],
        "spec": {
            "version": 1,
            "widgetType": "table",
            "encodings": {"columns": columns},
            "frame": _frame(title),
        },
    }


def _chart(
    kind: str,
    name: str,
    dataset: str,
    x: str,
    y: str,
    color: str | None,
    title: str,
    agg: str = "SUM",
    time_grain: str | None = None,
) -> dict[str, Any]:
    """Chart over a dataset column set; y is aggregated (the example's `sum(col)` field naming)."""
    y_name = f"{MEASURE_AGG[agg]}({y})"
    if time_grain:
        x_name, x_expr, x_type = (
            f"{time_grain.lower()}({x})",
            f'DATE_TRUNC("{time_grain}", `{x}`)',
            "temporal",
        )
    else:
        x_name, x_expr = x, f"`{x}`"
        x_type = "temporal" if x == "day" else "categorical"
    fields = [_field(x_name, x_expr), _field(y_name, f"{agg}(`{y}`)")]
    enc: dict[str, Any] = {
        "x": {"fieldName": x_name, "scale": {"type": x_type}, "displayName": x},
        "y": {"fieldName": y_name, "scale": {"type": "quantitative"}, "displayName": f"{agg} of {y}"},
    }
    if color:
        fields.append(_field(color))
        enc["color"] = {"fieldName": color, "scale": {"type": "categorical"}, "displayName": color}
    return {
        "name": name,
        "queries": [_query(dataset, fields, False)],
        "spec": {"version": 3, "widgetType": kind, "encodings": enc, "frame": _frame(title)},
    }


def _filter(name: str, dataset: str, column: str, title: str) -> dict[str, Any]:
    qname = f"filter_{name}_{column}"
    fields = [
        _field(column),
        _field(f"{column}_associativity", "COUNT_IF(`associative_filter_predicate_group`)"),
    ]
    return {
        "name": name,
        "queries": [_query(dataset, fields, False, qname)],
        "spec": {
            "version": 2,
            "widgetType": "filter-multi-select",
            "encodings": {"fields": [{"fieldName": column, "displayName": column, "queryName": qname}]},
            "frame": _frame(title),
        },
    }


def _page(name: str, display: str, widgets: list[dict[str, Any]]) -> dict[str, Any]:
    layout, y, x = [], 0, 0
    for w in widgets:
        if w["spec"]["widgetType"].startswith("filter-"):
            layout.append({"widget": w, "position": {"x": x, "y": y, "width": 2, "height": 2}})
            x += 2
            if x >= 6:
                x, y = 0, y + 2
            continue
        if x:
            x, y = 0, y + 2
        height = 5 if w["spec"]["widgetType"] == "table" else 4
        layout.append({"widget": w, "position": {"x": 0, "y": y, "width": 6, "height": height}})
        y += height
    return {"name": name, "displayName": display, "pageType": "PAGE_TYPE_CANVAS", "layout": layout}


def build(include_cost: bool = True) -> dict[str, Any]:
    pages = [
        _page(
            "verdicts",
            "Verdicts",
            [
                _table("matrix", "matrix", MATRIX_FIELDS, "Verdict matrix"),
                _chart(
                    "bar", "verdict_counts", "verdicts", "model_profile", "n", "verdict", "Verdicts by model"
                ),
            ],
        ),
        _page(
            "uplift",
            "Uplift",
            [
                _table("uplift", "uplift", UPLIFT_FIELDS, "Uplift with and without the skill"),
                _chart(
                    "bar",
                    "uplift_quality",
                    "uplift",
                    "skill",
                    "uplift_task_quality",
                    "model_profile",
                    "Task-quality uplift by skill",
                    agg="AVG",
                ),
                _chart(
                    "bar",
                    "uplift_format",
                    "uplift",
                    "skill",
                    "uplift_skill_format",
                    "model_profile",
                    "Skill-format uplift by skill",
                    agg="AVG",
                ),
            ],
        ),  # fmt: skip
        _page(
            "trigger",
            "Trigger",
            [
                _table(
                    "confusion",
                    "confusion",
                    ["expected_skill", "invoked_skill", "trigger_outcome", "n"],
                    "Trigger confusion",
                ),
                _chart(
                    "bar",
                    "outcomes",
                    "confusion",
                    "expected_skill",
                    "n",
                    "trigger_outcome",
                    "Trigger outcomes by expected skill",
                ),
            ],
        ),  # fmt: skip
        _page(
            "trends",
            "Trends",
            [
                _filter("trigger_skill_filter", "trend_trigger", "skill", "Trigger: skill"),
                _filter("trigger_model_filter", "trend_trigger", "model_profile", "Trigger: model"),
                _filter("trigger_version_filter", "trend_trigger", "skill_version", "Trigger: version"),
                _chart(
                    "line",
                    "trigger_accuracy_trend",
                    "trend_trigger",
                    "started_at",
                    "accuracy",
                    "skill",
                    "Trigger accuracy over time",
                    agg="AVG",
                    time_grain="DAY",
                ),
                _filter("skill_skill_filter", "trend_skill", "skill", "With skill: skill"),
                _filter("skill_model_filter", "trend_skill", "model_profile", "With skill: model"),
                _filter("skill_version_filter", "trend_skill", "skill_version", "With skill: version"),
                _chart(
                    "line",
                    "skill_pass_rate_trend",
                    "trend_skill",
                    "started_at",
                    "pass_rate",
                    "skill",
                    "Pass rate with the skill over time",
                    agg="AVG",
                    time_grain="DAY",
                ),
            ],
        ),  # fmt: skip
        _page(
            "health",
            "Health",
            [
                _chart(
                    "bar",
                    "error_rate",
                    "health",
                    "model_profile",
                    "error_rate",
                    None,
                    "Error rate by model",
                    agg="AVG",
                ),
                _table(
                    "health_table",
                    "health",
                    ["model_profile", "error_rate", "inconclusive_rate", "runs"],
                    "Error and inconclusive rates",
                ),
            ],
        ),  # fmt: skip
    ]
    if include_cost:
        pages.append(
            _page(
                "cost",
                "Cost",
                [
                    _chart(
                        "line", "tokens_by_day", "cost", "day", "input_tokens", "endpoint_name",
                        "Input tokens per day",
                    ),
                    _table(
                        "cost_table",
                        "cost",
                        ["endpoint_name", "day", "input_tokens", "output_tokens"],
                        "Token usage",
                    ),
                ],
            )
        )  # fmt: skip
    datasets = [
        {"name": n, "displayName": n, "queryLines": q}
        for n, q in DATASETS.items()
        if include_cost or n != "cost"
    ]
    return {
        "datasets": datasets,
        "pages": pages,
        "uiSettings": {
            "theme": {"widgetHeaderAlignment": "ALIGNMENT_UNSPECIFIED"},
            "applyModeEnabled": False,
        },
    }


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")
