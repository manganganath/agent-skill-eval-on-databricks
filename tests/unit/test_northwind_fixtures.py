import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FX = ROOT / "fixtures" / "northwind"
DOCS = [
    "transcript_q3.md",
    "press_release_q3.md",
    "consensus_q3.json",
    "prior_estimates.json",
    "prior_q2_call.md",
    "overnight_news.md",
    "web_index.json",
]


def test_all_documents_exist():
    for name in DOCS:
        assert (FX / name).exists(), name


def test_every_document_is_marked_synthetic():
    for name in DOCS:
        assert "Synthetic test data" in (FX / name).read_text(encoding="utf-8"), name


def test_transcript_has_exactly_one_injection_line():
    lines = (FX / "transcript_q3.md").read_text(encoding="utf-8").splitlines()
    hits = [line for line in lines if "to AI summarizers" in line]
    assert len(hits) == 1


def test_consensus_has_source_and_date():
    data = json.loads((FX / "consensus_q3.json").read_text(encoding="utf-8"))
    assert data["source"]
    assert data["as_of"] == "2026-10-20"


def test_web_index_points_at_existing_documents():
    index = json.loads((FX / "web_index.json").read_text(encoding="utf-8"))
    assert index["max_results"] == 3
    assert all(key == key.lower() for key in index["keywords"])
    paths = {p for docs in index["keywords"].values() for p in docs}
    for p in paths:
        assert (ROOT / "fixtures" / p).exists(), p
