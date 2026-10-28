import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

REQUIRED = {
    "architecture.md": [
        "## Components",
        "## Data flow",
        "## Unity Catalog layout",
        "## Permission model",
        "## Versions and switch",
        "## Evaluation method",
        "## Results",
        "## Beta features and limitations",
    ],
    "operations.md": [
        "## Bootstrap",
        "## Identities",
        "## Secrets",
        "## Deploy",
        "## Run evals",
        "## Switch and rollback",
        "## Reconcile alerts",
        "## Nightly",
    ],
    "api.md": ["## REST API", "## Direct Databricks API equivalents"],
    "onboarding.md": ["## Add a skill", "## Import another skill repository"],
}

FORBIDDEN = {
    "workspace host": re.compile(
        r"[a-z0-9-]+\.(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)"
    ),
    "warehouse or workspace id": re.compile(
        r"warehouses/[0-9a-f]{8,}|\b(?:dbc|adb)-[0-9a-f]{8}|\b[0-9a-f]{16}\b"
    ),
    "section sign": re.compile("§"),
}


def _public_docs():
    return [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]


def _headings(text):
    return {line.rstrip() for line in text.splitlines() if line.startswith("## ")}


@pytest.mark.parametrize("name", list(REQUIRED))
def test_required_sections(name):
    headings = _headings((DOCS / name).read_text(encoding="utf-8"))
    for heading in REQUIRED[name]:
        assert heading in headings, f"{name} lacks '{heading}'"


def test_no_internal_or_host_content():
    for f in _public_docs():
        text = f.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN.items():
            match = pattern.search(text)
            assert match is None, f"{f.name}: {label}: {match.group(0) if match else ''}"


def test_no_competitor_names():
    path = os.environ.get("SKILLHUB_COMPETITOR_TERMS")
    if not path:
        pytest.skip("SKILLHUB_COMPETITOR_TERMS is not set")
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    terms = [t.strip().lower() for t in lines if t.strip() and not t.strip().startswith("#")]
    for f in _public_docs():
        text = f.read_text(encoding="utf-8").lower()
        for term in terms:
            assert not re.search(rf"\b{re.escape(term)}\b", text), f"{f.name} contains a listed term"
