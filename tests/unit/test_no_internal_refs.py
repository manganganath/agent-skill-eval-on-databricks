"""The repository is public: no file may cite internal documents, work tracks or milestones."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SELF = "tests/unit/test_no_internal_refs.py"

# Each pattern is built from pieces, so this file does not match itself.
RULING = "R" + "2-" + r"\d+"
SPEC = "sp" + "ec" + r"\s+(?:§|section\s)\s*\d"
CONTRACTS = "contr" + "acts" + r"\s+(?:§|section\s)\s*\d"
TRACK = r"\bT" + "[1-6]" + r"\b"
MILESTONE = r"\bM" + "[12]" + r"\b"

CASE_INSENSITIVE = re.compile("|".join([RULING, SPEC, CONTRACTS]), re.IGNORECASE)
CASE_SENSITIVE = re.compile("|".join([TRACK, MILESTONE]))

PATHS = [
    "src",
    "scripts",
    ".github",
    "resources",
    "config",
    "docs",
    "app",
    "tests",
    "README.md",
    "databricks.yml",
]
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".sh",
    ".sql",
    ".txt",
    ".toml",
    ".ts",
    ".tsx",
    ".js",
    ".css",
    ".html",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *PATHS], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    return [
        f
        for f in out
        if f
        and f != SELF
        and not f.endswith("package-lock.json")
        and (Path(f).suffix in TEXT_SUFFIXES or Path(f).name == "CODEOWNERS")
    ]


def test_no_internal_references_in_tracked_files():
    hits = []
    for name in _tracked_files():
        for number, line in enumerate((ROOT / name).read_text(errors="ignore").splitlines(), 1):
            if CASE_INSENSITIVE.search(line) or CASE_SENSITIVE.search(line):
                hits.append(f"{name}:{number}: {line.strip()[:100]}")
    assert not hits, "internal references found:\n" + "\n".join(hits)
