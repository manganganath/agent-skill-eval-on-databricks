from types import SimpleNamespace as NS

import pytest
from databricks.sdk.service.sql import ComparisonOperator

from skillhub.results import alerts
from tests.fakes import make_cfg


class FakeAlerts:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created = []
        self.updated = []

    def create_alert(self, alert):
        self.created.append(alert)
        new = NS(id=f"id-{len(self.created)}", display_name=alert.display_name, full=alert)
        self.existing.append(new)
        return new

    def list_alerts(self, **kw):
        return iter(self.existing)

    def get_alert(self, id):
        return next(a for a in self.existing if a.id == id).full

    def update_alert(self, id, alert, update_mask):
        self.updated.append((id, alert, update_mask))


NAMES = [
    "skillhub[skill_hub]: verdict-regression",
    "skillhub[skill_hub]: error-rate",
    "skillhub[skill_hub]: inconclusive",
    "skillhub[skill_hub]: switch-failed",
]


def current(cfg, key, subscriptions=()):
    """An alert as the workspace returns it, with the current query and the given subscriptions."""
    from databricks.sdk.service.sql import (
        AlertV2,
        AlertV2Evaluation,
        AlertV2Notification,
        AlertV2OperandColumn,
        AlertV2Subscription,
        CronSchedule,
    )

    subs = [AlertV2Subscription(destination_id=d) for d in subscriptions]
    return AlertV2(
        display_name=f"skillhub[skill_hub]: {key}",
        query_text=alerts.queries(cfg)[key][0],
        warehouse_id="test-warehouse",
        schedule=CronSchedule(quartz_cron_schedule="0 0 * * * ?", timezone_id="UTC"),
        evaluation=AlertV2Evaluation(
            source=AlertV2OperandColumn(name=alerts.queries(cfg)[key][1]),
            comparison_operator=ComparisonOperator.GREATER_THAN,
            notification=AlertV2Notification(subscriptions=subs),
        ),
    )


def test_four_alerts_with_fixed_names_and_thresholds(monkeypatch):
    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    w = NS(alerts_v2=FakeAlerts())
    ids = alerts.ensure_alerts(make_cfg(), w)
    assert len(ids) == 4
    assert [a.display_name for a in w.alerts_v2.created] == NAMES
    for a in w.alerts_v2.created:
        assert a.evaluation.comparison_operator == ComparisonOperator.GREATER_THAN
        assert a.evaluation.threshold.value.double_value == 0.0
        assert a.warehouse_id == "test-warehouse"


def _existing(cfg, subscriptions=()):
    keys = list(alerts.queries(cfg))
    return [
        NS(display_name=f"skillhub[skill_hub]: {k}", id=f"x{i}", full=current(cfg, k, subscriptions))
        for i, k in enumerate(keys, 1)
    ]


def test_idempotent_by_display_name(monkeypatch):
    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    cfg = make_cfg()
    w = NS(alerts_v2=FakeAlerts(_existing(cfg)))
    assert alerts.ensure_alerts(cfg, w) == ["x1", "x2", "x3", "x4"]
    assert w.alerts_v2.created == [] and w.alerts_v2.updated == []


def test_existing_alert_gets_the_current_query(monkeypatch):
    """The fix must reach alerts created before the fix: the query text is brought up to date."""
    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    cfg = make_cfg()
    existing = _existing(cfg)
    existing[1].full.query_text = "SELECT 1 AS n FROM old.matrix_all"
    w = NS(alerts_v2=FakeAlerts(existing))
    alerts.ensure_alerts(cfg, w)
    ((alert_id, alert, mask),) = w.alerts_v2.updated
    assert (alert_id, mask) == ("x2", "query_text")
    assert alert.query_text == alerts.queries(cfg)["error-rate"][0]


def test_existing_alert_gets_a_missing_destination_once(monkeypatch):
    """Alerts created before a destination was set get it on the next bootstrap; nothing is removed."""
    monkeypatch.setenv("SKILLHUB_ALERT_DESTINATION_ID", "dest-123")
    cfg = make_cfg()
    existing = _existing(cfg, subscriptions=["other"])
    from databricks.sdk.service.sql import AlertV2Subscription

    existing[3].full.evaluation.notification.subscriptions.append(
        AlertV2Subscription(destination_id="dest-123")
    )
    w = NS(alerts_v2=FakeAlerts(existing))
    alerts.ensure_alerts(cfg, w)
    assert [(i, m) for i, _, m in w.alerts_v2.updated] == [
        ("x1", "evaluation.notification.subscriptions"),
        ("x2", "evaluation.notification.subscriptions"),
        ("x3", "evaluation.notification.subscriptions"),
    ]
    subs = w.alerts_v2.updated[0][1].evaluation.notification.subscriptions
    assert [s.destination_id for s in subs] == ["other", "dest-123"]


def test_error_alert_uses_configured_max_error_rate():
    q, col = alerts.queries(make_cfg())["error-rate"]
    assert "error_rate > 0.1" in q
    assert col == "n"
    assert "skill_hub.eval.runs_all" in q


@pytest.mark.parametrize("key", ["verdict-regression", "error-rate", "inconclusive"])
def test_run_alerts_count_only_gating_runs(key):
    """PR, Hub and manual runs and the without_skill baseline never raise an alert."""
    q, _ = alerts.queries(make_cfg())[key]
    assert "trigger_source IN ('merge', 'nightly')" in q
    assert "COALESCE(mode, '') <> 'without_skill'" in q
    # The filter is applied before the latest-row window, so a later PR row can't hide a nightly one.
    cte = q[q.index("WITH latest AS (") : q.index("\n)", q.index("WITH latest AS ("))]
    assert "FROM skill_hub.eval.runs_all" in cte and f"WHERE {alerts.GATING}" in cte


def test_switch_failed_alert_reads_every_department_journal():
    q, col = alerts.queries(make_cfg())["switch-failed"]
    assert col == "n"
    assert "skill_hub.eval.versions_all" in q
    assert "action IN ('switch_failed', 'drift')" in q
    assert "action = 'switch_started' AND ts < current_timestamp() - INTERVAL 1 HOUR" in q


def test_regression_compares_latest_with_previous_run_of_same_key():
    q, _ = alerts.queries(make_cfg())["verdict-regression"]
    assert "PARTITION BY dept, skill, model_profile, harness, mode" in q
    assert "l.harness = p.harness" in q
    assert "rn IN (1,2)" in q.replace(" ", "") or "rn IN (1, 2)" in q
    assert "l.s > p.s" in q
    assert "skill_hub.eval.runs_all" in q


def test_destination_is_attached_when_configured(monkeypatch):
    monkeypatch.setenv("SKILLHUB_ALERT_DESTINATION_ID", "dest-123")
    w = NS(alerts_v2=FakeAlerts())
    alerts.ensure_alerts(make_cfg(), w)
    subs = w.alerts_v2.created[0].evaluation.notification.subscriptions
    assert [s.destination_id for s in subs] == ["dest-123"]


@pytest.mark.parametrize("bad", ["0.1; DROP TABLE x", float("nan"), float("inf"), True, -0.1, 1.5])
def test_non_numeric_max_error_rate_is_rejected(bad):
    cfg = make_cfg()
    object.__setattr__(cfg.defaults, "max_error_rate", bad)
    with pytest.raises(ValueError):
        alerts.queries(cfg)


def test_two_catalogs_create_separate_alerts(monkeypatch):
    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    w = NS(alerts_v2=FakeAlerts())
    cfg_a, cfg_b = make_cfg(), make_cfg()
    object.__setattr__(cfg_b, "catalog", "other_hub")
    ids_a = alerts.ensure_alerts(cfg_a, w)
    ids_b = alerts.ensure_alerts(cfg_b, w)
    assert len(w.alerts_v2.created) == 8
    assert set(ids_a).isdisjoint(ids_b)
    assert "other_hub.eval.runs_all" in w.alerts_v2.created[5].query_text


def test_every_query_has_one_day_window():
    for key, (q, _) in alerts.queries(make_cfg()).items():
        assert "INTERVAL 1 DAY" in q, key


def test_alert_settings_and_no_subscribers_without_destination(monkeypatch):
    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    w = NS(alerts_v2=FakeAlerts())
    alerts.ensure_alerts(make_cfg(), w)
    cols = {k: c for k, (_, c) in alerts.queries(make_cfg()).items()}
    for key, a in zip(cols, w.alerts_v2.created, strict=True):
        assert a.evaluation.source.name == cols[key]
        assert a.evaluation.notification.retrigger_seconds == 86400
        assert a.evaluation.notification.notify_on_ok is True
        assert a.evaluation.notification.subscriptions == []
        assert a.schedule.quartz_cron_schedule == "0 0 * * * ?"
        assert a.schedule.timezone_id == "UTC"
        assert a.query_text and a.query_text == alerts.queries(make_cfg())[key][0]


def _run_switch_failed(rows):
    """Run the switch-failed query over a sqlite versions_all with these (action, minutes ago) rows."""
    import sqlite3

    q, col = alerts.queries(make_cfg())["switch-failed"]
    q = q.replace("skill_hub.eval.versions_all", "versions_all")
    q = q.replace("current_timestamp() - INTERVAL 1 DAY", "datetime('now', '-1 day')")
    q = q.replace("current_timestamp() - INTERVAL 1 HOUR", "datetime('now', '-1 hour')")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE versions_all (dept TEXT, skill TEXT, action TEXT, ts TEXT)")
    for action, minutes in rows:
        db.execute(
            "INSERT INTO versions_all VALUES ('common', 'a', ?, datetime('now', ?))",
            (action, f"-{minutes} minutes"),
        )
    return db.execute(q).fetchone()[0]


@pytest.mark.parametrize("later", ["published", "publish_refused", "drift_old"])
def test_a_later_non_switch_row_never_hides_a_stuck_switch(later):
    """N3: the latest switch-type row decides; published, publish_refused and drift rows are not switch rows."""
    action, minutes = ("drift", 3000) if later == "drift_old" else (later, 30)
    rows = [("switched", 5000), ("switch_started", 4000), (action, minutes)]
    assert _run_switch_failed(rows) == 1


@pytest.mark.parametrize("end", ["switched", "switch_failed_old", "retired"])
def test_a_finished_switch_does_not_fire(end):
    action, minutes = ("switch_failed", 3000) if end == "switch_failed_old" else (end, 3000)
    assert _run_switch_failed([("switch_started", 4000), (action, minutes), ("published", 10)]) == 0


def test_a_young_switch_started_is_not_stuck_yet():
    assert _run_switch_failed([("switched", 5000), ("switch_started", 10)]) == 0


# --- Alert ownership after the hand-over ---


def test_the_ci_job_leaves_an_alert_it_does_not_own_unchanged_with_a_warning(monkeypatch, caplog):
    """N6: before the hand-over a person owns the alerts; the ci Job must not fail on them."""
    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    cfg = make_cfg()
    existing = _existing(cfg)
    existing[1].full.query_text = "SELECT 1 AS n FROM old.matrix_all"
    existing[1].full.owner_user_name = "person@example.com"
    w = NS(alerts_v2=FakeAlerts(existing), current_user=NS(me=lambda: NS(user_name="ci-app")))
    with caplog.at_level("WARNING"):
        assert alerts.ensure_alerts(cfg, w) == ["x1", "x2", "x3", "x4"]
    assert w.alerts_v2.updated == []
    assert "owned by person@example.com, not ci-app: query_text left unchanged" in caplog.text
    existing[1].full.owner_user_name = "ci-app"  # after the hand-over the Job keeps it up to date
    alerts.ensure_alerts(cfg, w)
    assert [(i, m) for i, _, m in w.alerts_v2.updated] == [("x2", "query_text")]


def test_a_refused_alert_update_is_a_warning(monkeypatch, caplog):
    from databricks.sdk.errors import PermissionDenied

    monkeypatch.delenv("SKILLHUB_ALERT_DESTINATION_ID", raising=False)
    cfg = make_cfg()
    existing = _existing(cfg)
    existing[0].full.query_text = "SELECT 0 AS regressions"
    fake = FakeAlerts(existing)

    def refuse(id, alert, update_mask):
        raise PermissionDenied("not the owner")

    fake.update_alert = refuse
    with caplog.at_level("WARNING"):
        assert alerts.ensure_alerts(cfg, NS(alerts_v2=fake)) == ["x1", "x2", "x3", "x4"]
    assert "could not update alert x1 (PermissionDenied)" in caplog.text


def test_hand_over_alerts_makes_ci_the_owner_and_warns_when_refused(caplog):
    from databricks.sdk.errors import PermissionDenied

    cfg = make_cfg()
    existing = _existing(cfg)
    existing[2].full.owner_user_name = "ci-app"  # already handed over
    fake = FakeAlerts(existing)
    lines = alerts.hand_over_alerts(NS(alerts_v2=fake), ["x1", "x2", "x3"], "ci-app")
    assert [(i, m) for i, _, m in fake.updated] == [("x1", "owner_user_name"), ("x2", "owner_user_name")]
    assert all(a.owner_user_name == "ci-app" for _, a, _ in fake.updated)
    assert lines == ["alert x1: owner is now ci-app", "alert x2: owner is now ci-app"]

    def refuse(id, alert, update_mask):
        raise PermissionDenied("admins only")

    fake.update_alert = refuse
    existing[0].full.owner_user_name = "person@example.com"
    with caplog.at_level("WARNING"):
        (line,) = alerts.hand_over_alerts(NS(alerts_v2=fake), ["x1"], "ci-app")
    assert line.startswith("warning: could not make ci-app the owner of alert x1") and "Manual step" in line
