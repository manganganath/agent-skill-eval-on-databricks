from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
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
# <workspace_root>/<catalog>/candidates holds the CI candidate folders, next to the department experiment folders.
_RESERVED_KEYS = ("candidates",)
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
            raise ValueError(f"department key {key!r} is reserved: the CI candidate folders use that name")
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


class ModelProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    route: Literal["serving", "ug-anthropic"] = "serving"
    endpoint: str | None = None
    model: str | None = None
    class_: Literal["frontier", "fast"] = Field(alias="class")
    params: dict[str, object] = {}

    @property
    def provider(self) -> Literal["claude", "gpt", "gemini"]:
        name = (self.endpoint or self.model or "").lower()
        for p in ("claude", "gpt", "gemini"):
            if p in name:
                return p  # type: ignore[return-value]
        raise ConfigError(f"cannot tell the provider from model name '{name}'")


class Defaults(BaseModel):
    models: list[str]
    repeats: int = Field(default=3, ge=1)
    pass_score: float = Field(default=0.7, gt=0, le=1)
    fail_rate: float = Field(default=0.5, ge=0, le=1)
    warn_rate: float = Field(default=0.8, ge=0, le=1)
    max_error_rate: float = Field(default=0.1, ge=0, le=1)
    concurrency_per_endpoint: int = Field(default=8, ge=1)
    min_reviewed_cases: int = Field(default=5, ge=1)


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


def load_config(path: str | Path | None = None) -> HubConfig:
    """Load and validate hub.yaml. The default is $SKILLHUB_CONFIG, else config/hub.yaml."""
    path = path or os.environ.get("SKILLHUB_CONFIG", "config/hub.yaml")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    try:
        cfg = HubConfig.model_validate(_expand(raw))
    except ValidationError as e:
        raise ConfigError(str(e)) from e
    _check_references(cfg)
    return cfg


@dataclass(frozen=True)
class LintSettings:
    departments: frozenset[str]
    defaults: Defaults
    quarantined: frozenset[str] = frozenset()  # departments with quarantine: true


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
    return LintSettings(frozenset(depts), defaults, quarantined)
