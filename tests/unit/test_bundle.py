import pytest

from skillhub.bundle import BundleError, content_hash, discover_bundles, parse_frontmatter, skills_root


def test_load_bundle_excludes_evals_and_ignored_files(make_bundle):
    b = make_bundle(
        "common",
        "audit-xls",
        files={"references/a.md": "A", "evals/evals.json": "{}", ".DS_Store": "x"},
    )
    assert set(b.files) == {"SKILL.md", "references/a.md"}
    assert b.frontmatter["name"] == "audit-xls"
    assert b.description == "Does a thing."


def test_content_hash_is_order_free_and_length_prefixed():
    h = content_hash({"a": b"1", "b": b"2"})
    assert h == content_hash({"b": b"2", "a": b"1"})
    assert h.startswith("sha256:")
    assert content_hash({"a": b"1", "b": b"3"}) != h
    assert content_hash({"a": b"1b", "b": b""}) != content_hash({"a": b"1", "b": b"b"})


def test_content_hash_ignores_evals_paths():
    assert content_hash({"a": b"1", "evals/x.json": b"zzz"}) == content_hash({"a": b"1"})


def test_discover_sorts_by_department_then_name(make_bundle, tmp_path):
    make_bundle("operations", "b-skill")
    make_bundle("common", "z-skill")
    make_bundle("common", "a-skill")
    found = discover_bundles(tmp_path / "skills")
    assert [(b.dept, b.name) for b in found] == [
        ("common", "a-skill"),
        ("common", "z-skill"),
        ("operations", "b-skill"),
    ]


def test_frontmatter_without_fence_is_rejected():
    with pytest.raises(BundleError):
        parse_frontmatter(b"no frontmatter here")


@pytest.mark.parametrize(
    "data",
    [b"---\nname: [unclosed\n---\nbody\n", b"---\nname: \xff\xfe\n---\nbody\n", b"---\n- a\n- b\n---\n"],
)
def test_malformed_frontmatter_raises_bundle_error(data):
    with pytest.raises(BundleError):
        parse_frontmatter(data)


def test_skills_root_follows_env(monkeypatch, tmp_path, make_bundle):
    monkeypatch.delenv("SKILLHUB_SKILLS", raising=False)
    assert str(skills_root()) == "skills"
    make_bundle("common", "a-skill")
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    assert skills_root() == tmp_path / "skills"
    assert [b.name for b in discover_bundles()] == ["a-skill"]


def test_discover_accepts_skills_root_keyword(make_bundle, tmp_path):
    make_bundle("common", "a-skill")
    assert [b.name for b in discover_bundles(skills_root=tmp_path / "skills")] == ["a-skill"]


def test_symlinks_inside_a_bundle_are_refused(tmp_path, make_bundle):
    import pytest

    from skillhub.bundle import BundleError, load_bundle

    b = make_bundle("common", "linky")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (b.root / "leak.txt").symlink_to(secret)
    with pytest.raises(BundleError, match="symlink"):
        load_bundle(b.root, "common")
    (b.root / "leak.txt").unlink()
    (b.root / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BundleError, match="symlink"):
        load_bundle(b.root, "common")
