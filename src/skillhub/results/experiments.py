from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import mlflow
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel
from mlflow import MlflowClient
from mlflow.entities import UnityCatalog

from skillhub.config import HubConfig
from skillhub.naming import (
    CANDIDATES,
    evals_schema,
    experiment_parts,
    experiment_path,
    experiment_root,
    workspace_root,
)

log = logging.getLogger(__name__)
ADMINS_GROUP = "admins"
USERS_GROUP = "users"
# Principal application IDs, set on every Job from the bundle variables (resources/jobs.yml).
CI_ENV = "SKILLHUB_CI_PRINCIPAL"
CI_EVAL_ENV = "SKILLHUB_CI_EVAL_PRINCIPAL"
APP_ENV = "SKILLHUB_APP_PRINCIPAL"
DEPLOYER_ENV = "SKILLHUB_DEPLOYER"
_RANK = {"CAN_READ": 1, "CAN_EDIT": 2, "CAN_MANAGE": 3}
_WRITE_PRIVS = {"MODIFY", "ALL_PRIVILEGES"}
_READ_PRIVS = {"SELECT"}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class Drift:
    experiment: str
    principal: str
    kind: str
    detail: str


def configure_mlflow(cfg: HubConfig) -> None:
    """Tracing runs queries through this warehouse; set it before any trace call."""
    os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = cfg.workspace.sql_warehouse_id
    mlflow.set_tracking_uri("databricks")


def _trace_location(cfg: HubConfig, dept: str) -> UnityCatalog:
    catalog, schema = evals_schema(cfg, dept).split(".")
    return UnityCatalog(catalog, schema, table_prefix="traces")


def ensure_experiment(cfg: HubConfig, dept: str, skill: str) -> str:
    """Create <workspace_root>/<catalog>/<dept>/<skill> bound to <d>_evals traces. Idempotent.

    The trace location can only be set at creation, so an existing experiment must already
    point at the same location.
    """
    configure_mlflow(cfg)
    path = experiment_path(cfg, dept, skill)
    location = _trace_location(cfg, dept)
    existing = mlflow.get_experiment_by_name(path)
    if existing is None:
        mlflow.set_experiment(experiment_name=path, trace_location=location)
        existing = mlflow.get_experiment_by_name(path)
    elif getattr(existing, "trace_location", None) is None:
        raise RuntimeError(
            f"{path} exists without a UC trace location; the binding is set only at creation. "
            "Delete the experiment and run bootstrap again."
        )
    elif existing.trace_location != location:
        raise RuntimeError(f"{path} is bound to {existing.trace_location}, expected {location}")
    return existing.experiment_id


def _privs(assignment) -> set[str]:
    return {p.privilege.value for p in (assignment.privileges or []) if p.privilege}


def derived_acl(w, cfg: HubConfig, dept: str) -> dict[str, str]:
    """Principal -> experiment permission, read from the grants on <d>_evals.eval_runs.

    MODIFY is granted per table, not per schema, so eval_runs (the canonical eval write table) is
    the securable. MODIFY and ALL_PRIVILEGES map to CAN_EDIT (CI must write runs when it doesn't
    own the experiment); SELECT maps to CAN_READ.
    """
    acl: dict[str, str] = {}
    token = None
    while True:
        # Effective grants include privileges inherited from the schema and catalog.
        page = w.grants.get_effective(
            securable_type="table", full_name=f"{evals_schema(cfg, dept)}.eval_runs", page_token=token
        )
        for a in page.privilege_assignments or []:
            privs = _privs(a)
            if privs & _WRITE_PRIVS:
                acl[a.principal] = "CAN_EDIT"
            elif privs & _READ_PRIVS:
                acl.setdefault(a.principal, "CAN_READ")
        token = page.next_page_token
        if not token:
            return acl


def _request(w, principal: str, level: str) -> AccessControlRequest:
    lvl = PermissionLevel(level)
    if _UUID.match(principal):  # UC grants name service principals by application ID
        return AccessControlRequest(service_principal_name=principal, permission_level=lvl)
    if "@" in principal:
        return AccessControlRequest(user_name=principal, permission_level=lvl)
    if any(True for _ in w.groups.list(filter=f'displayName eq "{principal}"')):
        return AccessControlRequest(group_name=principal, permission_level=lvl)
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{principal}"')), None)
    if sp is None:
        raise ValueError(f"principal {principal!r} is not a user, group or service principal")
    return AccessControlRequest(service_principal_name=sp.application_id, permission_level=lvl)


def apply_experiment_acl(w, cfg: HubConfig, dept: str, experiment_id: str) -> None:
    """Merge the grant-derived ACL into the experiment (update merges; it doesn't replace owners)."""
    acl = derived_acl(w, cfg, dept)
    if not acl:
        return
    w.permissions.update(
        request_object_type="experiments",
        request_object_id=experiment_id,
        access_control_list=[_request(w, p, lvl) for p, lvl in sorted(acl.items())],
    )


def skill_experiments(cfg: HubConfig) -> list:
    """Every active experiment under this catalog's experiment root (paginated)."""
    root = experiment_root(cfg) + "/"
    client = MlflowClient()
    found, token = [], None
    while True:
        page = client.search_experiments(page_token=token, max_results=1000)
        found += [e for e in page if e.name.startswith(root)]
        token = page.token
        if not token:
            return found


def _env(name: str) -> str | None:
    return (os.environ.get(name) or "").strip() or None


def trusted_run_authors(w=None) -> frozenset[str]:
    """Identities whose MLflow runs sync and the gates accept.

    The ci_eval principal (the skill-eval Job, also for evals the Skill Hub requests) and the bundle deployer
    come from the environment. The caller's own identity is added too: in a Job that is the run identity, and on
    a laptop it is the deployer who ran the eval. The caller could write the eval tables directly, so trusting
    its own runs adds nothing. The app principal is not trusted: it never creates eval runs, only Try
    traces. Runs by anyone else are not trusted.
    """
    names = {v for k in (CI_EVAL_ENV, DEPLOYER_ENV) if (v := _env(k))}
    try:
        if w is None:
            from skillhub.auth import workspace_client

            w = workspace_client()
        me = getattr(w.current_user.me(), "user_name", None)
        if me:
            names.add(me)
    except Exception as e:  # noqa: BLE001 - the env names still apply; an empty set fails closed
        log.warning(
            "could not read the current identity (%s); trusting only %s", type(e).__name__, sorted(names)
        )
    return frozenset(names)


def _dir_request(w, cfg: HubConfig, principal: str, level: str) -> AccessControlRequest:
    if principal == cfg.roles.reviewers:
        return AccessControlRequest(group_name=principal, permission_level=PermissionLevel(level))
    return _request(w, principal, level)


def _dir_id(w, path: str) -> str:
    return str(w.workspace.get_status(path).object_id)


def _set_dir_acl(w, cfg: HubConfig, path: str, acl: dict[str, str]) -> str:
    """Replace the direct ACL of a workspace folder. Inherited entries are not changed by this call."""
    object_id = _dir_id(w, path)
    w.permissions.set(
        request_object_type="directories",
        request_object_id=object_id,
        access_control_list=[_dir_request(w, cfg, p, lvl) for p, lvl in sorted(acl.items())],
    )
    return object_id


def _level(p) -> str:
    return getattr(p.permission_level, "value", str(p.permission_level))


def _foreign_inherited(
    perms, allowed_sources: set[str], allowed: dict[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """(principal, level, source) for every permission inherited from an object outside allowed_sources.

    `admins` is never reported, and neither is a principal of `allowed` inheriting no more than its level there:
    the ci principal inherits CAN_MANAGE from its own home folder, which holds the default workspace root."""
    out = []
    for e in perms.access_control_list or []:
        principal = _entry_principal(e)
        if principal == ADMINS_GROUP:
            continue
        for p in e.all_permissions or []:
            sources = set(getattr(p, "inherited_from_object", None) or [])
            if not getattr(p, "inherited", False) or (sources and sources <= allowed_sources):
                continue
            level = _level(p)
            if allowed and principal in allowed and _RANK.get(level, 9) <= _RANK[allowed[principal]]:
                continue
            out.append((principal, level, ", ".join(sorted(sources)) or "a parent folder"))
    return out


def root_acl(cfg: HubConfig, *, ci: str, ci_eval: str, app: str | None) -> dict[str, str]:
    """The direct ACL of <workspace_root>/<catalog>. `users` gets nothing.

    The app keeps CAN_EDIT: the Skill Hub Try panel writes its traces to the skill experiments. Its runs are not
    trusted by sync or the gates."""
    acl = {ci: "CAN_MANAGE", ci_eval: "CAN_EDIT", cfg.roles.reviewers: "CAN_READ"}
    if app:
        acl[app] = "CAN_EDIT"
    return acl


def _base_acl(*, ci: str, ci_eval: str) -> dict[str, str]:
    """The direct ACL of <workspace_root>, which every catalog (dev and prod) shares."""
    return {ci: "CAN_MANAGE", ci_eval: "CAN_EDIT"}


def ensure_experiment_root(cfg: HubConfig, w, *, ci: str, ci_eval: str, app: str | None = None) -> list[str]:
    """Create the hub folders and replace their ACLs. Bootstrap runs it; it needs CAN_MANAGE.

    <workspace_root> is shared by every catalog, so it gets only the two CI principals (the same in dev and
    prod). <workspace_root>/<catalog> adds this target's app principal and the reviewers; it holds the department
    experiment folders and the CI candidates. Folder ACLs only add to what a folder inherits: every folder under
    /Workspace/Shared inherits `users` CAN_MANAGE, so the bundle puts the root in the ci principal's home
    folder. An inherited grant beyond these ACLs is warned about, and check-permissions reports it.
    """
    app = app or _env(APP_ENV)
    base = workspace_root(cfg)
    root = experiment_root(cfg)
    for path in (root, f"{root}/{CANDIDATES}", *(f"{root}/{dept}" for dept in cfg.departments)):
        w.workspace.mkdirs(path)
    want = root_acl(cfg, ci=ci, ci_eval=ci_eval, app=app)
    # The root first: a caller that is not an admin may lose its own access when the base is replaced.
    root_id = _set_dir_acl(w, cfg, root, want)
    base_id = _set_dir_acl(w, cfg, base, _base_acl(ci=ci, ci_eval=ci_eval))
    lines = [
        f"experiment root {root}: CAN_MANAGE ci, CAN_EDIT ci_eval and app, CAN_READ {cfg.roles.reviewers}"
    ]
    if not app:
        msg = f"{APP_ENV} is not set: the Skill Hub app has no access to {root}; set it and re-run bootstrap"
        log.warning(msg)
        lines.append(f"warning: {msg}")
    perms = w.permissions.get(request_object_type="directories", request_object_id=root_id)
    for principal, level, source in _foreign_inherited(
        perms, {f"/directories/{base_id}", f"/directories/{root_id}"}, want
    ):
        msg = (
            f"{principal} has {level} on {root}, inherited from {source}: they can edit runs and read traces. "
            "Set workspace_root (SKILLHUB_WORKSPACE_ROOT) to a folder outside /Workspace/Shared, such as the ci "
            "principal's home folder, or have a workspace admin remove that grant on the parent folder"
        )
        log.warning(msg)
        lines.append(f"warning: {msg}")
    return lines


def _entry_principal(e) -> str | None:
    return e.user_name or e.group_name or e.service_principal_name


def _exempt(p, exempt: set[str]) -> bool:
    """A permission inherited only from the experiment root or above; check_root_acl checks those once."""
    sources = set(getattr(p, "inherited_from_object", None) or [])
    return bool(getattr(p, "inherited", False) and sources and sources <= exempt)


def _entry_rank(e, exempt: set[str] = frozenset()) -> int:
    return max(
        (
            _RANK.get(p.permission_level.value, 0)
            for p in (e.all_permissions or [])
            if p.permission_level and not _exempt(p, exempt)
        ),
        default=0,
    )


def _owners(experiment) -> set[str]:
    tags = getattr(experiment, "tags", None) or {}
    return {v for k in ("mlflow.ownerEmail", "mlflow.ownerId") if (v := tags.get(k))}


def check_experiment_acl(
    w,
    cfg: HubConfig,
    dept: str,
    path: str,
    experiment_id: str,
    owners: set[str] | None = None,
    exempt: set[str] = frozenset(),
) -> list[Drift]:
    """Compare the experiment ACL with the effective UC grants. `admins` and the tag-derived owner
    (at CAN_MANAGE only) are exempt; anyone else outside the grants is `unexpected`, and anyone
    holding more than their grant implies is `excessive`. Permissions inherited only from the objects in
    `exempt` (the experiment root and its parents) are left to check_root_acl."""
    want = derived_acl(w, cfg, dept)
    perms = w.permissions.get(request_object_type="experiments", request_object_id=experiment_id)
    have = {_entry_principal(e): _entry_rank(e, exempt) for e in perms.access_control_list or []}
    drifts: list[Drift] = []
    for principal, level in want.items():
        if have.get(principal, 0) < _RANK[level]:
            drifts.append(Drift(path, principal, "missing", f"UC grants {level}; experiment lacks it"))
    for principal, rank in have.items():
        if rank == 0 or principal == ADMINS_GROUP:
            continue
        # Tags are writable by anyone with CAN_EDIT, so they only exempt the owner's CAN_MANAGE entry.
        if principal in (owners or set()) and rank == _RANK["CAN_MANAGE"]:
            continue
        if principal not in want:
            drifts.append(Drift(path, principal, "unexpected", "has experiment access but no UC grant"))
        elif rank > _RANK[want[principal]]:
            drifts.append(
                Drift(path, principal, "excessive", f"UC grants {want[principal]}; experiment allows more")
            )
    return drifts


def _ancestors(path: str) -> list[str]:
    """/a/b/c -> [/a/b, /a]."""
    parts = path.strip("/").split("/")
    return ["/" + "/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


def _parent_ids(w, cfg: HubConfig) -> set[str]:
    """Permission sources at or above the experiment root, as `/directories/<id>`. A folder this identity
    cannot see is left out."""
    root = experiment_root(cfg)
    out: set[str] = set()
    for path in (root, *_ancestors(root)):
        try:
            out.add(f"/directories/{_dir_id(w, path)}")
        except (NotFound, PermissionDenied):
            continue
    return out


def check_root_acl(cfg: HubConfig, w) -> list[Drift]:
    """Drift on <workspace_root>/<catalog>: a missing root, a grant inherited from above
    <workspace_root> beyond root_acl, and `users` or (when the principal IDs are set) anyone outside root_acl."""
    root = experiment_root(cfg)
    try:
        root_id = _dir_id(w, root)
        base_id = _dir_id(w, workspace_root(cfg))
        perms = w.permissions.get(request_object_type="directories", request_object_id=root_id)
    except NotFound:
        return [Drift(root, "-", "missing_root", "the experiment root does not exist; run bootstrap")]
    except PermissionDenied as e:
        return [Drift(root, "-", "unreadable", f"cannot read the folder ACL: {e}")]
    own = {f"/directories/{base_id}", f"/directories/{root_id}"}
    ci, ci_eval = _env(CI_ENV), _env(CI_EVAL_ENV)
    expected = root_acl(cfg, ci=ci, ci_eval=ci_eval, app=_env(APP_ENV)) if ci and ci_eval else None
    drifts = [
        Drift(root, principal, "inherited_access", f"{level} inherited from {source}")
        for principal, level, source in _foreign_inherited(perms, own, expected)
    ]
    flagged = {d.principal for d in drifts}
    for e in perms.access_control_list or []:
        principal = _entry_principal(e)
        if principal in flagged or principal == ADMINS_GROUP or _entry_rank(e, own) == 0:
            continue
        if principal == USERS_GROUP or (expected is not None and principal not in expected):
            drifts.append(Drift(root, principal, "unexpected", "has access to the experiment root"))
    return drifts


def fails_check(d: Drift) -> bool:
    """Whether a drift fails the nightly check-permissions task. `users` inheriting access from above the
    workspace root is reported but does not fail it (re-review N5): only moving workspace_root out of
    /Workspace/Shared, or an admin, can change it, and a Job that fails every night hides real failures."""
    return not (d.kind == "inherited_access" and d.principal == USERS_GROUP)


def check_permissions(cfg: HubConfig, w) -> list[Drift]:
    configure_mlflow(cfg)
    drifts = check_root_acl(cfg, w)
    exempt = _parent_ids(w, cfg)
    for exp in skill_experiments(cfg):
        try:
            dept, _ = experiment_parts(cfg, exp.name)
        except ValueError:
            drifts.append(Drift(exp.name, "-", "unknown_department", exp.name))
            continue
        if dept not in cfg.departments:
            drifts.append(Drift(exp.name, "-", "unknown_department", dept))
            continue
        drifts += check_experiment_acl(w, cfg, dept, exp.name, exp.experiment_id, _owners(exp), exempt)
    return drifts
