from pathlib import Path

import pytest

from skillhub.config import ImportSpec
from skillhub.importer import ImportFailed, department_for, import_skills, parse_source

SHA = "574ed3624aebd0418c7e96cd101262f30210ab26"
SPEC = ImportSpec(
    source=f"github.com/example/skills@{SHA}",
    map={
        "plugins/vertical-plugins/financial-analysis": "common",
        "plugins/vertical-plugins/equity-research": "equity_research",
        "plugins/vertical-plugins/operations": "operations",
        "plugins/agent-plugins/meeting-prep-agent": "wealth_management",
        "plugins/partner-built/*": "external",
    },
    exclude=["verify"],
)


def _skill(root: Path, rel_dir: str, body: str = "body") -> Path:
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: d\n---\n{body}\n", encoding="utf-8")
    return d


@pytest.fixture
def source(tmp_path) -> Path:
    src = tmp_path / "src"
    _skill(src, "plugins/vertical-plugins/financial-analysis/skills/audit-xls", "A")
    _skill(src, "plugins/vertical-plugins/equity-research/skills/morning-note", "M")
    _skill(src, "plugins/agent-plugins/earnings-reviewer/skills/morning-note", "M")  # identical, unmapped
    _skill(src, "plugins/agent-plugins/meeting-prep-agent/skills/prep", "P")
    _skill(src, "plugins/partner-built/acme/skills/acme-skill", "X")
    _skill(src, "plugins/vertical-plugins/operations/skills/verify", "V")  # excluded
    (src / "LICENSE").write_text("Apache License fake text\n", encoding="utf-8")
    return src


def test_parse_source_requires_full_sha():
    assert parse_source(f"github.com/a/b@{SHA}") == ("github.com/a/b", SHA)
    with pytest.raises(ImportFailed):
        parse_source("github.com/a/b@main")


def test_first_match_wins_and_wildcards_match_segments():
    assert department_for(SPEC.map, "plugins/partner-built/acme/skills/x") == "external"
    assert department_for(SPEC.map, "plugins/vertical-plugins/financial-analysis/skills/x") == "common"
    assert department_for(SPEC.map, "plugins/agent-plugins/earnings-reviewer/skills/x") is None


def test_imports_mapped_skills_and_writes_notices(source, tmp_path):
    dest = tmp_path / "skills"
    report = import_skills(SPEC, source, dest)
    assert report.skills == {
        "audit-xls": "common",
        "morning-note": "equity_research",
        "prep": "wealth_management",
        "acme-skill": "external",
    }
    assert not (dest / "operations").exists()  # verify was excluded
    assert (dest / "common" / "audit-xls" / "SKILL.md").read_bytes() == (
        source / "plugins/vertical-plugins/financial-analysis/skills/audit-xls/SKILL.md"
    ).read_bytes()
    notices = (tmp_path / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    assert "acme-skill" in notices and SHA in notices


def test_skill_only_under_unmapped_paths_fails(source, tmp_path):
    _skill(source, "plugins/other-plugin/skills/lonely")
    with pytest.raises(ImportFailed, match="lonely"):
        import_skills(SPEC, source, tmp_path / "skills")


def test_copies_must_be_byte_identical(source, tmp_path):
    _skill(source, "plugins/agent-plugins/earnings-reviewer/skills/morning-note", "DIFFERENT")
    with pytest.raises(ImportFailed, match="not byte-identical"):
        import_skills(SPEC, source, tmp_path / "skills")


def test_copies_in_several_departments_take_earliest_map_entry(source, tmp_path):
    # operations (3rd map entry) also holds morning-note; equity-research (2nd) wins
    _skill(source, "plugins/vertical-plugins/operations/skills/morning-note", "M")
    report = import_skills(SPEC, source, tmp_path / "skills")
    assert report.skills["morning-note"] == "equity_research"
    assert report.origin["morning-note"] == "plugins/vertical-plugins/equity-research/skills/morning-note"
    # a copy under an earlier map entry beats it
    _skill(source, "plugins/vertical-plugins/financial-analysis/skills/morning-note", "M")
    report = import_skills(SPEC, source, tmp_path / "skills2")
    assert report.skills["morning-note"] == "common"


def test_notices_keep_text_outside_generated_block(source, tmp_path):
    notices = tmp_path / "THIRD_PARTY_NOTICES"
    notices.write_text("Hand-written licence text\n", encoding="utf-8")
    import_skills(SPEC, source, tmp_path / "skills")
    import_skills(SPEC, source, tmp_path / "skills")
    text = notices.read_text(encoding="utf-8")
    assert text.startswith("Hand-written licence text")
    assert text.count("<!-- skillhub-import:start -->") == 1
    assert text.count("<!-- skillhub-import:end -->") == 1
    notices.write_text(text.replace("acme-skill", "STALE") + "\nTrailer\n", encoding="utf-8")
    import_skills(SPEC, source, tmp_path / "skills")
    text = notices.read_text(encoding="utf-8")
    assert "STALE" not in text and "acme-skill" in text and text.rstrip().endswith("Trailer")


def test_default_dest_uses_skills_root(source, tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "custom"))
    import_skills(SPEC, source)
    assert (tmp_path / "custom" / "common" / "audit-xls" / "SKILL.md").is_file()


def test_exclude_drops_names(source, tmp_path):
    report = import_skills(SPEC, source, tmp_path / "skills")
    assert report.excluded == ["verify"] and "verify" not in report.skills


def test_rerun_replaces_imported_skill_and_keeps_customer_skills(source, tmp_path):
    dest = tmp_path / "skills"
    import_skills(SPEC, source, dest)
    (dest / "common" / "audit-xls" / "SKILL.md").write_text("stale")
    (dest / "common" / "customer-own").mkdir(parents=True)
    (dest / "common" / "customer-own" / "SKILL.md").write_text("mine")
    import_skills(SPEC, source, dest)
    assert (dest / "common" / "audit-xls" / "SKILL.md").read_bytes() != b"stale"
    assert (dest / "common" / "customer-own" / "SKILL.md").read_text() == "mine"


def test_rerun_keeps_evals_and_drops_files_the_source_removed(source, tmp_path):
    dest = tmp_path / "skills"
    skill_src = source / "plugins/vertical-plugins/financial-analysis/skills/audit-xls"
    (skill_src / "old.md").write_text("old")
    import_skills(SPEC, source, dest)
    target = dest / "common" / "audit-xls"
    assert (target / "old.md").exists()
    (target / "evals").mkdir()
    (target / "evals" / "cases.yaml").write_text("mine")
    (skill_src / "old.md").unlink()
    import_skills(SPEC, source, dest)
    assert not (target / "old.md").exists()
    assert (target / "evals" / "cases.yaml").read_text() == "mine"


def test_failed_import_writes_nothing(source, tmp_path):
    dest = tmp_path / "skills"
    import_skills(SPEC, source, dest)
    notices = tmp_path / "THIRD_PARTY_NOTICES"
    before_notices = notices.read_bytes()
    (dest / "common" / "audit-xls" / "SKILL.md").write_text("local")
    _skill(source, "plugins/other-plugin/skills/lonely")  # sorts after audit-xls; must still block writes
    with pytest.raises(ImportFailed):
        import_skills(SPEC, source, dest)
    assert (dest / "common" / "audit-xls" / "SKILL.md").read_text() == "local"
    assert notices.read_bytes() == before_notices
    fresh = tmp_path / "fresh"
    with pytest.raises(ImportFailed):
        import_skills(SPEC, source, fresh)
    assert not fresh.exists() and not (tmp_path / "THIRD_PARTY_NOTICES").read_text().count("lonely")


def test_symlinks_fail(source, tmp_path):
    d = source / "plugins/vertical-plugins/financial-analysis/skills/audit-xls"
    (d / "link.md").symlink_to(source / "LICENSE")
    with pytest.raises(ImportFailed, match="symlink"):
        import_skills(SPEC, source, tmp_path / "skills")
    (d / "link.md").unlink()
    (d / "SKILL.md").rename(d / "real.md")
    (d / "SKILL.md").symlink_to(d / "real.md")
    with pytest.raises(ImportFailed, match="symlink"):
        import_skills(SPEC, source, tmp_path / "skills")


def test_notices_carry_license_notice_url_and_sha(source, tmp_path):
    (source / "NOTICE").write_text("Source notice text\n", encoding="utf-8")
    import_skills(SPEC, source, tmp_path / "skills")
    text = (tmp_path / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    for needle in (
        "Apache License fake text",
        "Source notice text",
        "https://github.com/example/skills",
        SHA,
    ):
        assert needle in text


def test_missing_license_fails(source, tmp_path):
    (source / "LICENSE").unlink()
    with pytest.raises(ImportFailed, match="LICENSE"):
        import_skills(SPEC, source, tmp_path / "skills")
    assert not (tmp_path / "skills").exists()


def test_unknown_department_in_map_fails(source, tmp_path):
    with pytest.raises(ImportFailed, match="unknown department"):
        import_skills(SPEC, source, tmp_path / "skills", departments=["common"])
    assert not (tmp_path / "skills").exists()


def test_bad_skill_folder_name_fails(source, tmp_path):
    d = _skill(source, "plugins/vertical-plugins/financial-analysis/skills/bad-folder")
    (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: d\n---\nb\n", encoding="utf-8")
    with pytest.raises(ImportFailed, match="not a valid skill name"):
        import_skills(SPEC, source, tmp_path / "skills")


def test_clone_source_failure_is_import_failed_and_cleans_up(tmp_path, monkeypatch):
    import subprocess

    from skillhub import importer

    def boom(cmd, **kw):
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "core.hooksPath=/dev/null" in cmd and "protocol.ext.allow=never" in cmd
        raise subprocess.CalledProcessError(128, cmd, stderr="fatal: nope\n")

    monkeypatch.setattr(importer.subprocess, "run", boom)
    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(ImportFailed, match="fatal: nope"), importer.clone_source(SPEC.source, work):
        pass
    monkeypatch.setattr(importer.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "tmpclone"))
    (tmp_path / "tmpclone").mkdir()
    with pytest.raises(ImportFailed), importer.clone_source(SPEC.source):
        pass
    assert not (tmp_path / "tmpclone").exists()


def _cfg_file(tmp_path, cfg_map):
    root = Path(__file__).resolve().parents[2]
    text = (root / "config/hub.yaml").read_text(encoding="utf-8")
    head = text[: text.index("\nimport:")]
    body = "\n".join(f"    {k}: {v}" for k, v in cfg_map.items())
    p = tmp_path / "hub.yaml"
    p.write_text(f"{head}\nimport:\n  source: {SPEC.source}\n  map:\n{body}\n  exclude: [verify]\n")
    return p


def test_import_command_success(source, tmp_path, cfg):
    import json

    from typer.testing import CliRunner

    from skillhub.cli import app

    p = _cfg_file(tmp_path, SPEC.map)
    dest = tmp_path / "out"
    r = CliRunner().invoke(
        app, ["--config", str(p), "import", "--source-dir", str(source), "--dest", str(dest)]
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["total"] == 4 and data["excluded"] == ["verify"]
    assert data["by_department"]["common"] == 1
    assert (dest / "common" / "audit-xls" / "SKILL.md").is_file()


def test_import_command_failure_prints_one_error_line(source, tmp_path, cfg):
    from typer.testing import CliRunner

    from skillhub.cli import app

    p = _cfg_file(tmp_path, {"plugins/vertical-plugins/financial-analysis": "common"})
    r = CliRunner().invoke(
        app, ["--config", str(p), "import", "--source-dir", str(source), "--dest", str(tmp_path / "o")]
    )
    assert r.exit_code == 1
    assert "does not cover" in r.output
    assert not (tmp_path / "o").exists()


def _named(root: Path, rel_dir: str, name: str, body: str = "B") -> None:
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}\n", encoding="utf-8")


def test_frontmatter_name_is_destination_and_rename_is_recorded(source, tmp_path):
    _named(source, "plugins/partner-built/acme/skills/foo-beta", "foo-single")
    dest = tmp_path / "skills"
    report = import_skills(SPEC, source, dest)
    assert report.skills["foo-single"] == "external"
    assert (dest / "external" / "foo-single" / "SKILL.md").is_file()
    assert not (dest / "external" / "foo-beta").exists()
    rec = {"from": "foo-beta", "to": "foo-single", "path": "plugins/partner-built/acme/skills/foo-beta"}
    assert rec in report.as_dict()["renamed"]
    notices = (tmp_path / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    assert "imported under its declared name" in notices.lower() and "foo-beta -> foo-single" in notices
    # third-party text is not edited
    assert "name: foo-single" in (dest / "external" / "foo-single" / "SKILL.md").read_text()


def test_copies_with_different_folders_but_same_name_dedupe(source, tmp_path):
    _named(source, "plugins/vertical-plugins/equity-research/skills/note-a", "shared-note")
    _named(source, "plugins/vertical-plugins/operations/skills/note-b", "shared-note")
    report = import_skills(SPEC, source, tmp_path / "skills")
    assert report.skills["shared-note"] == "equity_research"
    _named(source, "plugins/vertical-plugins/operations/skills/note-b", "shared-note", "DIFF")
    with pytest.raises(ImportFailed, match="not byte-identical"):
        import_skills(SPEC, source, tmp_path / "skills2")


def test_exclude_matches_upstream_folder_name(source, tmp_path):
    _named(source, "plugins/partner-built/acme/skills/foo-beta", "foo-single")
    spec = SPEC.model_copy(update={"exclude": ["verify", "foo-beta"]})
    report = import_skills(spec, source, tmp_path / "skills")
    assert "foo-single" not in report.skills and "foo-beta" in report.excluded


@pytest.mark.parametrize(
    ("license_text", "label"),
    [
        (
            "                                 Apache License\n                           Version 2.0, January 2004\n",
            "(Apache-2.0)",
        ),
        ("MIT License\n\nCopyright (c) 2026 Someone\n", "(MIT)"),
        ("Some other licence\n", None),
    ],
)
def test_notices_name_only_the_licence_the_source_has(source, tmp_path, license_text, label):
    (source / "LICENSE").write_text(license_text, encoding="utf-8")
    import_skills(SPEC, source, tmp_path / "skills")
    first = next(
        line for line in (tmp_path / "THIRD_PARTY_NOTICES").read_text().splitlines() if "derived from" in line
    )
    if label:
        assert first.endswith(f"{label}.")
    else:
        assert "(" not in first and first.endswith(".")
