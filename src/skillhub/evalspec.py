from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_log = logging.getLogger(__name__)


class Criteria(BaseModel):
    """Per-skill verdict thresholds. They may only tighten `defaults` in hub.yaml: see `gate_thresholds`."""

    fail_rate: float | None = Field(default=None, ge=0, le=1)
    warn_rate: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _warn_not_below_fail(self) -> Criteria:
        if self.fail_rate is not None and self.warn_rate is not None and self.warn_rate < self.fail_rate:
            raise ValueError("criteria.warn_rate must not be below criteria.fail_rate")
        return self


class EvalCase(BaseModel):
    id: int
    kind: Literal["trigger", "effectiveness"] = "trigger"
    prompt: str  # the user input; "prompt" is the customer's field name
    should_trigger: bool
    expected_skill: str | None = None
    assertions: list[str] = []
    fixtures: list[str] = []
    expected_tool_calls: list[dict] | None = None
    bug_scope: list[str] | None = None
    origin: Literal["generated", "human"] = "human"
    reviewed: bool = True

    @field_validator("expected_skill", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        """The MLflow dataset API rejects nulls, so dataset rows carry "" for "no skill"; it means None."""
        return None if v == "" else v

    def resolved_expected(self, skill_name: str) -> str | None:
        """The skill this case expects to fire, or None when no skill is expected.

        A near-miss has should_trigger=False and expected_skill=<sibling>, so the sibling is returned.
        """
        if self.expected_skill is not None:
            return self.expected_skill
        return skill_name if self.should_trigger else None


EFFECTIVENESS_FIRST_ID = 101  # trigger case ids are 1-100, effectiveness case ids 101 and up


class EvalFile(BaseModel):
    skill_name: str
    criteria: Criteria = Criteria()
    models: list[str] | None = None
    evals: list[EvalCase]

    @model_validator(mode="after")
    def _ids_are_unique(self) -> EvalFile:
        """A reused id would silently replace another case, and its reviewed results with it."""
        dup = sorted(i for i, n in Counter(c.id for c in self.evals).items() if n > 1)
        if dup:
            raise ValueError(f"case ids must be unique; repeated: {', '.join(map(str, dup))}")
        return self


class Criterion(BaseModel):
    id: str  # "c1".."c10"
    text: str
    type: Literal["task_quality", "skill_format"]
    weight: int = Field(ge=1, le=5)
    source: str


class Rubric(BaseModel):
    skill_name: str
    source_hash: str
    generated_by: str
    pass_score: float = Field(default=0.7, gt=0, le=1)
    reviewed: bool = False
    criteria: list[Criterion] = Field(min_length=5, max_length=10)


def _dump(obj: dict) -> str:
    # exclude_none keeps customer files minimal; round-trips to the same model.
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def load_evals(path: Path) -> EvalFile:
    return EvalFile.model_validate_json(path.read_text(encoding="utf-8"))


def read_evals(path: Path) -> tuple[EvalFile, bytes]:
    """evals.json parsed, and the bytes it was parsed from. The eval digests cover exactly these bytes."""
    data = path.read_bytes()
    return EvalFile.model_validate_json(data), data


def save_evals(path: Path, ef: EvalFile) -> None:
    path.write_text(_dump(ef.model_dump(mode="json", exclude_none=True)), encoding="utf-8")


def load_rubric(path: Path) -> Rubric:
    return Rubric.model_validate_json(path.read_text(encoding="utf-8"))


def save_rubric(path: Path, r: Rubric) -> None:
    path.write_text(_dump(r.model_dump(mode="json", exclude_none=True)), encoding="utf-8")


def rubric_hash(r: Rubric) -> str:
    canon = json.dumps(r.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GateThresholds:
    """The thresholds a verdict is computed with: hub.yaml `defaults`, tightened (never loosened) per skill."""

    pass_score: float
    fail_rate: float
    warn_rate: float
    max_error_rate: float
    min_reviewed_cases: int


def _explicit_pass_score(rubric: Rubric | None) -> float | None:
    """pass_score only when rubric.json sets it; the model default must not override hub.yaml."""
    if rubric is None or "pass_score" not in rubric.model_fields_set:
        return None
    return rubric.pass_score


def loosening_thresholds(defaults, criteria: Criteria | None, rubric: Rubric | None = None) -> list[str]:
    """Per-skill values that would loosen the gate. Each is ignored for gating.

    Directions, from eval/verdict.py: a case passes when its score is >= pass_score, and a run fails or warns
    when pass_rate < fail_rate or < warn_rate. So a lower value of any of the three is looser.
    max_error_rate and min_reviewed_cases are never per skill: a higher min_reviewed_cases turns a gating
    verdict into a non-blocking `advisory`, so neither direction is safe.
    """
    out: list[str] = []
    for name in ("fail_rate", "warn_rate"):
        value = getattr(criteria, name, None) if criteria is not None else None
        if value is not None and value < getattr(defaults, name):
            out.append(f"criteria.{name} {value} is below defaults.{name} {getattr(defaults, name)}")
    score = _explicit_pass_score(rubric)
    if score is not None and score < defaults.pass_score:
        out.append(f"rubric pass_score {score} is below defaults.pass_score {defaults.pass_score}")
    return out


def gate_thresholds(
    defaults, criteria: Criteria | None = None, rubric: Rubric | None = None, *, skill: str = ""
) -> GateThresholds:
    """The thresholds for one skill. A per-skill value may only tighten hub.yaml `defaults`; a loosening one
    is ignored with a WARNING (lint reports it too)."""
    for issue in loosening_thresholds(defaults, criteria, rubric):
        _log.warning("%s: %s; the default is used for gating", skill or "skill", issue)

    def tighter(value: float | None, default: float) -> float:
        return default if value is None else max(value, default)

    c = criteria or Criteria()
    return GateThresholds(
        pass_score=tighter(_explicit_pass_score(rubric), defaults.pass_score),
        fail_rate=tighter(c.fail_rate, defaults.fail_rate),
        warn_rate=tighter(c.warn_rate, defaults.warn_rate),
        max_error_rate=defaults.max_error_rate,
        min_reviewed_cases=defaults.min_reviewed_cases,
    )


# --- eval inputs: what the gates bind besides the skill content ---------------------------------------

NO_RUBRIC = "none"  # rubric_hash of a skill without rubric.json (trigger-only evals)
FIXTURE_INDEX = "web_index.json"  # the agent's fixture search index (agent/fixtures.py)
PLANTED_FIXTURE = "northwind/model.xlsx"  # the audit-xls workbook with planted bugs (eval/scorers.py)
PLANTED_MANIFEST = "northwind/model_bugs.json"  # the planted bugs the scorer checks the answer against


def fixture_index_files(root: Path) -> list[str]:
    """Every search index under the fixtures root, relative and sorted. The agent searches all of them."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob(FIXTURE_INDEX) if p.is_file())


def fixture_paths(ef: EvalFile, index_files: Iterable[str] = ()) -> list[str]:
    """The fixture files whose bytes decide what the agent and the scorers read for these cases: each case's
    `fixtures`, every search index when any case has fixtures (the search tool reads them all), and the
    planted-bug manifest when a case uses the planted workbook."""
    refs = {f for c in ef.evals for f in c.fixtures}
    paths = set(refs)
    if refs:
        paths |= set(index_files)
    if PLANTED_FIXTURE in refs:
        paths.add(PLANTED_MANIFEST)
    return sorted(paths)


def read_fixture(root: Path, rel: str) -> bytes | None:
    """A fixture's bytes, or None when it is missing or resolves outside the root (the agent cannot read
    those either)."""
    base = Path(root).resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        return None
    return p.read_bytes()


def dataset_digest(evals_json: bytes, fixtures: Mapping[str, bytes | None]) -> str:
    """The digest the gates bind as `dataset_digest`: the evals.json bytes and the bytes of every fixture in
    `fixtures` (from fixture_paths). A missing fixture is part of the digest too."""
    h = hashlib.sha256()

    def add(name: str, data: bytes | None) -> None:
        h.update(name.encode("utf-8") + b"\0")
        h.update(b"missing\0" if data is None else len(data).to_bytes(8, "big") + data)

    add("evals/evals.json", evals_json)
    for rel in sorted(fixtures):
        add(f"fixtures/{rel}", fixtures[rel])
    return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class EvalDigests:
    """What one skill's eval read besides its content: the rubric and the cases with their fixtures."""

    rubric_hash: str  # NO_RUBRIC without rubric.json
    dataset_digest: str


def eval_digests(skill_root: Path, fixtures_root: Path) -> EvalDigests:
    """The digests of a skill folder's evals/ and the fixtures its cases use, as the runner computes them from
    what it loads. Raises OSError or ValueError for a missing or invalid evals.json or rubric.json."""
    ef, data = read_evals(Path(skill_root) / "evals" / "evals.json")
    rubric_path = Path(skill_root) / "evals" / "rubric.json"
    rubric = rubric_hash(load_rubric(rubric_path)) if rubric_path.exists() else NO_RUBRIC
    paths = fixture_paths(ef, fixture_index_files(fixtures_root))
    return EvalDigests(rubric, dataset_digest(data, {rel: read_fixture(fixtures_root, rel) for rel in paths}))


# --- the planned matrix: which (model, mode) cells an eval runs for a skill -----------------------------

REQUEST_MODES = ("trigger", "effectiveness")
EFFECTIVENESS_ORDER = ("without_skill", "with_skill")  # without first, so with_skill can compute uplift
CASE_KIND = {"trigger": "trigger", "with_skill": "effectiveness", "without_skill": "effectiveness"}
GATING_MODES = ("trigger", "with_skill")  # without_skill is the baseline and never gates


def expand_modes(modes: Iterable[str]) -> list[str]:
    """Request modes -> run modes: trigger stays, effectiveness is without_skill then with_skill."""
    out: list[str] = []
    for m in modes:
        out += ["trigger"] if m == "trigger" else list(EFFECTIVENESS_ORDER)
    return list(dict.fromkeys(out))


def run_modes(ef: EvalFile, modes: Iterable[str]) -> list[str]:
    """The run modes a skill gets: those requested that its evals.json has cases for."""
    kinds = {c.kind for c in ef.evals}
    return [m for m in expand_modes(modes) if CASE_KIND[m] in kinds]


def resolve_models(ef: EvalFile, request_models: Iterable[str], default_models: Iterable[str]) -> list[str]:
    """An explicit request list wins; else the skill's evals.json `models`; else defaults.models."""
    return list(dict.fromkeys(list(request_models) or ef.models or list(default_models)))


def planned_cells(
    ef: EvalFile,
    *,
    request_models: Iterable[str] = (),
    default_models: Iterable[str],
    modes: Iterable[str] = REQUEST_MODES,
) -> list[str]:
    """Every gating cell an eval of this skill must produce, as `model/mode`, resolved like the runner resolves
    them. A model the runner cannot serve stays in the plan: its missing rows fail the gates closed."""
    gating = [m for m in run_modes(ef, modes) if m in GATING_MODES]
    return [
        f"{model}/{mode}" for model in resolve_models(ef, request_models, default_models) for mode in gating
    ]
