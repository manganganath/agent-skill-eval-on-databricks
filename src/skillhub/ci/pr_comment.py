# src/skillhub/ci/pr_comment.py
"""Sticky PR comment with the eval matrix."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from skillhub.evalspec import CASE_KIND, NO_RUBRIC, REQUEST_MODES
from skillhub.naming import evals_schema

MARKER = "<!-- skillhub:pr-comment -->"
API = "https://api.github.com"
PASSING = frozenset({"pass", "warn", "advisory"})
COLUMNS = (
    "Skill",
    "Model",
    "Class",
    "Mode",
    "Verdict",
    "Trigger accuracy",
    "Uplift (task)",
    "Uplift (format)",
    "Spread",
    "Unreviewed",
)
LINK_COLUMN = "Run"
COMMENT_MAX = 65536  # characters: GitHub refuses a longer comment body (422)
_EXPECT = re.compile(r"^(?P<ref>[a-z0-9_]+/[a-z0-9][a-z0-9_-]*)=(?P<hash>sha256:[0-9a-f]{64})$")
_REF = re.compile(r"[a-z0-9_]+/[a-z0-9][a-z0-9_-]*")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
CELL = re.compile(r"[A-Za-z0-9_.-]+/(?:trigger|with_skill)")  # a gating cell: model profile/mode


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def _cell(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("`", "'")
    # Neutralise HTML and @mentions: the comment is posted on a public repo.
    return text.replace("<", "&lt;").replace("@", "@\u200b")


def exit_code(verdicts: list[str]) -> int:
    """0 only when there is at least one verdict and all are pass, warn or advisory (fail closed)."""
    if not verdicts:
        return 1
    return 0 if all(v in PASSING for v in verdicts) else 1


def _gating(r: dict[str, Any]) -> bool:
    return r.get("mode") != "without_skill"


def _counts(r: dict[str, Any], kinds: set[str]) -> bool:
    """A row of a mode in `kinds` counts toward the gate; a row with an unknown mode always does."""
    kind = CASE_KIND.get(r.get("mode") or "")
    return kind is None or kind in kinds


def _blocking(r: dict[str, Any], gating_modes: Iterable[str] = REQUEST_MODES) -> bool:
    return (
        _gating(r)
        and _counts(r, set(gating_modes))
        and (not r.get("mode") or r.get("verdict") not in PASSING)
    )


def blocking_verdicts(rows: list[dict[str, Any]], gating_modes: Iterable[str] = REQUEST_MODES) -> list[str]:
    """Verdicts that decide the exit code. `without_skill` rows are the baseline and are ignored, and so are
    rows of a request mode left out of `gating_modes` (reported, not blocking).

    A NULL verdict, or a row with no mode or an unknown one, becomes or stays a blocker (fail closed).
    """
    kinds = set(gating_modes)
    return [
        (r.get("verdict") or "") if r.get("mode") else "" for r in rows if _gating(r) and _counts(r, kinds)
    ]


def _ref(r: dict[str, Any]) -> str:
    return f"{r.get('dept')}/{r.get('skill')}"


def missing_skills(rows: list[dict[str, Any]], skills: str | Iterable[str]) -> list[str]:
    """Entries of a `dept/skill,...` list (or an iterable of them) that have no gating row. Empty: no check."""
    items = skills.split(",") if isinstance(skills, str) else skills
    wanted = list(dict.fromkeys(x.strip() for x in items if x.strip()))
    present = {_ref(r) for r in rows if _gating(r)}
    return [w for w in wanted if w not in present]


def parse_expect_hashes(values: Iterable[str]) -> dict[str, str]:
    """`dept/skill=sha256:<hex>` entries -> {dept/skill: hash}. ValueError on a bad or conflicting entry."""
    out: dict[str, str] = {}
    for v in values:
        m = _EXPECT.match(v.strip())
        if not m:
            raise ValueError(f"--expect-hash entries look like dept/skill=sha256:<64 hex digits>, got {v!r}")
        if out.get(m["ref"], m["hash"]) != m["hash"]:
            raise ValueError(f"--expect-hash gives two hashes for {m['ref']}")
        out[m["ref"]] = m["hash"]
    return out


@dataclass(frozen=True)
class Expected:
    """What every gating row of one skill must show. A None field is not checked (`--expect-hash` binds the
    content only); the merge and PR gates and the publish gate bind all of them."""

    content_hash: str
    rubric_hash: str | None = None  # NO_RUBRIC for a skill without rubric.json
    dataset_digest: str | None = None  # The evals.json and the fixtures its cases use
    cells: tuple[str, ...] | None = None  # Every planned gating cell, as model/mode


def binding_mismatches(rows: list[dict[str, Any]], expected: Mapping[str, Expected]) -> list[str]:
    """One message per gating row of an expected skill that evaluated other content or other eval
    inputs: a rubric or cases and fixtures that are not the expected ones."""
    out = []
    for r in rows:
        want = expected.get(_ref(r))
        if want is None or not _gating(r):
            continue
        where = f"{_ref(r)} {r.get('model_profile')} {r.get('mode')}"
        if r.get("content_hash") != want.content_hash:
            out.append(
                f"{where}: evaluated content {r.get('content_hash')} is not the expected {want.content_hash}"
            )
        for field, label in (("rubric_hash", "rubric"), ("dataset_digest", "cases and fixtures")):
            value = getattr(want, field)
            if value is not None and r.get(field) != value:
                out.append(f"{where}: evaluated {label} {field}={r.get(field)} is not the expected {value}")
    return out


def missing_cells(rows: list[dict[str, Any]], expected: Mapping[str, Expected]) -> list[str]:
    """`dept/skill model/mode` for every planned gating cell with no trusted row. A failing child run
    deleted before sync, or a model left out of the eval, leaves its cell empty, so the gate fails closed."""
    present = {(_ref(r), f"{r.get('model_profile')}/{r.get('mode')}") for r in rows if _gating(r)}
    return [
        f"{ref} {cell}"
        for ref, want in expected.items()
        for cell in want.cells or ()
        if (ref, cell) not in present
    ]


def parse_cells(value: str) -> tuple[str, ...]:
    """`model/mode,model/mode` -> the cells. ValueError on a malformed entry."""
    cells = tuple(dict.fromkeys(c.strip() for c in value.split(",") if c.strip()))
    bad = [c for c in cells if not CELL.fullmatch(c)]
    if bad:
        raise ValueError(f"cells look like <model profile>/trigger or <model profile>/with_skill, got {bad}")
    return cells


def hash_mismatches(rows: list[dict[str, Any]], expected: dict[str, str]) -> list[str]:
    """binding_mismatches for content hashes only: {dept/skill: content_hash}."""
    return binding_mismatches(rows, {ref: Expected(h) for ref, h in expected.items()})


def parse_expectations(text: str) -> dict[str, Expected]:
    """The JSON list `skillhub eval-digests` prints -> {dept/skill: Expected}, with the planned cells.
    ValueError when malformed."""
    try:
        items = json.loads(text)
    except ValueError as e:
        raise ValueError(f"the expected eval digests are not JSON: {e}") from None
    if not isinstance(items, list):
        raise ValueError("the expected eval digests must be a JSON list")
    out: dict[str, Expected] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each expected eval digest must be an object")
        ref = f"{item.get('dept')}/{item.get('skill')}"
        content, rubric, dataset = item.get("hash"), item.get("rubric_hash"), item.get("dataset_digest")
        if not _REF.fullmatch(ref) or ref in out:
            raise ValueError(f"bad or repeated skill {ref!r} in the expected eval digests")
        if not (isinstance(content, str) and _SHA.fullmatch(content) and isinstance(dataset, str)):
            raise ValueError(f"{ref}: hash and dataset_digest must look like sha256:<64 hex digits>")
        if not _SHA.fullmatch(dataset) or not (
            rubric == NO_RUBRIC or (isinstance(rubric, str) and _SHA.fullmatch(rubric))
        ):
            raise ValueError(
                f"{ref}: dataset_digest must be sha256:<hex>, and rubric_hash sha256:<hex> or none"
            )
        cells = item.get("cells")
        if not isinstance(cells, list) or not all(isinstance(c, str) and CELL.fullmatch(c) for c in cells):
            raise ValueError(f"{ref}: cells must be a list of <model profile>/<trigger|with_skill>")
        out[ref] = Expected(content, rubric, dataset, tuple(cells))
    return out


def render_matrix_markdown(
    rows: list[dict[str, Any]],
    *,
    run_key: str,
    links: bool = False,
    hub_url: str | None = None,
    gating_modes: Iterable[str] = REQUEST_MODES,
) -> str:
    """The comment body. The repository can be public, so by default it holds no workspace host, run or
    experiment ID and no Skill Hub URL. `links` (SKILLHUB_PR_LINKS=true, for private repositories)
    adds an MLflow link per row (`run_url`) and the Skill Hub URL. A body too long for one GitHub comment shows
    the blocking rows first and says how many rows it leaves out."""
    lines = [MARKER, f"### Skill evaluation `{_cell(run_key)}`", ""]
    if not rows:
        lines += [
            "No evaluation results were recorded for this run. Re-run the check. If it fails again, "
            "check the `skill-eval` Job run.",
            "",
        ]
        return "\n".join(lines)
    columns = (*COLUMNS, LINK_COLUMN) if links else COLUMNS
    lines += ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    ordered = sorted(
        rows, key=lambda r: tuple(str(r.get(k) or "") for k in ("skill", "model_profile", "mode"))
    )
    table = [_matrix_line(r, links) for r in ordered]
    footer = ["", f"Skill Hub: {hub_url}" if links and hub_url else "Details in Skill Hub."]
    body = "\n".join([*lines, *table, *footer])
    if len(body) <= COMMENT_MAX:
        return body

    def note(left_out: int) -> str:
        return f"{left_out} of {len(rows)} rows are not shown: a comment holds {COMMENT_MAX} characters."

    # Too long for one comment: the blocking rows first, then as many others as fit.
    room = COMMENT_MAX - len("\n".join([*lines, "", note(len(rows)), *footer]))
    shown: list[str] = []
    for r in sorted(
        ordered, key=lambda r: not _blocking(r, gating_modes)
    ):  # stable: each group keeps its order
        line = _matrix_line(r, links)
        if len(line) + 1 > room:
            break
        shown.append(line)
        room -= len(line) + 1
    return "\n".join([*lines, *shown, "", note(len(rows) - len(shown)), *footer])


def _matrix_line(r: dict[str, Any], links: bool) -> str:
    unreviewed = (r.get("n_cases") or 0) - (r.get("n_reviewed") or 0)
    cells = [
        r["skill"],
        r["model_profile"],
        r.get("model_class") or "-",
        r["mode"] or "-",
        r["verdict"] or "-",
        _pct(r.get("accuracy")),
        _num(r.get("uplift_task_quality")),
        _num(r.get("uplift_skill_format")),
        _num(r.get("spread")),
        unreviewed,
    ]
    if links:
        cells.append(f"[MLflow]({r['run_url']})" if r.get("run_url") else "-")
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_bot_comment(client: httpx.Client, url: str, headers: dict[str, str]) -> int | None:
    next_url: str | None = url
    while next_url:
        resp = client.get(next_url, headers=headers)
        resp.raise_for_status()
        for comment in resp.json():
            is_bot = (comment.get("user") or {}).get("type") == "Bot"
            if is_bot and MARKER in (comment.get("body") or ""):
                return int(comment["id"])
        next_url = resp.links.get("next", {}).get("url")
    return None


def upsert_pr_comment(
    *, repo: str, pr: int, body: str, token: str, client: httpx.Client | None = None, api: str = API
) -> str:
    """Create or update the sticky comment.

    The match requires a Bot author, so the comment is posted as `GITHUB_TOKEN`; a PAT would create a duplicate.
    """
    own = client is None
    http = client or httpx.Client(timeout=30)
    try:
        headers = _headers(token)
        existing = _find_bot_comment(http, f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100", headers)
        if existing is None:
            resp = http.post(f"{api}/repos/{repo}/issues/{pr}/comments", headers=headers, json={"body": body})
            resp.raise_for_status()
            return "created"
        resp = http.patch(
            f"{api}/repos/{repo}/issues/comments/{existing}", headers=headers, json={"body": body}
        )
        resp.raise_for_status()
        return "updated"
    finally:
        if own:
            http.close()


def _matrix_sql(cfg, n_authors: int = 1, *, merge_only: bool = False) -> str:
    """eval_runs lives in each department's evals schema, so read every one of them.

    Only child rows (parent_run_id set) by trusted authors count. A row with no mode stays in, and
    blocks the gate (blocking_verdicts). With `merge_only` (the publish gate), only rows whose trigger_source is
    the :trigger_source parameter count, before the latest row of each cell is picked.
    """
    authors = ", ".join(f":author_{i}" for i in range(n_authors))
    source = " AND trigger_source = :trigger_source" if merge_only else ""
    parts = [
        f"SELECT * EXCEPT (rn) FROM ("
        f"SELECT dept, skill, skill_version, content_hash, rubric_hash, dataset_digest, model_profile, "
        f"model_class, mode, verdict, accuracy, "
        f"spread, uplift_task_quality, uplift_skill_format, n_cases, n_reviewed, run_id, experiment_id, "
        f"ROW_NUMBER() OVER (PARTITION BY skill, skill_version, model_profile, "
        f"COALESCE(harness, 'reference'), mode "
        f"ORDER BY started_at DESC, synced_at DESC) AS rn "
        f"FROM {evals_schema(cfg, dept)}.eval_runs "
        f"WHERE run_key = :run_key AND parent_run_id IS NOT NULL AND run_author IN ({authors}){source}"
        f") WHERE rn = 1"
        for dept in cfg.departments
    ]
    return " UNION ALL ".join(parts)


def load_matrix_rows(
    fetch: Callable[[str, dict[str, str]], list[dict[str, Any]]],
    cfg,
    run_key: str,
    *,
    trusted: Iterable[str],
    trigger_source: str | None = None,
) -> list[dict[str, Any]]:
    """`fetch(sql, params)` returns rows as dicts: the latest row per partition, by trusted authors only.

    With `trigger_source`, only rows of runs started with that trigger source count (the publish gate passes the
    merge eval's). Without it every trigger source counts, as the PR and merge gates read their own keys."""
    authors = sorted(set(trusted))
    if not authors:
        raise ValueError(
            "no trusted run authors: set SKILLHUB_CI_EVAL_PRINCIPAL or run as the ci_eval principal"
        )
    params = {"run_key": run_key, **{f"author_{i}": a for i, a in enumerate(authors)}}
    if trigger_source is None:
        return fetch(_matrix_sql(cfg, len(authors)), params)
    return fetch(
        _matrix_sql(cfg, len(authors), merge_only=True), {**params, "trigger_source": trigger_source}
    )


def add_run_links(rows: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Add `run_url` (the MLflow run page) to each row. Only for SKILLHUB_PR_LINKS=true."""
    base = host.rstrip("/")
    for r in rows:
        r["run_url"] = f"{base}/ml/experiments/{r['experiment_id']}/runs/{r['run_id']}"
    return rows
