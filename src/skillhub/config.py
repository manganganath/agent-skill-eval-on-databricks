from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from skillhub import SkillHubError


class ConfigError(SkillHubError, ValueError):
    """hub.yaml is missing a value, names an unknown profile, or breaks a rule."""


_IDENT = re.compile(r"[a-z0-9_]+")
# ${VAR} or ${VAR:-default}. As in the shell, the default applies when VAR is unset or empty.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# A key ending in one of these would share a schema name with another department: x_versions -> dept_x_versions.
_RESERVED_SUFFIXES = ("_versions", "_evals")
# <workspace_root>/<catalog>/candidates holds the CI candidate folders, and .../drafts the Skill Hub drafts, next to
# the department experiment folders.
_RESERVED_KEYS = ("candidates", "drafts")
# A workspace folder path: absolute, segments of letters, digits, @ . _ + - (user names and application IDs).
_WORKSPACE_PATH = re.compile(r"(?:/[A-Za-z0-9@._+-]+)+")
DEFAULT_WORKSPACE_ROOT = "/Shared/skill-hub"


def check_department_keys(keys: Iterable[str]) -> None:
    """Department keys name schemas, so they are identifiers that cannot collide."""
    keys = list(keys)
    for key in keys:
        if not _IDENT.fullmatch(key):
            raise ValueError(f"department key {key!r} must match ^[a-z0-9_]+$")
        if key.endswith(_RESERVED_SUFFIXES):
            raise ValueError(
                f"department key {key!r} must not end in _versions or _evals: its schemas would collide "
                "with another department's"
            )
        if key in _RESERVED_KEYS:
            raise ValueError(
                f"department key {key!r} is reserved: the candidate and draft folders use that name"
            )
    if "common" not in keys:
        raise ValueError("departments must include common: every department scope sees it")


class Workspace(BaseModel):
    host: str
    sql_warehouse_id: str


class Department(BaseModel):
    readers: list[str]
    quarantine: bool = False


class Roles(BaseModel):
    authors: str
    reviewers: str
    ci: str  # service principal display name or application ID: publishes and switches
    ci_eval: str = "skillhub-ci-eval"  # service principal that only runs evals
    # The group of the external agents' own principals (an agent on Amazon Bedrock AgentCore). Bootstrap lets it
    # write spans to each department's trace table. Optional: bootstrap skips it while the group does not exist.
    agents: str = "skillhub-agents"


# An AWS region name, for example us-east-1 or us-gov-west-1.
AWS_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")
# A Databricks secret scope name.
_SECRET_SCOPE = re.compile(r"[A-Za-z0-9_.@-]{1,128}")

ProfileKind = Literal["hub_yaml", "ug_model", "external_model", "external_agent"]
REFERENCE_HARNESS = "reference"  # the built-in agent loop


class ModelProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    route: Literal["serving", "ug-anthropic", "bedrock"] = "serving"
    endpoint: str | None = None
    model: str | None = None
    class_: Literal["frontier", "fast"] = Field(alias="class")
    params: dict[str, object] = {}
    # Profiles merged from the model catalog (skillhub.model_catalog) set these. hub.yaml profiles keep the defaults.
    kind: ProfileKind = "hub_yaml"
    model_id: str | None = None  # a Bedrock model ID or inference profile ID
    region: str | None = None  # the AWS region of a Bedrock profile
    catalog_version: int | None = None  # the Delta version of the model catalog the profile was read from
    # The harness that runs the cases: the built-in agent loop, or an external agent (claude-code, agentcore), whose
    # own settings are in `agent`, such as {"cli_version": "2.1.314"} or {"runtime_arn": "arn:aws:..."}.
    harness: str = REFERENCE_HARNESS
    agent: dict[str, str] | None = None

    @property
    def provider(self) -> Literal["claude", "gpt", "gemini", "other"]:
        """The model family, from the endpoint, model or model ID. `other` gets no provider-specific parameters."""
        name = (self.endpoint or self.model or self.model_id or "").lower()
        for p in ("claude", "gpt", "gemini"):
            if p in name:
                return p  # type: ignore[return-value]
        return "other"


class BedrockSettings(BaseModel):
    """Where the eval Jobs find the AWS keys for Amazon Bedrock. Only skillhub-ci-eval may read the scope."""

    secret_scope: str = "skillhub-aws"
    default_region: str = "us-east-1"

    @field_validator("secret_scope")
    @classmethod
    def _scope_is_a_name(cls, v: str) -> str:
        if not _SECRET_SCOPE.fullmatch(v):
            raise ValueError(f"external.bedrock.secret_scope {v!r} is not a secret scope name")
        return v

    @field_validator("default_region")
    @classmethod
    def _region_is_a_region(cls, v: str) -> str:
        if not AWS_REGION.fullmatch(v):
            raise ValueError(f"external.bedrock.default_region {v!r} is not an AWS region such as us-east-1")
        return v


class ExternalSettings(BaseModel):
    """External model providers. The models themselves are registered in the model catalog."""

    bedrock: BedrockSettings = BedrockSettings()


_DOMAIN = re.compile(
    r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


class ScanLlmSettings(BaseModel):
    """The LLM reviewer of the security scan: a hub.yaml model profile. Off unless enabled."""

    enabled: bool = False
    model: str | None = None


class ScanSettings(BaseModel):
    """The security scan of skill bundles (`skillhub scan`)."""

    # Hosts a skill may name in a URL; a subdomain of one matches. Any other host is a medium finding. The label with
    # which a reviewer accepts high findings is the repository variable SKILLHUB_SCAN_OVERRIDE_LABEL, not a setting
    # here: a pull request could change this file.
    allowed_domains: list[str] = []
    llm: ScanLlmSettings = ScanLlmSettings()

    @field_validator("allowed_domains")
    @classmethod
    def _domains_are_hosts(cls, v: list[str]) -> list[str]:
        out = [d.strip().lower().removeprefix("*.") for d in v]
        bad = [d for d in out if not _DOMAIN.fullmatch(d)]
        if bad:
            raise ValueError(f"scan.allowed_domains: {', '.join(bad)} is not a host name such as sec.gov")
        return out


class RetentionSettings(BaseModel):
    """How long the results tables keep their write history (`DESCRIBE HISTORY`: who wrote, when). Unset: Delta's
    default, 30 days. The rows themselves are append-only and stay either way."""

    history_days: int | None = Field(default=None, ge=30, le=36500)


class Defaults(BaseModel):
    models: list[str]
    repeats: int = Field(default=3, ge=1)
    pass_score: float = Field(default=0.7, gt=0, le=1)
    fail_rate: float = Field(default=0.5, ge=0, le=1)
    warn_rate: float = Field(default=0.8, ge=0, le=1)
    max_error_rate: float = Field(default=0.1, ge=0, le=1)
    concurrency_per_endpoint: int = Field(default=8, ge=1)
    skill_workers: int = Field(default=6, ge=1)  # skills evaluated at once, each in its own process
    min_reviewed_cases: int = Field(default=5, ge=1)
    # The modes whose verdicts block the PR, merge and publish gates. Every requested mode still runs and is
    # recorded; a mode left out is reported but does not block.
    gating_modes: list[Literal["trigger", "effectiveness"]] = Field(
        default_factory=lambda: ["trigger", "effectiveness"], min_length=1
    )


class ImportSpec(BaseModel):
    source: str  # "github.com/<owner>/<repo>@<sha>"
    map: dict[str, str]  # path glob -> department key; order matters
    exclude: list[str] = []


def normalise_workspace_root(value: str) -> str:
    """The workspace folder that holds the experiments and the candidate folders, in API form: absolute,
    without a trailing slash or the /Workspace prefix. ValueError for anything else."""
    path = value.strip().rstrip("/")
    if path == "/Workspace" or path.startswith("/Workspace/"):
        path = path.removeprefix("/Workspace")
    if not _WORKSPACE_PATH.fullmatch(path) or any(part in (".", "..") for part in path.split("/")):
        raise ValueError(
            f"workspace_root {value!r} must be an absolute workspace folder such as /Users/<ci principal>/skill-hub"
        )
    return path


class HubConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    workspace: Workspace
    catalog: str
    # Experiments: <workspace_root>/<catalog>/<dept>/<skill>. CI candidates: <workspace_root>/<catalog>/candidates.
    workspace_root: str = DEFAULT_WORKSPACE_ROOT
    departments: dict[str, Department]
    roles: Roles
    models: dict[str, ModelProfile]
    judges: list[str]
    judges_pr: list[str]
    generator: str
    defaults: Defaults
    import_: ImportSpec = Field(alias="import")
    external: ExternalSettings = ExternalSettings()
    scan: ScanSettings = ScanSettings()
    retention: RetentionSettings = RetentionSettings()

    @field_validator("catalog")
    @classmethod
    def _catalog_is_an_identifier(cls, v: str) -> str:
        if not _IDENT.fullmatch(v):
            raise ValueError(f"catalog {v!r} must match ^[a-z0-9_]+$")
        return v

    @field_validator("workspace_root")
    @classmethod
    def _workspace_root_is_a_folder(cls, v: str) -> str:
        return normalise_workspace_root(v)

    @field_validator("departments")
    @classmethod
    def _department_keys_are_identifiers(cls, v: dict[str, Department]) -> dict[str, Department]:
        check_department_keys(v)
        return v


def _expand(value: object) -> object:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if default is not None:
                return os.environ.get(name) or default
            if name not in os.environ:
                raise ConfigError(f"environment variable {name} is not set (used by hub.yaml)")
            return os.environ[name]

        return _VAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _check_references(cfg: HubConfig) -> None:
    refs = [
        ("judges", cfg.judges),
        ("judges_pr", cfg.judges_pr),
        ("defaults.models", cfg.defaults.models),
        ("generator", [cfg.generator]),
    ]
    for label, names in refs:
        for name in names:
            if name not in cfg.models:
                raise ConfigError(f"{label} names unknown model profile '{name}'")
    if cfg.defaults.warn_rate < cfg.defaults.fail_rate:
        raise ConfigError("defaults.warn_rate must not be below defaults.fail_rate")
    _check_scan_model(cfg.scan, cfg.models)
    for name, p in cfg.models.items():
        # hub.yaml holds the release-gate profiles, served by Databricks. The model catalog holds the rest.
        if (
            p.kind != "hub_yaml"
            or p.route == "bedrock"
            or p.model_id
            or p.region
            or p.catalog_version is not None
            or p.harness != REFERENCE_HARNESS
            or p.agent is not None
        ):
            raise ConfigError(
                f"models.{name}: hub.yaml profiles are served by Databricks on the reference harness; register "
                "external models and agents in the Skill Hub model catalog"
            )


def _check_scan_model(scan: ScanSettings, models: Iterable[str]) -> None:
    if scan.llm.enabled and not scan.llm.model:
        raise ConfigError("scan.llm.model must name a model profile when scan.llm.enabled is true")
    if scan.llm.model and scan.llm.model not in models:
        raise ConfigError(f"scan.llm.model names unknown model profile '{scan.llm.model}'")


def _sdk_host() -> str:
    from skillhub.auth import workspace_client

    return workspace_client().config.host or ""


def _resolve_host(host: str) -> str:
    """The workspace URL. A Databricks Job gets DATABRICKS_HOST="" from the bundle, so an empty value means the host
    the SDK resolved (the runtime's own). A bare hostname gets https://."""
    host = (host or "").strip() or _sdk_host().strip()
    if not host:
        raise ConfigError(
            "the workspace host is unknown: set DATABRICKS_HOST (used by hub.yaml workspace.host)"
        )
    if "://" not in host:
        host = f"https://{host}"
    return host.rstrip("/")


def load_config(path: str | Path | None = None) -> HubConfig:
    """Load and validate hub.yaml. The default is $SKILLHUB_CONFIG, else config/hub.yaml."""
    path = path or os.environ.get("SKILLHUB_CONFIG", "config/hub.yaml")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    try:
        cfg = HubConfig.model_validate(_expand(raw))
    except ValidationError as e:
        raise ConfigError(str(e)) from e
    cfg.workspace.host = _resolve_host(cfg.workspace.host)
    _check_references(cfg)
    return cfg


@dataclass(frozen=True)
class LintSettings:
    departments: frozenset[str]
    defaults: Defaults
    quarantined: frozenset[str] = frozenset()  # departments with quarantine: true
    models: frozenset[str] = frozenset()  # the hub.yaml model profile names
    scan: ScanSettings = field(default_factory=ScanSettings)


def load_lint_settings(path: str | Path | None = None) -> LintSettings:
    """The parts of hub.yaml that lint and changed-skills need: department keys, quarantine and verdict defaults.

    No ${VARS} are expanded, so lint runs without a workspace or its environment variables.
    """
    path = path or os.environ.get("SKILLHUB_CONFIG", "config/hub.yaml")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    try:
        depts = {k: Department.model_validate(v) for k, v in (raw.get("departments") or {}).items()}
        check_department_keys(depts)
        defaults = Defaults.model_validate(raw.get("defaults") or {})
    except (ValueError, AttributeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if defaults.warn_rate < defaults.fail_rate:
        raise ConfigError("defaults.warn_rate must not be below defaults.fail_rate")
    quarantined = frozenset(k for k, d in depts.items() if d.quarantine)
    models = raw.get("models") or {}
    if not isinstance(models, dict):
        raise ConfigError(f"{path}: models must be a mapping of profile names")
    try:
        scan = ScanSettings.model_validate(raw.get("scan") or {})
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    _check_scan_model(scan, models)
    return LintSettings(frozenset(depts), defaults, quarantined, frozenset(models), scan)
