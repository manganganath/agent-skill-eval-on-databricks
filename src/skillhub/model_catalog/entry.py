"""One row of the model catalog: an eval target that a reviewer registered in the Skill Hub.

Kinds: `ug_model`, a serving endpoint on the Unity Gateway; `external_model`, a model that Databricks does not
serve, reached through Amazon Bedrock; and `external_agent`, a harness that runs each case itself. This release
runs the two model kinds. An `external_agent` row is kept as it is, but it is never offered, tested or run.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from skillhub.config import AWS_REGION, ModelProfile

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
KINDS = ("ug_model", "external_model", "external_agent")
RUNNABLE_KINDS = ("ug_model", "external_model")
REFERENCE = "reference"  # the built-in agent loop: the harness of every model
_ENDPOINT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
# A Bedrock model ID (amazon.nova-lite-v1:0), inference profile ID (us.anthropic.claude-sonnet-5-5) or ARN.
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")

Kind = Literal["ug_model", "external_model", "external_agent"]
TestStatus = Literal["passed", "failed"]


@dataclass(frozen=True)
class Target:
    """What a row points at. A connection test proves one target; changing it makes the test stale."""

    route: str = ""
    endpoint: str = ""
    model_id: str = ""
    region: str = ""

    def params(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_params(cls, params: Mapping[str, str | None]) -> Target:
        return cls(**{k: str(params.get(k) or "") for k in ("route", "endpoint", "model_id", "region")})


@dataclass(frozen=True)
class ConnectionTest:
    """The last connection test of a row, stored as JSON in `test_result`."""

    status: TestStatus
    message: str
    job_run_id: int | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> ConnectionTest | None:
        """None for no test, or for text that is not a test result."""
        try:
            data = json.loads(text) if text else None
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("status") not in ("passed", "failed"):
            return None
        run = data.get("job_run_id")
        return cls(data["status"], str(data.get("message") or ""), run if isinstance(run, int) else None)


class CatalogEntry(BaseModel):
    """One `model_profiles` row, as stored. `problems` checks a row before it is written."""

    model_config = ConfigDict(extra="ignore")

    name: str
    kind: Kind
    route: str | None = None
    endpoint: str | None = None
    model_id: str | None = None
    region: str | None = None
    model_class: Literal["frontier", "fast"]
    harness: str = REFERENCE
    agent: str | None = None  # external agents only
    price_in: float | None = None  # USD per million input tokens
    price_out: float | None = None
    enabled: bool = False
    tested_at: str | None = None
    test_result: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None

    @field_validator("tested_at", "created_at", "updated_at", mode="before")
    @classmethod
    def _as_text(cls, v: object) -> str | None:
        return None if v is None else str(v)

    @field_validator("harness", mode="before")
    @classmethod
    def _harness_default(cls, v: object) -> object:
        return REFERENCE if v is None else v

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled_default(cls, v: object) -> object:
        return False if v is None else v

    @property
    def runnable(self) -> bool:
        return self.kind in RUNNABLE_KINDS

    @property
    def last_test(self) -> ConnectionTest | None:
        return ConnectionTest.from_json(self.test_result)

    def target(self) -> Target:
        return Target(self.route or "", self.endpoint or "", self.model_id or "", self.region or "")

    def to_profile(self, version: int | None) -> ModelProfile:
        """The eval runner's profile for this row. `version` is the Delta version of the table it was read from."""
        if not self.runnable:
            raise ValueError(f"{self.name}: a {self.kind} row is not a model profile")
        return ModelProfile(
            route=self.route,  # type: ignore[arg-type]
            endpoint=self.endpoint,
            model_id=self.model_id,
            region=self.region,
            kind=self.kind,  # type: ignore[arg-type]
            catalog_version=version,
            **{"class": self.model_class},
        )


RESERVED_NAME = "default"  # `--models default` means the hub.yaml models


def problems(entry: CatalogEntry) -> list[str]:
    """What is wrong with a model row before it is written, in plain words. [] when it may be written."""
    if not entry.runnable:
        return [f"kind: {entry.kind} rows cannot be added in this release"]
    out: list[str] = []
    if not NAME_RE.fullmatch(entry.name):
        out.append(
            "name: use 1 to 40 lowercase letters, digits and hyphens, starting with a letter or a digit"
        )
    elif entry.name == RESERVED_NAME:
        out.append("name: `default` is reserved for the release-gate models; pick another name")
    if entry.kind == "ug_model":
        if entry.route == "ug-anthropic":
            out.append("route: the ug-anthropic route is not supported yet; use serving")
        elif entry.route != "serving":
            out.append("route: a model on Unity Gateway uses the serving route")
        if not entry.endpoint or not _ENDPOINT_RE.fullmatch(entry.endpoint):
            out.append("endpoint: pick a serving endpoint")
        if entry.model_id or entry.region:
            out.append("model_id and region are for external models only")
    elif entry.kind == "external_model":
        if entry.route != "bedrock":
            out.append("route: an external model uses the bedrock route")
        if not entry.model_id or not _MODEL_ID_RE.fullmatch(entry.model_id):
            out.append(
                "model_id: give a Bedrock model ID such as amazon.nova-lite-v1:0, or an inference profile ID "
                "such as us.anthropic.claude-sonnet-5-5"
            )
        if not entry.region or not AWS_REGION.fullmatch(entry.region):
            out.append("region: give an AWS region such as us-east-1")
        if entry.endpoint:
            out.append("endpoint is for models on Unity Gateway only")
    if entry.harness != REFERENCE:
        out.append("harness: a model runs on the reference harness")
    for field in ("price_in", "price_out"):
        value = getattr(entry, field)
        if value is not None and (not math.isfinite(value) or value < 0):
            out.append(f"{field}: give a price of 0 or more, in USD per million tokens")
    return out


def clashes(name: str, hub_models: Iterable[str]) -> bool:
    """Whether the name is a hub.yaml profile. hub.yaml would win, so registration refuses it."""
    return name in set(hub_models)
