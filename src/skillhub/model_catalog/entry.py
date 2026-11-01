"""One row of the model catalog: an eval target that a reviewer registered in the Skill Hub.

Kinds: `ug_model`, a serving endpoint on the Unity Gateway; `external_model`, a model that Databricks does not
serve, reached through Amazon Bedrock; and `external_agent`, a harness that runs each case itself and sends its
OpenTelemetry trace to Databricks. An agent is Claude Code (`claude-code`, which drives a Claude serving endpoint on
the `ug-anthropic` route) or an agent on Amazon Bedrock AgentCore (`agentcore`, which calls a Bedrock model). Its own
settings are a JSON object in the `agent` column: {"cli_version": ...} or {"runtime_arn": ...}.
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
RUNNABLE_KINDS = KINDS
REFERENCE = "reference"  # the built-in agent loop: the harness of every model
HARNESSES = ("claude-code", "agentcore")  # the external agents this release runs
AGENT_ROUTE = {
    "claude-code": "ug-anthropic",
    "agentcore": "bedrock",
}  # the route each agent's model is reached on
AGENT_FIELDS = {"claude-code": ("cli_version",), "agentcore": ("runtime_arn",)}
_CLI_VERSION_RE = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")
# arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime id>. The region is checked against the row's region.
_RUNTIME_ARN_RE = re.compile(
    r"arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/[A-Za-z][A-Za-z0-9_-]{0,99}"
)
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
    agent: str | None = None  # external agents only: their settings as canonical JSON (agent_json)
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

    @field_validator("agent", mode="before")
    @classmethod
    def _agent_as_json(cls, v: object) -> object:
        return agent_json(v) if isinstance(v, Mapping) else v

    @property
    def runnable(self) -> bool:
        """Whether the runner can run this row: a known kind whose fields are all valid."""
        return self.kind in RUNNABLE_KINDS and not problems(self)

    @property
    def agent_fields(self) -> dict[str, str] | None:
        """The agent's settings. {} when there are none, None when the column holds something else than an object of
        text values."""
        if not self.agent:
            return {}
        try:
            data = json.loads(self.agent)
        except ValueError:
            return None
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            return None
        return data

    @property
    def last_test(self) -> ConnectionTest | None:
        return ConnectionTest.from_json(self.test_result)

    def target(self) -> Target:
        return Target(self.route or "", self.endpoint or "", self.model_id or "", self.region or "")

    def to_profile(self, version: int | None) -> ModelProfile:
        """The eval runner's profile for this row. `version` is the Delta version of the table it was read from."""
        if not self.runnable:
            raise ValueError(f"{self.name}: this {self.kind} row is not a profile the runner can run")
        return ModelProfile(
            route=self.route,  # type: ignore[arg-type]
            endpoint=self.endpoint,
            model_id=self.model_id,
            region=self.region,
            kind=self.kind,  # type: ignore[arg-type]
            catalog_version=version,
            harness=self.harness,
            agent=self.agent_fields or None,
            **{"class": self.model_class},
        )


def agent_json(fields: Mapping[str, object] | None) -> str | None:
    """The `agent` column for these settings: canonical JSON, so equal settings are equal text. None for none."""
    if not fields:
        return None
    return json.dumps({str(k): v for k, v in fields.items()}, sort_keys=True, separators=(",", ":"))


RESERVED_NAME = "default"  # `--models default` means the hub.yaml models


def problems(entry: CatalogEntry) -> list[str]:
    """What is wrong with a row before it is written, in plain words. [] when it may be written."""
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
    elif entry.kind == "external_agent":
        out += _agent_problems(entry)
    if entry.kind != "external_agent" and entry.harness != REFERENCE:
        out.append("harness: a model runs on the reference harness")
    if entry.kind != "external_agent" and entry.agent:
        out.append("agent: settings are for external agents only")
    for field in ("price_in", "price_out"):
        value = getattr(entry, field)
        if value is not None and (not math.isfinite(value) or value < 0):
            out.append(f"{field}: give a price of 0 or more, in USD per million tokens")
    return out


def _agent_problems(entry: CatalogEntry) -> list[str]:
    if entry.harness not in HARNESSES:
        known = f"harness: use {' or '.join(HARNESSES)}"
        return [
            known if entry.harness in ("", REFERENCE) else f"{known}; {entry.harness} is not supported yet"
        ]
    out: list[str] = []
    route = AGENT_ROUTE[entry.harness]
    if entry.route != route:
        out.append(f"route: {entry.harness} reaches its model on the {route} route")
    fields = entry.agent_fields
    if fields is None:
        return [*out, "agent: give the agent's settings as a JSON object of text values"]
    extra = sorted(set(fields) - set(AGENT_FIELDS[entry.harness]))
    if extra:
        out.append(
            f"agent: {entry.harness} takes only {', '.join(AGENT_FIELDS[entry.harness])}, not {', '.join(extra)}"
        )
    if entry.harness == "claude-code":
        if not entry.endpoint or not _ENDPOINT_RE.fullmatch(entry.endpoint):
            out.append("endpoint: pick the serving endpoint of the Claude model that Claude Code drives")
        if entry.model_id or entry.region:
            out.append("model_id and region are not used by claude-code")
        if not _CLI_VERSION_RE.fullmatch(fields.get("cli_version", "")):
            out.append("agent.cli_version: give the Claude Code version to install, such as 2.1.314")
    else:
        if not entry.model_id or not _MODEL_ID_RE.fullmatch(entry.model_id):
            out.append("model_id: give the Bedrock model ID or inference profile ID the agent calls")
        if not entry.region or not AWS_REGION.fullmatch(entry.region):
            out.append("region: give the AWS region of the agent runtime, such as us-east-1")
        if entry.endpoint:
            out.append("endpoint is not used by agentcore")
        arn = _RUNTIME_ARN_RE.fullmatch(fields.get("runtime_arn", ""))
        if arn is None:
            out.append(
                "agent.runtime_arn: give the ARN of the agent runtime, such as "
                "arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/<runtime id>"
            )
        elif entry.region and arn.group(1) != entry.region:
            out.append(f"agent.runtime_arn: the runtime is in {arn.group(1)}, not in {entry.region}")
    return out


def arn_region(arn: str | None) -> str | None:
    """The region of an AgentCore runtime ARN, or None when it is not one."""
    found = _RUNTIME_ARN_RE.fullmatch(arn or "")
    return found.group(1) if found else None


def clashes(name: str, hub_models: Iterable[str]) -> bool:
    """Whether the name is a hub.yaml profile. hub.yaml would win, so registration refuses it."""
    return name in set(hub_models)
