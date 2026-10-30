from __future__ import annotations

import re

from skillhub.config import HubConfig

_RUN_KEY = re.compile(r"[a-z0-9_]+")
# Names that reach SQL, REST paths and file paths. Lint's skill rule is stricter.
DEPT_RE = re.compile(r"[a-z0-9_]+")
SKILL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def check_dept(dept: str) -> str:
    if not isinstance(dept, str) or not DEPT_RE.fullmatch(dept):
        raise ValueError(f"invalid department {dept!r}: use lowercase letters, digits and _")
    return dept


def check_skill_name(skill: str) -> str:
    if not isinstance(skill, str) or not SKILL_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name {skill!r}: it must match ^{SKILL_RE.pattern}$")
    return skill


def schema_base(dept: str) -> str:
    check_dept(dept)
    return dept if dept in ("common", "external") else f"dept_{dept}"


def live_schema(cfg: HubConfig, dept: str) -> str:
    return f"{cfg.catalog}.{schema_base(dept)}"


def versions_schema(cfg: HubConfig, dept: str) -> str:
    return f"{cfg.catalog}.{schema_base(dept)}_versions"


def evals_schema(cfg: HubConfig, dept: str) -> str:
    return f"{cfg.catalog}.{schema_base(dept)}_evals"


def eval_schema(cfg: HubConfig) -> str:
    return f"{cfg.catalog}.eval"


def run_schema(cfg: HubConfig, run_key: str, dept: str) -> str:
    if not isinstance(run_key, str) or not _RUN_KEY.fullmatch(run_key):
        raise ValueError(f"invalid run key {run_key!r}: use lowercase letters, digits and _")
    return f"{cfg.catalog}.run_{run_key}_{schema_base(dept)}"


def live_fqn(cfg: HubConfig, dept: str, skill: str) -> str:
    return f"{live_schema(cfg, dept)}.{check_skill_name(skill)}"


def version_fqn(cfg: HubConfig, dept: str, skill: str, n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"invalid version {n!r}: use a positive whole number")
    return f"{versions_schema(cfg, dept)}.{check_skill_name(skill)}-v{n}"


def workspace_root(cfg: HubConfig) -> str:
    """The hub's workspace folder (hub.yaml `workspace_root`), shared by every catalog. The bundle sets it to the
    ci principal's home folder: folders under /Workspace/Shared inherit `users` CAN_MANAGE, which no lower ACL
    can remove."""
    return cfg.workspace_root


def experiment_root(cfg: HubConfig) -> str:
    """Folder of one catalog's skill experiments and CI candidates. The catalog keeps dev and prod apart."""
    return f"{workspace_root(cfg)}/{cfg.catalog}"


CANDIDATES = "candidates"  # a reserved department key (config.py), so no experiment folder has this name


def candidates_root(cfg: HubConfig) -> str:
    """Folder of this catalog's CI candidate uploads, one sub-folder per run key, in /Workspace form: the eval Job
    reads the candidates as files. CI uploads to the same place ($SKILLHUB_WORKSPACE_ROOT/$SKILLHUB_CATALOG)."""
    return f"/Workspace{experiment_root(cfg)}/{CANDIDATES}"


DRAFTS = "drafts"  # a reserved department key too: Skill Hub drafts, apart from the CI candidates


def drafts_root(cfg: HubConfig) -> str:
    """Folder of this catalog's Skill Hub drafts, one sub-folder per draft, in /Workspace form like candidates_root.
    The app principal may create folders here, never in candidates/, so it cannot change a pull request's upload."""
    return f"/Workspace{experiment_root(cfg)}/{DRAFTS}"


def experiment_path(cfg: HubConfig, dept: str, skill: str) -> str:
    return f"{experiment_root(cfg)}/{dept}/{skill}"


# The department's experiment for skills that have no experiment of their own yet: Skill Hub drafts and pull requests
# that add a skill. A skill name cannot start with "_" (SKILL_RE), so no skill experiment has this name.
UNRELEASED = "_unreleased"


def unreleased_experiment_path(cfg: HubConfig, dept: str) -> str:
    """Bootstrap creates it as ci, so no eval identity needs MANAGE on the department folder to create one."""
    return experiment_path(cfg, dept, UNRELEASED)


def experiment_parts(cfg: HubConfig, path: str) -> tuple[str, str | None]:
    """(dept, skill) of a path built by experiment_path for this catalog, with skill None for the department's
    unreleased experiment (unreleased_experiment_path), whose runs name their skill. ValueError for any other path."""
    root = experiment_root(cfg) + "/"
    rest = path[len(root) :].split("/") if path.startswith(root) else []
    if len(rest) != 2 or not all(rest):
        raise ValueError(f"not a skill-hub experiment path under {root}: {path!r}")
    return rest[0], None if rest[1] == UNRELEASED else rest[1]


def experiment_dept(cfg: HubConfig, path: str) -> str:
    """Department of an experiment path built by experiment_path for this catalog."""
    return experiment_parts(cfg, path)[0]


def dataset_table(cfg: HubConfig, dept: str, skill: str) -> str:
    return f"{evals_schema(cfg, dept)}.ds_{check_skill_name(skill).replace('-', '_')}"


def judge_name(base: str, profile: str) -> str:
    return f"{base}__{profile}"


def make_run_key(label: str) -> str:
    """Turn a label such as 'pr12-ab12cd3' into 'pr12_ab12cd3'.

    Every character outside [a-z0-9] becomes '_', and the result is truncated to 40 characters.
    """
    key = re.sub(r"[^a-z0-9]", "_", label.lower())[:40]
    if not re.search(r"[a-z0-9]", key):
        raise ValueError(f"run key label {label!r} has no letters or digits")
    return key
