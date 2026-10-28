from __future__ import annotations

import importlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from databricks.sdk.errors import DatabricksError, NotFound, PermissionDenied
from databricks.sdk.service.iam import (
    AccessControlRequest,
    ComplexValue,
    Patch,
    PatchOp,
    PatchSchema,
    PermissionLevel,
)

from skillhub import SkillHubError
from skillhub.bundle import discover_bundles
from skillhub.config import HubConfig
from skillhub.naming import eval_schema, evals_schema, live_schema, versions_schema

_log = logging.getLogger(__name__)

_PRINCIPAL = re.compile(r"[A-Za-z0-9_.@-]+")
_IDENT = re.compile(r"[a-z0-9_]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class BootstrapError(SkillHubError):
    """Bootstrap cannot proceed. The message says what the deployer should do."""


def _q(principal: str) -> str:
    if not _PRINCIPAL.fullmatch(principal):
        raise BootstrapError(f"unsupported principal name {principal!r}")
    return f"`{principal}`"


def required_groups(cfg: HubConfig) -> list[str]:
    names = {cfg.roles.authors, cfg.roles.reviewers}
    for dept in cfg.departments.values():
        names.update(dept.readers)
    return sorted(names)


def _check_ident(kind: str, value: str) -> None:
    if not _IDENT.fullmatch(value):
        raise BootstrapError(f"{kind} {value!r} must match ^[a-z0-9_]+$")


def _ci_pair(ci_principal: str, ci_eval_principal: str) -> tuple[str, str]:
    ci, ci_eval = _q(ci_principal), _q(ci_eval_principal)
    if ci == ci_eval:
        raise BootstrapError("roles.ci and roles.ci_eval must be different principals")
    return ci, ci_eval


def grant_plan(cfg: HubConfig, ci_principal: str, ci_eval_principal: str) -> list[str]:
    """Schemas and grants from config. Grants are on schemas, so every skill inherits them.

    The two CI principals are given by application ID (UC identifies principals in GRANT by application
    ID), so a display-name grant cannot come back:
      ci       publishes and switches: writes the live and versions volumes and the journal.
      ci_eval  only evaluates: creates run schemas, reads the live and versions volumes to stage copies,
               and reads `<d>_evals`.
    `<d>_evals` gets only schema-level USE SCHEMA and SELECT for both. Table-level MODIFY and ci_eval's CREATE
    TABLE (it owns the `ds_<skill>` tables it creates) come from `table_grant_plan`, after the tables exist.
    A quarantined department is granted to reviewers and the two CI principals only.
    """
    _check_ident("catalog", cfg.catalog)
    for dept in cfg.departments:
        _check_ident("department key", dept)
    cat = cfg.catalog
    authors, reviewers = _q(cfg.roles.authors), _q(cfg.roles.reviewers)
    ci, ci_eval = _ci_pair(ci_principal, ci_eval_principal)
    principals = [_q(p) for p in required_groups(cfg)] + [ci, ci_eval]
    # Revoke privileges that earlier versions granted. REVOKE of a privilege that is not held is a no-op.
    stmts = [f"REVOKE CREATE SCHEMA ON CATALOG {cat} FROM {ci}"]
    stmts += [f"GRANT USE CATALOG ON CATALOG {cat} TO {p}" for p in principals]
    stmts += [
        f"CREATE SCHEMA IF NOT EXISTS {eval_schema(cfg)}",
        f"GRANT USE SCHEMA ON SCHEMA {eval_schema(cfg)} TO {reviewers}",
        f"GRANT SELECT ON SCHEMA {eval_schema(cfg)} TO {reviewers}",
    ]
    for dept, d in cfg.departments.items():
        live, ver, ev = live_schema(cfg, dept), versions_schema(cfg, dept), evals_schema(cfg, dept)
        stmts += [f"CREATE SCHEMA IF NOT EXISTS {s}" for s in (live, ver, ev)]
        # Table-level only now. REVOKE takes a single principal.
        stmts += [f"REVOKE MODIFY ON SCHEMA {ev} FROM {p}" for p in (ci, ci_eval)]
        if d.quarantine:
            skill_readers, ver_readers, ev_readers = [reviewers], [reviewers], [reviewers]
        else:
            skill_readers = [_q(r) for r in d.readers]
            ver_readers = [authors, reviewers]
            ev_readers = skill_readers
        for r in skill_readers:
            stmts += [
                f"GRANT USE SCHEMA ON SCHEMA {live} TO {r}",
                f"GRANT READ VOLUME ON SCHEMA {live} TO {r}",
            ]
        for r in ev_readers:
            stmts += [
                f"GRANT USE SCHEMA ON SCHEMA {ev} TO {r}",
                f"GRANT SELECT ON SCHEMA {ev} TO {r}",
            ]
        for r in ver_readers:
            stmts += [
                f"GRANT USE SCHEMA ON SCHEMA {ver} TO {r}",
                f"GRANT READ VOLUME ON SCHEMA {ver} TO {r}",
            ]
        stmts += [
            f"GRANT USE SCHEMA ON SCHEMA {ev} TO {reviewers}",
            f"GRANT SELECT ON SCHEMA {ev} TO {reviewers}",
            f"GRANT USE SCHEMA, CREATE VOLUME, READ VOLUME, WRITE VOLUME ON SCHEMA {live} TO {ci}",
            f"GRANT USE SCHEMA, CREATE VOLUME, READ VOLUME, WRITE VOLUME ON SCHEMA {ver} TO {ci}",
            f"GRANT USE SCHEMA, SELECT ON SCHEMA {ev} TO {ci}",
            f"GRANT USE SCHEMA, READ VOLUME ON SCHEMA {live} TO {ci_eval}",
            f"GRANT USE SCHEMA, READ VOLUME ON SCHEMA {ver} TO {ci_eval}",
            f"GRANT USE SCHEMA, SELECT ON SCHEMA {ev} TO {ci_eval}",
        ]
    # Staging creates run_* schemas. Granted last, so ci_eval cannot squat a production schema name on a
    # fresh catalog (the owner checks would catch it, but bootstrap would fail).
    stmts.append(f"GRANT CREATE SCHEMA ON CATALOG {cat} TO {ci_eval}")
    return stmts


def hub_schemas(cfg: HubConfig) -> list[str]:
    """Every schema bootstrap creates: `eval`, then live, versions and evals per department."""
    out = [eval_schema(cfg)]
    for dept in cfg.departments:
        out += [live_schema(cfg, dept), versions_schema(cfg, dept), evals_schema(cfg, dept)]
    return out


def results_schemas(cfg: HubConfig) -> list[str]:
    """The schemas that hold results tables and views."""
    return [eval_schema(cfg), *(evals_schema(cfg, d) for d in cfg.departments)]


# Tables in `<d>_evals` that ci_eval writes. `skill_versions` is deliberately absent.
EVAL_WRITE_TABLES = ("eval_runs", "eval_cases", "rubrics")
JOURNAL_TABLE = "skill_versions"
TRACE_PREFIX = "traces"  # MLflow binds UnityCatalog(table_prefix="traces") on each `<d>_evals`


def trace_table_grants(cfg: HubConfig, sql, ci_eval_principal: str) -> list[str]:
    """ci_eval's access to the MLflow trace objects that `ensure_experiment` created.

    MLflow's Unity Catalog trace storage creates managed tables and views. Tables get MODIFY and SELECT;
    views get SELECT only, because Unity Catalog refuses MODIFY on a view.
    """
    ci_eval = _q(ci_eval_principal)
    stmts: list[str] = []
    for dept in cfg.departments:
        ev = evals_schema(cfg, dept)
        rows = sql.execute(
            f"SELECT table_name, table_type FROM {cfg.catalog}.information_schema.tables "
            f"WHERE table_schema = :schema AND table_name LIKE '{TRACE_PREFIX}%'",
            {"schema": ev.split(".", 1)[1]},
        )
        for row in rows:
            name = row.get("table_name") or ""
            if not name.startswith(TRACE_PREFIX) or not re.fullmatch(r"[A-Za-z0-9_]+", name):
                raise BootstrapError(f"unexpected trace table name {name!r} in {ev}")
            is_view = "VIEW" in str(row.get("table_type") or "").upper()
            privileges = "SELECT" if is_view else "MODIFY, SELECT"
            stmts.append(f"GRANT {privileges} ON TABLE {ev}.{name} TO {ci_eval}")
    return stmts


def _deployer(w) -> str:
    return w.current_user.me().user_name


@dataclass(frozen=True)
class _Owners:
    """Who may own a hub schema, results table or view: the deployer and the ci principal, never ci_eval."""

    deployer: str
    ci: str
    ci_eval: str

    def check(self, kind: str, name: str, owner: str | None) -> None:
        if owner == self.ci_eval or owner not in (self.deployer, self.ci):
            raise BootstrapError(
                f"{kind} {name} is owned by {owner!r}; bootstrap accepts only the deploying principal "
                f"{self.deployer!r} or the ci principal {self.ci!r}. It may have been created by another "
                "principal before bootstrap. Drop it or change its owner, then re-run bootstrap."
            )


def _schema_owner(w, name: str) -> str | None:
    """The owner, or None when the schema does not exist."""
    try:
        return w.schemas.get(name).owner
    except NotFound:
        return None


_TABLE_NAME = re.compile(r"[A-Za-z0-9_]+")


def ownership_transfers(
    cfg: HubConfig, w, owners: _Owners, schema_owners: dict[str, str | None]
) -> list[str]:
    """ALTER ... OWNER TO ci for every hub schema, results table and view the deployer still owns.

        Then the skillhub-bootstrap Job, which runs as ci, passes the owner checks and may re-apply the grants
    . Tables owned by ci_eval (the `ds_<skill>` datasets) keep their owner.
    """
    ci = _q(owners.ci)
    stmts: list[str] = []
    for schema in results_schemas(cfg):
        cat, _, sch = schema.partition(".")
        try:
            tables = list(w.tables.list(catalog_name=cat, schema_name=sch))
        except NotFound:
            continue
        except PermissionDenied as e:  # a schema ci already owns: its tables were handed over with it
            _log.warning("could not list the tables in %s (%s); they keep their owner", schema, e)
            continue
        for t in tables:
            if t.owner != owners.deployer:
                continue
            if not _TABLE_NAME.fullmatch(t.name or ""):
                raise BootstrapError(f"unexpected table name {t.name!r} in {schema}")
            kind = getattr(t.table_type, "value", t.table_type)
            if kind in (None, "MANAGED", "EXTERNAL"):
                stmts.append(f"ALTER TABLE {schema}.{t.name} OWNER TO {ci}")
            elif kind == "VIEW":
                stmts.append(f"ALTER VIEW {schema}.{t.name} OWNER TO {ci}")
            else:
                _log.warning(
                    "left %s.%s (%s) with its owner: bootstrap does not create that kind",
                    schema,
                    t.name,
                    kind,
                )
    stmts += [
        f"ALTER SCHEMA {s} OWNER TO {ci}" for s in hub_schemas(cfg) if schema_owners.get(s) == owners.deployer
    ]
    return stmts


def table_grant_plan(cfg: HubConfig, ci_principal: str, ci_eval_principal: str) -> list[str]:
    """Table-level MODIFY grants on `<d>_evals`. UC has no DENY, so write access is per table.

    Apply these after the results step has created the tables and the owners were checked. Schema-level
    grants come from `grant_plan`.
    """
    _check_ident("catalog", cfg.catalog)
    ci, ci_eval = _ci_pair(ci_principal, ci_eval_principal)
    stmts: list[str] = []
    for dept in cfg.departments:
        _check_ident("department key", dept)
        ev = evals_schema(cfg, dept)
        stmts.append(
            f"GRANT CREATE TABLE ON SCHEMA {ev} TO {ci_eval}"
        )  # after ensure_tables: cannot pre-create
        stmts.append(f"GRANT MODIFY ON TABLE {ev}.{JOURNAL_TABLE} TO {ci}")
        stmts += [f"GRANT MODIFY ON TABLE {ev}.{t} TO {ci_eval}" for t in EVAL_WRITE_TABLES]
    return stmts


CI_ENTITLEMENTS = ("workspace-access", "databricks-sql-access")


def print_identities(cfg: HubConfig) -> str:
    lines = [
        "Account groups to create and assign to this workspace (an account admin does this; Unity Catalog "
        "cannot grant to workspace-local groups):"
    ]
    lines += [f"  {g}" for g in required_groups(cfg)]
    for name in (cfg.roles.ci, cfg.roles.ci_eval):
        if _UUID.fullmatch(name):
            lines.append(f"Service principal that must already exist (application ID): {name}")
        else:
            lines.append(f"Service principal to create (display name): {name}")
        lines.append(f"  entitlements: {', '.join(CI_ENTITLEMENTS)}")
    lines.append("Then run: skillhub bootstrap --no-create-identities")
    return "\n".join(lines)


def _check_catalog(cfg: HubConfig, w):
    try:
        return w.catalogs.get(cfg.catalog)
    except NotFound as e:
        raise BootstrapError(
            f"catalog '{cfg.catalog}' was not found, or you cannot see it. Set `catalog` in config/hub.yaml "
            "to a catalog you own (or ask an admin to create one), then re-run bootstrap."
        ) from e


def _check_name(name: str) -> str:
    """Names go into SCIM filters, so refuse anything (such as a quote) that could change the filter."""
    if not _PRINCIPAL.fullmatch(name):
        raise BootstrapError(f"unsupported principal name {name!r}")
    return name


# Account groups through the workspace's account SCIM proxy, and workspace assignment .
# Unity Catalog grants only to account principals: a workspace-local group gets PRINCIPAL_DOES_NOT_EXIST.
ACCOUNT_GROUPS_PATH = "/api/2.0/account/scim/v2/Groups"
ASSIGNMENTS_PATH = "/api/2.0/preview/permissionassignments"
_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_JSON = {"Accept": "application/json", "Content-Type": "application/json"}
_SCIM_ID = re.compile(r"[0-9]+")


def _resource_type(obj) -> str | None:
    meta = obj.get("meta") if isinstance(obj, dict) else getattr(obj, "meta", None)
    if isinstance(meta, dict):
        return meta.get("resourceType")
    return getattr(meta, "resource_type", None)


def _find_group(w, name: str):
    """The workspace's view of the group: an assigned account group ("Group") or a workspace-local one."""
    return next(iter(w.groups.list(filter=f'displayName eq "{_check_name(name)}"')), None)


def _find_account_group(w, name: str) -> dict | None:
    res = w.api_client.do(
        "GET", ACCOUNT_GROUPS_PATH, query={"filter": f'displayName eq "{_check_name(name)}"'}, headers=_JSON
    )
    found = [g for g in (res or {}).get("Resources") or [] if g.get("displayName") == name]
    if len(found) > 1:
        raise BootstrapError(f"{len(found)} account groups are named {name!r}; remove the duplicates")
    if found and _resource_type(found[0]) not in (None, "Group"):
        raise BootstrapError(f"account SCIM returned {name!r} as {_resource_type(found[0])!r}, not a Group")
    return found[0] if found else None


def _scim_id(group: dict, name: str) -> str:
    gid = str(group.get("id") or "")
    if not _SCIM_ID.fullmatch(gid):
        raise BootstrapError(f"account group {name!r} has an unexpected id {gid!r}")
    return gid


def _assigned_principal_ids(w) -> set[str]:
    res = w.api_client.do("GET", ASSIGNMENTS_PATH, headers=_JSON)
    ids = set()
    for a in (res or {}).get("permission_assignments") or []:
        pid = (a.get("principal") or {}).get("principal_id")
        if pid is not None and a.get("permissions"):
            ids.add(str(pid))
    return ids


@dataclass
class _Group:
    name: str
    account: dict | None  # the account group, or None when it does not exist yet
    assigned: bool  # assigned to this workspace


def _group_state(w, names: list[str], create: bool) -> list[_Group]:
    """Each required group as an account group and its workspace assignment. Refuses workspace-local groups.

    Reading account groups and assignments needs a workspace admin. Without --create-identities (the
    bootstrap Job runs as ci, which is not an admin) a refusal falls back to the workspace's SCIM view, where
    an assigned account group has resourceType "Group".
    """
    try:
        seen = {n: _find_group(w, n) for n in names}
    except PermissionDenied as e:
        if create:
            raise BootstrapError(
                f"listing workspace groups was refused ({e}). Creating account groups needs a workspace admin."
            ) from e
        _log.warning(
            "cannot list workspace groups (%s): the groups are not checked here. A missing one makes a GRANT "
            "fail with PRINCIPAL_DOES_NOT_EXIST; run bootstrap as a workspace admin to check them.",
            e,
        )
        return [_Group(n, {"id": None}, True) for n in names]  # unverified, assumed present
    local = [n for n, g in seen.items() if _resource_type(g) == "WorkspaceGroup"]
    if local:
        raise BootstrapError(
            f"workspace-local group(s) {', '.join(local)} have the names of required groups. Unity Catalog cannot "
            "grant to workspace-local groups. Delete them (Settings > Identity and access > Groups), create "
            "account groups with the same names, then re-run bootstrap."
        )
    try:
        assigned = _assigned_principal_ids(w)
        accounts = {n: _find_account_group(w, n) for n in names}
    except PermissionDenied as e:
        if create:
            raise BootstrapError(
                f"reading account groups and workspace assignments was refused ({e}). Creating and assigning "
                "account groups needs a workspace admin; run bootstrap as one, or without --create-identities."
            ) from e
        _log.info("account groups are not readable here (%s); checking the workspace view of each group", e)
        return [
            _Group(n, {"id": getattr(g, "id", None)} if g else None, g is not None) for n, g in seen.items()
        ]
    return [_Group(n, a, a is not None and _scim_id(a, n) in assigned) for n, a in accounts.items()]


def _create_and_assign(w, g: _Group, log: list[str]) -> None:
    if g.account is None:
        g.account = w.api_client.do(
            "POST",
            ACCOUNT_GROUPS_PATH,
            body={"schemas": [_GROUP_SCHEMA], "displayName": g.name},
            headers=_JSON,
        )
        log.append(f"created account group {g.name}")
    if not g.assigned:
        w.api_client.do(
            "PUT",
            f"{ASSIGNMENTS_PATH}/principals/{_scim_id(g.account, g.name)}",
            body={"permissions": ["USER"]},
            headers=_JSON,
        )
        g.assigned = True
        log.append(f"assigned account group {g.name} to this workspace")


def _find_sp(w, name: str):
    """The service principal with this display name (or application ID when `name` is a UUID)."""
    attr = "applicationId" if _UUID.fullmatch(name) else "displayName"
    found = list(w.service_principals.list(filter=f'{attr} eq "{_check_name(name)}"'))
    if len(found) > 1:
        raise BootstrapError(f"{len(found)} service principals match {name!r}; remove the duplicates")
    return found[0] if found else None


class SecretOutError(BootstrapError):
    """A secret output option is missing or unusable. Raised before anything is created."""


def _open_secret_file(path: Path | None, sp_name: str, flag: str = "--secret-out"):
    """Create the secret file exclusively with mode 0600. Refuses an existing path or missing parent."""
    if path is None:
        raise SecretOutError(f"a new OAuth secret is needed for {sp_name}: pass {flag} PATH")
    if not path.parent.is_dir():
        raise SecretOutError(f"{flag} parent directory does not exist: {path.parent}")
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as e:
        raise SecretOutError(f"{flag} {path} already exists; refusing to overwrite it") from e
    except OSError as e:
        raise SecretOutError(f"cannot create {flag} {path}: {e.strerror}") from e


def _is_active(secret) -> bool:
    """An expired secret counts as absent. An unreadable expiry counts as active (never mint extras)."""
    expire = getattr(secret, "expire_time", None)
    if not isinstance(expire, str) or not expire:
        return True
    try:
        when = datetime.fromisoformat(expire.replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when > datetime.now(UTC)


def _create_secret(w, sp_id: str):
    """Create a secret with the SDK logger held at INFO so debug logging cannot record the value."""
    sdk_log = logging.getLogger("databricks.sdk")
    old = sdk_log.level
    if old < logging.INFO:
        sdk_log.setLevel(logging.INFO)
    try:
        return w.service_principal_secrets_proxy.create(service_principal_id=sp_id)
    finally:
        sdk_log.setLevel(old)


def _ensure_entitlements(w, i, create: bool, log: list[str]) -> None:
    """Give an existing CI principal the entitlements it needs, or warn when we may not change it."""
    have = {getattr(e, "value", None) for e in (getattr(i.sp, "entitlements", None) or [])}
    lacking = [e for e in CI_ENTITLEMENTS if e not in have]
    if not lacking:
        return
    if not create:
        _log.warning(
            "service principal %s lacks the entitlements %s; its API calls will fail until an admin adds them "
            "(re-run with --create-identities, or add them in Settings > Identity and access)",
            i.name,
            ", ".join(lacking),
        )
        return
    w.service_principals.patch(
        i.sp.id,
        operations=[Patch(op=PatchOp.ADD, path="entitlements", value=[{"value": e} for e in lacking])],
        schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
    )
    log.append(f"added entitlements {', '.join(lacking)} to service principal {i.name}")


@dataclass
class _Identity:
    """One CI service principal and the secret file it needs."""

    role: str  # config key, for messages: roles.ci or roles.ci_eval
    name: str  # display name or application ID from config
    flag: str  # the CLI flag that names its secret file
    secret_out: Path | None
    sp: Any = None
    need_secret: bool = False
    fd: int | None = None
    written: bool = False


def _ensure_identities(
    cfg: HubConfig,
    w,
    create: bool,
    secret_out: Path | None,
    eval_secret_out: Path | None = None,
    log: list[str] | None = None,
) -> tuple[list[str], str, str]:
    """Create or check groups and both CI principals. Returns (log lines, ci app ID, ci_eval app ID)."""
    log = [] if log is None else log
    groups = _group_state(w, required_groups(cfg), create)
    ids = [
        _Identity("roles.ci", cfg.roles.ci, "--secret-out", secret_out),
        _Identity("roles.ci_eval", cfg.roles.ci_eval, "--eval-secret-out", eval_secret_out),
    ]
    missing = [
        f"account group {g.name}" if g.account is None else f"workspace assignment of account group {g.name}"
        for g in groups
        if g.account is None or not g.assigned
    ]
    for i in ids:
        i.sp = _find_sp(w, i.name)
        if i.sp is None and _UUID.fullmatch(i.name):
            raise BootstrapError(
                f"{i.role} is the application ID {i.name}, but no service principal has it. "
                f"Add that service principal to the workspace (it cannot be created by application ID), "
                f"or set {i.role} to a display name."
            )
        if i.sp is None:
            missing.append(f"service principal {i.name}")
    if all(i.sp is not None for i in ids) and str(ids[0].sp.id) == str(ids[1].sp.id):
        raise BootstrapError(
            "roles.ci and roles.ci_eval resolve to the same service principal; they must differ"
        )
    if missing and not create:
        raise BootstrapError(
            "missing identities: " + ", ".join(missing) + ". Ask an admin to create them (groups are "
            "account groups assigned to this workspace; `skillhub bootstrap --print-identities` lists them), "
            "or re-run with --create-identities if you have the rights. Nothing was granted."
        )
    for i in ids:
        # A secret is needed when the principal is new or has no unexpired one (this also repairs a half-run).
        i.need_secret = create and (
            i.sp is None
            or not any(
                _is_active(x)
                for x in w.service_principal_secrets_proxy.list(service_principal_id=str(i.sp.id))
            )
        )
    try:
        for i in ids:  # open every file before creating anything
            if i.need_secret:
                i.fd = _open_secret_file(i.secret_out, i.name, i.flag)
            elif i.secret_out is not None:
                log.append(f"{i.flag} given but no secret was created; nothing written")
        for g in groups if create else ():  # never a workspace-local group: UC cannot grant to one
            _create_and_assign(w, g, log)
        for i in ids:
            if i.sp is None:
                i.sp = w.service_principals.create(
                    display_name=i.name, entitlements=[ComplexValue(value=e) for e in CI_ENTITLEMENTS]
                )
                log.append(f"created service principal {i.name} (id {i.sp.id})")
            else:
                _ensure_entitlements(w, i, create, log)
            if i.fd is not None:
                _write_new_secret(w, i, log)
    except BaseException as e:
        for i in ids:
            if i.fd is not None:
                os.close(i.fd)
                i.fd = None
                i.secret_out.unlink(missing_ok=True)  # nothing was written yet
        kept = [f"{i.secret_out} (secret for {i.name})" for i in ids if i.written]
        if kept and isinstance(e, Exception):
            raise BootstrapError(
                f"bootstrap stopped ({type(e).__name__}: {e}) after writing: {', '.join(kept)}. "
                "Each of these files holds a live secret: use it or delete it, then re-run bootstrap."
            ) from e
        raise
    app_ids = []
    for i in ids:
        app_id = getattr(i.sp, "application_id", None)
        if not app_id:
            raise BootstrapError(f"service principal {i.name} has no application ID")
        app_ids.append(app_id)
    log.append(f"CI service principal application ID (DATABRICKS_CLIENT_ID): {app_ids[0]}")
    log.append(f"CI eval service principal application ID (DATABRICKS_CLIENT_ID): {app_ids[1]}")
    return log, app_ids[0], app_ids[1]


def _write_new_secret(w, i: _Identity, log: list[str]) -> None:
    try:
        created = _create_secret(w, str(i.sp.id))
    except PermissionDenied as e:
        raise BootstrapError(
            f"{i.name}: The workspace refused to create an OAuth secret through the API. Generate it in the UI: "
            f"Settings → Identity and access → Service principals → {i.name} → Secrets → Generate secret "
            f"(scope: Other APIs → all-apis). Save it to {i.secret_out} with mode 0600, then re-run bootstrap; "
            "a principal with an unexpired secret gets no new one."
        ) from e
    secret_id = getattr(created, "id", None)
    fd, i.fd = i.fd, None
    try:
        with os.fdopen(fd, "w") as f:  # takes ownership of fd and closes it
            fd = None
            f.write(created.secret)
    except Exception as e:
        if fd is not None:
            os.close(fd)
        i.secret_out.unlink(missing_ok=True)
        raise BootstrapError(
            f"the OAuth secret (id {secret_id}) was created but could not be saved "
            f"({type(e).__name__}). Delete secret {secret_id} on service principal "
            f"{i.name}, then re-run bootstrap."
        ) from None
    i.written = True
    log.append(f"wrote OAuth secret for {i.name} to {i.secret_out}")


# Permission levels on a SQL warehouse that include running statements on it.
_WAREHOUSE_USE = frozenset({"CAN_USE", "CAN_MONITOR", "CAN_MANAGE", "IS_OWNER"})


def ensure_warehouse_access(cfg: HubConfig, w, app_ids: list[str], create: bool) -> list[str]:
    """CAN_USE on the SQL warehouse for both CI principals: without it every statement they run fails.

    With `create` the grant is added (the Permissions API PATCH is additive). Otherwise the ACL is only read
    and a missing grant is a warning with the manual step.
    """
    wid = cfg.workspace.sql_warehouse_id
    manual = (
        f"Manual step: give CAN USE on SQL warehouse {wid} to {', '.join(app_ids)} (warehouse > Permissions)."
    )
    if create:
        try:
            w.permissions.update(
                request_object_type="warehouses",
                request_object_id=wid,
                access_control_list=[
                    AccessControlRequest(service_principal_name=a, permission_level=PermissionLevel.CAN_USE)
                    for a in app_ids
                ],
            )
        except DatabricksError as e:
            _log.warning("could not grant CAN_USE on SQL warehouse %s (%s). %s", wid, e, manual)
            return [
                f"warning: CAN_USE on SQL warehouse {wid} was not granted; see the log for the manual step"
            ]
        return [f"SQL warehouse {wid}: CAN_USE for {', '.join(app_ids)}"]
    try:
        acl = w.permissions.get(request_object_type="warehouses", request_object_id=wid)
        have = {
            e.service_principal_name
            for e in (acl.access_control_list or [])
            if any(
                getattr(p.permission_level, "value", None) in _WAREHOUSE_USE
                for p in (e.all_permissions or [])
            )
        }
    except DatabricksError as e:
        _log.warning("could not read the permissions of SQL warehouse %s (%s). %s", wid, e, manual)
        return [f"warning: could not check CAN_USE on SQL warehouse {wid}"]
    lacking = [a for a in app_ids if a not in have]
    if not lacking:
        return []
    _log.warning(
        "%s may lack CAN_USE on SQL warehouse %s; re-run with --create-identities or do it by hand. %s",
        ", ".join(lacking),
        wid,
        manual,
    )
    return [f"warning: {', '.join(lacking)} may lack CAN_USE on SQL warehouse {wid}"]


def _optional(module: str, attr: str) -> Any | None:
    """Return an optional function if its module is present."""
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name and module.startswith(e.name):
            return None
        raise
    return getattr(mod, attr)


def run_hooks(cfg: HubConfig, sql, skills_root: Path | None) -> list[str]:
    ran: list[str] = []
    experiment_ids: list[str] = []
    ensure_experiment = _optional("skillhub.results.experiments", "ensure_experiment")
    if not ensure_experiment:
        _log.warning("skillhub.results.experiments is not available: MLflow experiments were not created")
    else:
        for b in discover_bundles(skills_root):
            experiment_ids.append(ensure_experiment(cfg, b.dept, b.name))
        ran.append("ensure_experiment")
    register_prompts = _optional("skillhub.prompts", "register_prompts")
    if register_prompts:
        register_prompts(cfg)
        ran.append("register_prompts")
    register_judges = _optional("skillhub.eval.judges", "register_judges")
    if register_judges:
        if not experiment_ids:
            _log.warning("no skill experiments to register judges in: judges were not registered")
        for experiment_id in experiment_ids:  # Make_judge judges live in each skill experiment
            register_judges(cfg, experiment_id)
        if experiment_ids:
            ran.append("register_judges")
    return ran


@dataclass(frozen=True)
class ResultsSteps:
    """The results steps. Injectable so unit tests need no results package."""

    ensure_tables: Callable[[Any, Any], Any]
    ensure_views: Callable[[Any, Any], Any]
    ensure_alerts: Callable[[Any, Any], list[str]]
    ensure_genie_space: Callable[[Any, Any], str]
    # (cfg, w, *, ci, ci_eval) -> log lines. Creates the experiment folders and locks their ACLs.
    ensure_experiment_root: Callable[..., list[str]]
    # (w, ids, owner) -> log lines and (w, space_id, owner) -> log line: the person's first run hands the
    # alerts and the Genie space to ci. None: not handed over (unit tests of other steps).
    hand_over_alerts: Callable[..., list[str]] | None = None
    hand_over_genie: Callable[..., str] | None = None


def _required(module: str, attr: str) -> Callable:
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        raise BootstrapError(
            f"bootstrap needs {module}.{attr}, which could not be loaded ({e}). "
            "Install a build of skillhub that includes the results package."
        ) from e


def default_results_steps() -> ResultsSteps:
    """Resolve the results steps at call time. A missing one is an error, never a silent skip."""
    return ResultsSteps(
        ensure_tables=_required("skillhub.results.tables", "ensure_tables"),
        ensure_views=_required("skillhub.results.tables", "ensure_views"),
        ensure_alerts=_required("skillhub.results.alerts", "ensure_alerts"),
        ensure_genie_space=_required("skillhub.results.genie", "ensure_genie_space"),
        ensure_experiment_root=_required("skillhub.results.experiments", "ensure_experiment_root"),
        hand_over_alerts=_required("skillhub.results.alerts", "hand_over_alerts"),
        hand_over_genie=_required("skillhub.results.genie", "hand_over_space"),
    )


def grant_genie_access(cfg: HubConfig, w, space_id: str) -> str:
    """CAN_RUN on the Genie space for the users groups. UC still governs the data.

    Uses the Permissions API object type `genie`. A failure warns with the manual step and does not stop bootstrap.
    """
    groups = sorted({cfg.roles.authors, cfg.roles.reviewers})
    try:
        w.permissions.update(
            request_object_type="genie",
            request_object_id=space_id,
            access_control_list=[
                AccessControlRequest(group_name=g, permission_level=PermissionLevel.CAN_RUN) for g in groups
            ],
        )
    except DatabricksError as e:
        _log.warning(
            "could not set CAN_RUN on Genie space %s (%s). Manual step: open the space, choose Share, "
            "and give CAN RUN to: %s.",
            space_id,
            e,
            ", ".join(groups),
        )
        return f"warning: Genie space {space_id} ACL not set; see log for the manual step"
    return f"Genie space {space_id}: CAN_RUN for {', '.join(groups)}"


class _Log(list):
    """Log lines, each also passed to `emit` as it happens, so a later failure cannot hide it (M6)."""

    def __init__(self, emit: Callable[[str], None] | None) -> None:
        super().__init__()
        self._emit = emit

    def append(self, line: str) -> None:
        super().append(line)
        if self._emit is not None:
            self._emit(line)

    def __iadd__(self, lines):
        for line in lines:
            self.append(line)
        return self


def bootstrap(
    cfg: HubConfig,
    w,
    sql,
    *,
    create_identities: bool,
    skills_root: Path | None = None,
    results: ResultsSteps | None = None,
    secret_out: Path | None = None,
    eval_secret_out: Path | None = None,
    emit: Callable[[str], None] | None = None,
) -> list[str]:
    """Identities, schemas, grants and results artefacts. Safe to re-run.

    Who runs it decides what it does:
    - The first run is by a person who owns the catalog. It does everything, then hands every hub schema,
      results table and view it owns to the ci principal (ALTER ... OWNER TO).
    - Later runs are the skillhub-bootstrap Job, which runs as ci. ci owns the schemas, so it may re-apply the
      grants. It cannot create schemas or, unless it owns the catalog, change catalog-level grants:
      those are skipped and logged.
    - A person re-running it after the hand-over (for identities, or a new department) does the identity
      and catalog steps, creates missing schemas, hands them over, and leaves the rest to the Job.
    Every owner must be the deployer or ci. ci_eval never runs bootstrap and never owns a hub object.
    """
    log_lines = _Log(emit)
    steps = results if results is not None else default_results_steps()  # fail before changing anything
    grant_plan(cfg, cfg.roles.ci, cfg.roles.ci_eval)  # validate every identifier before any change is made
    catalog = _check_catalog(cfg, w)
    _, ci_app_id, ci_eval_app_id = _ensure_identities(
        cfg, w, create_identities, secret_out, eval_secret_out, log=log_lines
    )
    log_lines += ensure_warehouse_access(cfg, w, [ci_app_id, ci_eval_app_id], create_identities)
    deployer = _deployer(w)
    if deployer == ci_eval_app_id:
        raise BootstrapError(
            "bootstrap is running as ci_eval, which only evaluates. Run it as the catalog owner, "
            "or as the skillhub-bootstrap Job (ci)."
        )
    owners = _Owners(deployer, ci_app_id, ci_eval_app_id)
    as_ci = deployer == ci_app_id
    schema_owners = {name: _schema_owner(w, name) for name in hub_schemas(cfg)}
    for name, owner in schema_owners.items():
        if owner is not None:
            owners.check("schema", name, owner)
    missing = [name for name, owner in schema_owners.items() if owner is None]
    if as_ci and missing:
        raise BootstrapError(
            f"schema(s) {', '.join(missing)} do not exist, and ci cannot create schemas. Run "
            "`skillhub bootstrap` as the catalog owner to create them, then re-run this Job."
        )
    handover = not as_ci and any(owner == ci_app_id for owner in schema_owners.values())
    catalog_grants = not as_ci or getattr(catalog, "owner", None) == ci_app_id
    stmts = grant_plan(cfg, ci_app_id, ci_eval_app_id)
    applied = skipped = 0
    for stmt in stmts:
        if stmt.startswith("CREATE SCHEMA IF NOT EXISTS "):
            name = stmt.removeprefix("CREATE SCHEMA IF NOT EXISTS ")
            if schema_owners[name] is None:
                sql.execute(stmt)
                schema_owners[name] = w.schemas.get(name).owner
                owners.check("schema", name, schema_owners[name])  # squatted between the check and the create
                applied += 1
        elif " ON CATALOG " in stmt:
            if not catalog_grants:
                skipped += 1
                continue
            sql.execute(stmt)
            applied += 1
        elif not handover:
            sql.execute(stmt)
            applied += 1
    log_lines.append(f"applied {applied} schema and grant statements")
    if skipped:
        log_lines.append(
            f"skipped {skipped} catalog-level grant statements: ci does not own catalog {cfg.catalog}. "
            "The catalog owner applies them with `skillhub bootstrap`."
        )
    if handover:
        moved = ownership_transfers(cfg, w, owners, schema_owners)
        for stmt in moved:
            sql.execute(stmt)
        log_lines.append(
            f"handed {len(moved)} schemas, tables and views to the ci principal {ci_app_id}. The hub is already "
            "owned by ci: run the skillhub-bootstrap Job to apply the grants, tables, views and hooks."
        )
        return log_lines
    steps.ensure_tables(cfg, sql)
    steps.ensure_views(cfg, sql)
    log_lines.append("results tables and views ensured")
    for dept in cfg.departments:  # ci_eval could have pre-created any of these on an upgraded catalog
        for table in (JOURNAL_TABLE, *EVAL_WRITE_TABLES):
            name = f"{evals_schema(cfg, dept)}.{table}"
            owners.check("table", name, w.tables.get(name).owner)
    table_stmts = table_grant_plan(cfg, ci_app_id, ci_eval_app_id)  # the tables exist now
    for stmt in table_stmts:
        sql.execute(stmt)
    log_lines.append(f"applied {len(table_stmts)} table grant statements")
    # Before the hooks, so the experiment folders and their ACLs exist when ensure_experiment creates runs' homes.
    log_lines += steps.ensure_experiment_root(cfg, w, ci=ci_app_id, ci_eval=ci_eval_app_id)
    log_lines += [f"hook ran: {name}" for name in run_hooks(cfg, sql, skills_root)]
    trace_stmts = trace_table_grants(cfg, sql, ci_eval_app_id)  # ensure_experiment created them
    for stmt in trace_stmts:
        sql.execute(stmt)
    log_lines.append(f"granted ci_eval access to {len(trace_stmts)} trace tables")
    alerts = steps.ensure_alerts(cfg, w)
    log_lines.append(f"alerts ensured: {', '.join(alerts) if alerts else 'none'}")
    space_id = steps.ensure_genie_space(cfg, w)
    log_lines.append(grant_genie_access(cfg, w, space_id))
    if not as_ci:
        # The alerts and the Genie space first: the ci Job updates the alerts and needs to find the space.
        if steps.hand_over_alerts is not None:
            log_lines += steps.hand_over_alerts(w, alerts, ci_app_id)
        if steps.hand_over_genie is not None:
            log_lines.append(steps.hand_over_genie(w, space_id, ci_app_id))
        moved = ownership_transfers(cfg, w, owners, schema_owners)
        for stmt in moved:
            sql.execute(stmt)
        log_lines.append(
            f"handed {len(moved)} schemas, tables and views to the ci principal {ci_app_id}; "
            "re-run bootstrap as the skillhub-bootstrap Job from now on"
        )
    return log_lines
