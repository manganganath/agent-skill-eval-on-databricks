from skillhub.bundle import content_hash
from skillhub.naming import live_fqn, live_schema
from skillhub.registry.journal import new_row
from skillhub.registry.reconcile import reconcile, reconcile_skill
from skillhub.registry.versions import publish

SHA = "a" * 40
DEPT, SKILL = "equity_research", "earnings-preview"


def _make_live(reg, make_bundle):
    """Publish v1 and put it live by hand, journaling exactly what switch() would journal."""
    b = make_bundle(DEPT, SKILL, body="one")
    p = publish(reg.cfg, reg.uc, reg.journal, b, git_sha=SHA, eval_run_id="r", actor="ci", job_run_id=None)
    live = live_fqn(reg.cfg, DEPT, SKILL)
    reg.uc.create(live_schema(reg.cfg, DEPT), SKILL)
    for rel, data in b.files.items():
        reg.uc.upload_file(live, rel, data)
    reg.uc.finalize(live)
    h = content_hash(b.files)
    for action in ("switch_started", "switched"):
        reg.journal.append(
            new_row(
                DEPT,
                SKILL,
                action,
                actor="ci",
                to_version=p.version,
                versioned_fqn=p.versioned_fqn,
                content_hash=h,
            )
        )
    return b


def _kinds(reg):
    return [i.kind for i in reconcile_skill(reg.cfg, reg.uc, reg.journal, DEPT, SKILL)]


def test_clean_live_skill_has_no_issues(reg, make_bundle):
    _make_live(reg, make_bundle)
    assert reconcile(reg.cfg, reg.uc, reg.journal) == []


def test_external_edit_is_hash_drift(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.uc.external_edit(live_fqn(reg.cfg, DEPT, SKILL), "SKILL.md", b"hand edit")
    assert "hash_drift" in _kinds(reg)


def test_unfinished_switch_without_a_target_fqn_is_reported_alone(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.journal.append(new_row(DEPT, SKILL, "switch_started", actor="ci", to_version=2))
    assert _kinds(reg) == ["unfinished_switch"]


def test_switched_skill_with_missing_live_copy_is_reported_alone(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.uc.delete(live_fqn(reg.cfg, DEPT, SKILL))
    assert _kinds(reg) == ["missing_live"]


def test_retired_skill_with_no_live_copy_is_clean(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.uc.delete(live_fqn(reg.cfg, DEPT, SKILL))
    reg.journal.append(new_row(DEPT, SKILL, "retired", actor="ci", from_version=1))
    assert _kinds(reg) == []


def test_description_change_without_finalize_is_unfinalized(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.uc.external_edit(
        live_fqn(reg.cfg, DEPT, SKILL),
        "SKILL.md",
        b"---\nname: earnings-preview\ndescription: changed\n---\nbody\n",
    )
    assert {"hash_drift", "unfinalized_change"} <= set(_kinds(reg))


def test_live_skill_without_any_journal_row_is_drift(reg, make_bundle):
    b = make_bundle("common", "stray")
    reg.uc.create("skill_hub.common", "stray")
    reg.uc.upload_file("skill_hub.common.stray", "SKILL.md", b.files["SKILL.md"])
    reg.uc.finalize("skill_hub.common.stray")
    issues = reconcile_skill(reg.cfg, reg.uc, reg.journal, "common", "stray")
    assert [i.kind for i in issues] == ["hash_drift"]


def _run_cli(reg, monkeypatch):
    from typer.testing import CliRunner

    from skillhub import cli

    monkeypatch.setattr(cli, "_cfg", lambda ctx: reg.cfg)
    monkeypatch.setattr(cli, "_services", lambda cfg: (None, reg.uc, reg.sql, reg.journal))
    return CliRunner().invoke(cli.app, ["reconcile"])


def test_cli_reconcile_clean_exits_0(reg, make_bundle, monkeypatch):
    _make_live(reg, make_bundle)
    res = _run_cli(reg, monkeypatch)
    assert res.exit_code == 0
    assert "no issues" in res.output


def test_cli_reconcile_prints_one_line_per_issue_and_exits_1(reg, make_bundle, monkeypatch):
    _make_live(reg, make_bundle)
    reg.uc.external_edit(live_fqn(reg.cfg, DEPT, SKILL), "SKILL.md", b"hand edit")
    res = _run_cli(reg, monkeypatch)
    assert res.exit_code == 1
    lines = res.output.strip().splitlines()
    assert len(lines) >= 1 and all(line.startswith(f"{DEPT}/{SKILL}: ") for line in lines)
    assert any(": hash_drift: " in line for line in lines)


def test_unfinished_switch_is_found_after_a_later_published_row(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.journal.append(new_row(DEPT, SKILL, "switch_started", actor="ci", to_version=2))
    reg.journal.append(new_row(DEPT, SKILL, "published", actor="ci", version=2, content_hash="sha256:x"))
    assert _kinds(reg) == ["unfinished_switch"]


def test_crash_after_delete_is_unfinished_switch_not_missing_live(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.uc.delete(live_fqn(reg.cfg, DEPT, SKILL))
    reg.journal.append(new_row(DEPT, SKILL, "switch_started", actor="ci", to_version=2))
    assert _kinds(reg) == ["unfinished_switch"]


def test_retired_skill_with_live_copy_says_retired(reg, make_bundle):
    _make_live(reg, make_bundle)
    reg.journal.append(new_row(DEPT, SKILL, "retired", actor="ci", from_version=1))
    issues = reconcile_skill(reg.cfg, reg.uc, reg.journal, DEPT, SKILL)
    assert [i.kind for i in issues] == ["hash_drift"]
    assert "retired" in issues[0].detail
