from __future__ import annotations

import logging
import math
import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from databricks.sdk.service.sql import (
    AlertV2,
    AlertV2Evaluation,
    AlertV2Notification,
    AlertV2Operand,
    AlertV2OperandColumn,
    AlertV2OperandValue,
    AlertV2Subscription,
    ComparisonOperator,
    CronSchedule,
)

from skillhub.config import HubConfig
from skillhub.naming import eval_schema

log = logging.getLogger(__name__)
DESTINATION_ENV = "SKILLHUB_ALERT_DESTINATION_ID"


# Only gating runs raise alerts. PR, Hub and manual runs report to whoever started them, and the
# without_skill baseline is not a verdict on the skill.
GATING = "parent_run_id IS NOT NULL AND trigger_source IN ('merge', 'nightly') AND COALESCE(mode, '') <> 'without_skill'"
# Journal rows that start or end a switch (registry/reconcile.py). A retire ends an unfinished switch too.
SWITCH_ROWS = "'switch_started', 'switch_failed', 'switched', 'retired'"


def _latest_gating(ev: str, columns: str) -> str:
    """CTE `latest`: the latest gating run per (dept, skill, model, harness, mode), rn = 1 first."""
    return f"""WITH latest AS (
  SELECT {columns},
         ROW_NUMBER() OVER (PARTITION BY dept, skill, model_profile, harness, mode
                            ORDER BY started_at DESC, run_id DESC) AS rn
  FROM {ev}.runs_all
  WHERE {GATING}
)"""


def queries(cfg: HubConfig) -> dict[str, tuple[str, str]]:
    """Alert key -> (SQL, output column). Each alert fires when its column is > 0."""
    ev = eval_schema(cfg)
    max_err = cfg.defaults.max_error_rate
    if (
        isinstance(max_err, bool)
        or not isinstance(max_err, (int, float))
        or not math.isfinite(max_err)
        or not 0 <= max_err <= 1
    ):
        raise ValueError(f"defaults.max_error_rate must be a number from 0 to 1, got {max_err!r}")
    max_err = float(max_err)
    regression = f"""
{_latest_gating(ev, "dept, skill, model_profile, harness, mode, verdict, started_at")},
sev AS (
  SELECT dept, skill, model_profile, harness, mode, started_at, rn,
         CASE verdict WHEN 'pass' THEN 0 WHEN 'warn' THEN 1 WHEN 'fail' THEN 2 END AS s
  FROM latest
  WHERE rn IN (1, 2)
)
SELECT COUNT(*) AS regressions
FROM sev l
JOIN sev p
  ON l.dept = p.dept AND l.skill = p.skill AND l.model_profile = p.model_profile
  AND l.harness = p.harness AND l.mode = p.mode
WHERE l.rn = 1 AND p.rn = 2 AND l.s > p.s AND l.started_at >= current_timestamp() - INTERVAL 1 DAY
""".strip()
    error_rate = f"""
{_latest_gating(ev, "error_rate, started_at")}
SELECT COUNT(*) AS n FROM latest
WHERE rn = 1 AND error_rate > {max_err} AND started_at >= current_timestamp() - INTERVAL 1 DAY
""".strip()
    inconclusive = f"""
{_latest_gating(ev, "verdict, started_at")}
SELECT COUNT(*) AS n FROM latest
WHERE rn = 1 AND verdict = 'inconclusive' AND started_at >= current_timestamp() - INTERVAL 1 DAY
""".strip()
    # A switch that failed or was refused for drift in the last day, or one still not finished after an hour.
    # "Not finished" reads only the rows that start or end a switch, as reconcile does: a later published,
    # publish_refused or drift row must not hide a stuck switch_started.
    switch_failed = f"""
WITH last AS (
  SELECT dept, skill, action, ts, ROW_NUMBER() OVER (PARTITION BY dept, skill ORDER BY ts DESC) AS rn
  FROM {ev}.versions_all
  WHERE action IN ({SWITCH_ROWS})
)
SELECT COUNT(*) AS n FROM (
  SELECT dept FROM {ev}.versions_all
  WHERE action IN ('switch_failed', 'drift') AND ts >= current_timestamp() - INTERVAL 1 DAY
  UNION ALL
  SELECT dept FROM last
  WHERE rn = 1 AND action = 'switch_started' AND ts < current_timestamp() - INTERVAL 1 HOUR
)
""".strip()
    return {
        "verdict-regression": (regression, "regressions"),
        "error-rate": (error_rate, "n"),
        "inconclusive": (inconclusive, "n"),
        "switch-failed": (switch_failed, "n"),
    }


def _subscriptions(alert) -> list:
    notification = getattr(getattr(alert, "evaluation", None), "notification", None)
    return list(getattr(notification, "subscriptions", None) or [])


def _me(w: WorkspaceClient) -> str | None:
    """The caller's identity, or None when it cannot be read (the update is then tried, and a refusal warns)."""
    try:
        return getattr(w.current_user.me(), "user_name", None)
    except Exception:  # noqa: BLE001 - an unknown caller only skips the owner pre-check
        return None


def _refresh(
    w: WorkspaceClient, alert_id: str, sql_text: str, dest: str | None, me: str | None = None
) -> None:
    """Bring an existing alert's query and subscription up to date. Other settings stay as they are.

    Only the owner may change an alert. When the caller is not its owner (the ci Job, before bootstrap handed
    the alerts to ci), or the update is refused, the alert is left as it is with a WARNING: the Job never
    fails on it."""
    current = w.alerts_v2.get_alert(alert_id)
    subs = _subscriptions(current)
    new_query = current.query_text != sql_text
    new_sub = bool(dest) and dest not in {s.destination_id for s in subs}
    if new_sub and (current.evaluation is None or current.evaluation.notification is None):
        log.warning("alert %s has no notification settings; add destination %s by hand", alert_id, dest)
        new_sub = False
    mask = [
        m for m, on in (("query_text", new_query), ("evaluation.notification.subscriptions", new_sub)) if on
    ]
    if not mask:
        return
    owner = getattr(current, "owner_user_name", None)
    if me and owner and owner != me:  # decided before anything is changed
        log.warning(
            "alert %s is owned by %s, not %s: %s left unchanged. Run `skillhub bootstrap` as a workspace admin "
            "to hand the alerts to ci, or change the alert by hand",
            alert_id,
            owner,
            me,
            ", ".join(mask),
        )
        return
    if new_query:
        current.query_text = sql_text
    if new_sub:
        current.evaluation.notification.subscriptions = [*subs, AlertV2Subscription(destination_id=dest)]
    try:
        w.alerts_v2.update_alert(alert_id, current, update_mask=",".join(mask))
    except DatabricksError as e:
        log.warning(
            "could not update alert %s (%s): %s left unchanged", alert_id, type(e).__name__, ", ".join(mask)
        )


def hand_over_alerts(w: WorkspaceClient, alert_ids: list[str], owner: str) -> list[str]:
    """Make `owner` (the ci principal) the owner of each alert, so the ci Job can keep them up to date.

    Runs at the end of a person's first bootstrap. Changing an owner needs a workspace admin; a refused change
    is a WARNING with the manual step, never a failure."""
    lines: list[str] = []
    for alert_id in alert_ids:
        try:
            current = w.alerts_v2.get_alert(alert_id)
            if getattr(current, "owner_user_name", None) == owner:
                continue
            current.owner_user_name = owner
            w.alerts_v2.update_alert(alert_id, current, update_mask="owner_user_name")
            lines.append(f"alert {alert_id}: owner is now {owner}")
        except DatabricksError as e:
            msg = (
                f"could not make {owner} the owner of alert {alert_id} ({type(e).__name__}). Manual step: open the "
                "alert, choose Share or More > Change owner, and pick the ci service principal"
            )
            log.warning(msg)
            lines.append(f"warning: {msg}")
    return lines


def ensure_alerts(cfg: HubConfig, w: WorkspaceClient) -> list[str]:
    """Create the SQL alerts if missing. Returns alert IDs in the order of queries().

    Existing alerts are matched by display name (which includes the catalog). Their query is updated to the
    current text, and the destination from SKILLHUB_ALERT_DESTINATION_ID is added when it is missing; a
    subscription is never removed. An alert the caller does not own is left as it is, with a WARNING.
    Without a destination the alerts have no subscribers, with a WARNING.
    """
    existing = {a.display_name: a.id for a in w.alerts_v2.list_alerts()}
    me = _me(w) if existing else None
    dest = (os.environ.get(DESTINATION_ENV) or "").strip() or None
    if not dest:
        log.warning(
            "%s is not set; alerts are created without subscribers and notify nobody", DESTINATION_ENV
        )
    ids: list[str] = []
    for key, (sql_text, column) in queries(cfg).items():
        name = f"skillhub[{cfg.catalog}]: {key}"
        if name in existing:
            _refresh(w, existing[name], sql_text, dest, me)
            ids.append(existing[name])
            continue
        alert = AlertV2(
            display_name=name,
            query_text=sql_text,
            warehouse_id=cfg.workspace.sql_warehouse_id,
            evaluation=AlertV2Evaluation(
                source=AlertV2OperandColumn(name=column),
                comparison_operator=ComparisonOperator.GREATER_THAN,
                threshold=AlertV2Operand(value=AlertV2OperandValue(double_value=0.0)),
                notification=AlertV2Notification(
                    notify_on_ok=True,
                    retrigger_seconds=86400,
                    subscriptions=[AlertV2Subscription(destination_id=dest)] if dest else [],
                ),
            ),
            schedule=CronSchedule(quartz_cron_schedule="0 0 * * * ?", timezone_id="UTC"),
        )
        ids.append(w.alerts_v2.create_alert(alert).id)
    return ids
