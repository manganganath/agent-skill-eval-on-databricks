from skillhub.naming import evals_schema
from skillhub.registry.journal import Journal, new_row

SHA = "a" * 40


def test_append_and_history_are_oldest_first(reg):
    j = reg.journal
    j.append(
        new_row(
            "common", "audit-xls", "published", actor="ci", version=1, content_hash="sha256:1", git_sha=SHA
        )
    )
    j.append(
        new_row(
            "common", "audit-xls", "published", actor="ci", version=2, content_hash="sha256:2", git_sha=SHA
        )
    )
    hist = j.history("common", "audit-xls")
    assert [r.version for r in hist] == [1, 2]


def test_latest_version_and_version_by_hash(reg):
    j = reg.journal
    assert j.latest_version("common", "audit-xls") == 0
    j.append(
        new_row(
            "common", "audit-xls", "published", actor="ci", version=1, content_hash="sha256:h1", git_sha=SHA
        )
    )
    j.append(
        new_row(
            "common", "audit-xls", "published", actor="ci", version=2, content_hash="sha256:h2", git_sha=SHA
        )
    )
    assert j.latest_version("common", "audit-xls") == 2
    assert j.version_by_hash("common", "audit-xls", "sha256:h1") == 1
    assert j.version_by_hash("common", "audit-xls", "sha256:nope") is None


def test_live_state_follows_switched_and_retired_only(reg):
    j = reg.journal
    j.append(
        new_row(
            "common", "audit-xls", "published", actor="ci", version=1, content_hash="sha256:h1", git_sha=SHA
        )
    )
    assert j.live_state("common", "audit-xls") is None  # publishing does not make a skill live
    j.append(new_row("common", "audit-xls", "switch_started", actor="ci", to_version=1))
    j.append(new_row("common", "audit-xls", "switched", actor="ci", to_version=1, content_hash="sha256:h1"))
    j.append(new_row("common", "audit-xls", "drift", actor="ci", content_hash="sha256:x"))
    state = j.live_state("common", "audit-xls")
    assert state.action == "switched" and state.to_version == 1
    j.append(new_row("common", "audit-xls", "retired", actor="ci"))
    assert j.live_state("common", "audit-xls").action == "retired"


def test_skills_lists_journaled_names(reg):
    reg.journal.append(
        new_row("common", "b-skill", "published", actor="ci", version=1, content_hash="sha256:1")
    )
    reg.journal.append(
        new_row("common", "a-skill", "published", actor="ci", version=1, content_hash="sha256:2")
    )
    assert reg.journal.skills("common") == ["a-skill", "b-skill"]


def test_journal_is_insert_only_and_targets_the_dept_evals_schema(reg):
    reg.journal.append(
        new_row("common", "audit-xls", "published", actor="ci", version=1, content_hash="sha256:1")
    )
    reg.journal.history("common", "audit-xls")
    dml = [s for s, _ in reg.sql.statements if not s.startswith("CREATE")]
    assert all(s.split()[0] in ("INSERT", "SELECT") for s in dml)
    assert dml[0].startswith(f"INSERT INTO {evals_schema(reg.cfg, 'common')}.skill_versions")
    assert not any(hasattr(Journal, m) for m in ("update", "delete", "merge", "upsert"))


def test_nullable_int_columns_are_cast_in_the_insert(reg):
    reg.journal.append(new_row("common", "audit-xls", "drift", actor="ci", content_hash="sha256:x"))
    stmt, params = reg.sql.statements[-1]
    for c in ("version", "from_version", "to_version"):
        assert f"CAST(:{c} AS INT)" in stmt
    assert params["version"] is None
    assert reg.journal.history("common", "audit-xls")[0].version is None


def test_switch_after_retire_is_live_again(reg):
    j = reg.journal
    j.append(new_row("common", "s", "switched", actor="ci", to_version=1))
    j.append(new_row("common", "s", "retired", actor="ci"))
    j.append(new_row("common", "s", "switched", actor="ci", to_version=2))
    state = j.live_state("common", "s")
    assert state.action == "switched" and state.to_version == 2
