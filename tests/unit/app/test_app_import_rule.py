"""The app principal never calls a catalog model: no app module reaches the Bedrock client or the AWS keys."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3] / "app" / "backend"
FORBIDDEN = ("skillhub.gateway.bedrock", "skillhub.gateway.aws")


def imported(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_the_import_scan_sees_a_forbidden_import():
    tree = ast.parse("from skillhub.gateway import bedrock\nimport skillhub.gateway.aws")
    assert {"skillhub.gateway.bedrock", "skillhub.gateway.aws"} <= imported(tree)


def test_no_app_backend_module_imports_the_bedrock_client_or_aws_keys():
    files = sorted(BACKEND.rglob("*.py"))
    assert files
    bad = [
        f"{f.relative_to(BACKEND)}: {name}"
        for f in files
        for name in imported(ast.parse(f.read_text()))
        if any(name == m or name.startswith(m + ".") for m in FORBIDDEN)
    ]
    assert bad == []
