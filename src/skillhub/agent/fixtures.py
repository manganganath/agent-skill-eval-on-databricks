from __future__ import annotations

import json
import os
from pathlib import Path

import openpyxl

from skillhub.evalspec import FIXTURE_INDEX, EvalCase
from skillhub.gateway.models import ToolSpec

NO_RESULTS = "No results."
MAX_RESULTS = 3
WEB_INDEX = FIXTURE_INDEX  # the dataset digest covers every index file (evalspec.fixture_paths)
MAX_CHARS = 20_000
FAKE_TOOLS = ("web_search", "read_file", "read_workbook")


class FixtureIndexError(Exception):
    """A web_index.json file is unreadable or malformed."""


def _cap(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return f"{text[:MAX_CHARS]}\n[truncated: {MAX_CHARS} of {len(text)} chars]"


def read_workbook_dump(path: Path) -> str:
    """Cell-by-cell: 'Sheet!A1 formula-or-value'. Formulas come from one load, cached values from a second."""
    lines: list[str] = []
    wb_f = openpyxl.load_workbook(path, data_only=False, read_only=True)
    try:
        wb_v = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            for ws in wb_f.worksheets:
                for row_f, row_v in zip(ws.iter_rows(), wb_v[ws.title].iter_rows(), strict=False):
                    for cell, cell_v in zip(row_f, row_v, strict=False):
                        if cell.value is None:
                            continue
                        coord = f"{ws.title}!{cell.coordinate}"
                        if isinstance(cell.value, str) and cell.value.startswith("="):
                            lines.append(f"{coord} formula={cell.value} value={cell_v.value}")
                        else:
                            lines.append(f"{coord} value={cell.value}")
        finally:
            wb_v.close()
    finally:
        wb_f.close()
    return _cap("\n".join(lines))


class FixtureStore:
    def __init__(self, root: Path | str | None = None):
        # SKILLHUB_FIXTURES sets the default root; an explicit root wins.
        self.root = Path(root or os.environ.get("SKILLHUB_FIXTURES", "fixtures")).resolve()
        self._index_cache: dict[str, list[str]] | None = None

    def tools_for(self, case: EvalCase) -> list[ToolSpec]:
        if not case.fixtures:
            return []
        # The model is told the exact paths: without them it guesses (`model.xlsx`) and every read is refused.
        paths = sorted(case.fixtures)
        listed = ", ".join(paths)

        def path_param() -> dict:
            return {
                "type": "object",
                "properties": {"path": {"type": "string", "enum": paths, "description": f"One of: {listed}"}},
                "required": ["path"],
            }

        return [
            ToolSpec(
                "web_search",
                "Search the fixture document set by keyword.",
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            ),
            ToolSpec("read_file", f"Read one of this case's files as text. Files: {listed}.", path_param()),
            ToolSpec(
                "read_workbook",
                f"Read one of this case's Excel workbooks cell by cell (formulas and values). Files: {listed}.",
                path_param(),
            ),
        ]

    @staticmethod
    def _declared(rel: str, case: EvalCase) -> str | None:
        """The declared fixture `rel` names: exact, then without a leading `fixtures/` or `./`, then ignoring
        case, then a bare file name that matches exactly one declared fixture. None when nothing matches."""
        if rel in case.fixtures:
            return rel
        want = rel.strip().replace("\\", "/")
        while want.startswith("./"):
            want = want[2:]
        if want.lower().startswith("fixtures/"):
            want = want[len("fixtures/") :]
        by_lower = {f.lower(): f for f in case.fixtures}
        if want.lower() in by_lower:
            return by_lower[want.lower()]
        if "/" not in want:
            hits = [f for f in case.fixtures if f.rsplit("/", 1)[-1].lower() == want.lower()]
            if len(hits) == 1:
                return hits[0]
        return None

    def _resolve(self, rel: str, case: EvalCase) -> Path:
        """Resolve a path the case declares in `fixtures`; raise ValueError for anything else."""
        declared = self._declared(rel, case)
        if declared is None:
            raise ValueError(
                f"not a fixture of this case: {rel}; this case's files: {', '.join(sorted(case.fixtures))}"
            )
        p = (self.root / declared).resolve()
        if self.root not in p.parents:
            raise ValueError(f"path escapes fixtures root: {rel}")
        return p

    def call(self, name: str, arguments: dict, case: EvalCase) -> str:
        try:
            if name == "web_search":
                return self._search(str(arguments["query"]), case)
            if name == "read_file":
                return _cap(self._resolve(str(arguments["path"]), case).read_text(encoding="utf-8"))
            if name == "read_workbook":
                return read_workbook_dump(self._resolve(str(arguments["path"]), case))
        except FixtureIndexError as e:
            return f"Error: {e}"
        except KeyError as e:
            return f"Error: missing argument {e}"
        except (ValueError, OSError) as e:
            return f"Error: {e}"
        except Exception as e:  # unreadable or corrupt workbook, bad index file
            return f"Error: {type(e).__name__}: {e}"
        return f"Error: unknown tool {name}"

    def _index(self) -> dict[str, list[str]]:
        if self._index_cache is None:
            index: dict[str, list[str]] = {}
            for path in sorted(self.root.rglob(WEB_INDEX)):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for keyword, docs in data["keywords"].items():
                        index.setdefault(keyword.lower(), []).extend(docs)
                except (OSError, ValueError, KeyError, AttributeError, TypeError) as e:
                    raise FixtureIndexError(f"invalid {WEB_INDEX} ({path.relative_to(self.root)})") from e
            self._index_cache = index
        return self._index_cache

    def _search(self, query: str, case: EvalCase) -> str:
        q = query.lower()
        in_scope = set(case.fixtures)
        matched: dict[str, set[str]] = {}  # doc path -> distinct keywords that matched
        for keyword, docs in self._index().items():
            if keyword in q:
                for doc in docs:
                    if doc in in_scope:
                        matched.setdefault(doc, set()).add(keyword)
        ranked = sorted(matched, key=lambda d: (-len(matched[d]), d))[:MAX_RESULTS]
        if not ranked:
            return NO_RESULTS
        return "\n\n".join(f"=== {d} ===\n{self._doc_text(d, case)}" for d in ranked)

    def _doc_text(self, rel: str, case: EvalCase) -> str:
        """Text of one search hit, capped; an unreadable doc becomes an inline marker."""
        try:
            path = self._resolve(rel, case)
            if path.suffix == ".xlsx":
                return read_workbook_dump(path)
            return _cap(path.read_text(encoding="utf-8"))
        except Exception as e:  # one bad doc must not wipe out the other hits
            return f"[unreadable: {type(e).__name__}: {e}]"
