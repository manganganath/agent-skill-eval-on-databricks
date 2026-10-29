import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillhub.bundle import SkillBundle
from skillhub.eval.judges import criterion_instructions
from skillhub.gateway.models import ChatResult
from skillhub.generate.rubric import generate_rubric
from skillhub.generate.tests import GenerationError
from tests.fakes import FakeModelClient

CFG = SimpleNamespace(generator="claude-opus", catalog="cat", defaults=SimpleNamespace(pass_score=0.7))
BUNDLE = SkillBundle(
    dept="common",
    name="audit-xls",
    root=Path("."),
    files={"SKILL.md": b"# Audit\nStep 1: check balance sheet"},
    frontmatter={"name": "audit-xls", "description": "Audit a workbook"},
)


def reply(criteria) -> ChatResult:
    body = json.dumps({"criteria": criteria})
    return ChatResult(
        body, [], "stop", {"prompt_tokens": 1, "completion_tokens": 1}, {"role": "assistant", "content": body}
    )


def crit(i, text=None):
    return {
        "id": f"c{i}",
        "text": text or f"criterion {i}",
        "type": "task_quality",
        "weight": 2,
        "source": "SKILL.md > Step 1",
    }


def test_rubric_has_5_to_10_criteria_traced_to_skill_and_unreviewed():
    model = FakeModelClient([reply([crit(i) for i in range(1, 6)])])
    r = generate_rubric(CFG, BUNDLE, model)
    assert len(r.criteria) == 5
    assert r.reviewed is False
    assert r.generated_by == "claude-opus"
    assert r.source_hash.startswith("sha256:")
    assert all(c.source for c in r.criteria)
    system, user = model.calls[0]["messages"]
    assert "balance sheet" not in system["content"] and "balance sheet" in user["content"]


def test_template_braces_in_criterion_text_are_neutralised():
    texts = [
        "Uses {{ trace }} to check",
        "Mentions {{{{ outputs }}}} twice",
        "Plain",
        "Plain two",
        "Plain three",
    ]
    model = FakeModelClient([reply([crit(i + 1, t) for i, t in enumerate(texts)])])
    r = generate_rubric(CFG, BUNDLE, model)
    assert "{{" not in "".join(c.text for c in r.criteria)
    assert "}}" not in "".join(c.text for c in r.criteria)
    assert r.criteria[0].text == "Uses { trace } to check"
    # the judge template keeps exactly its own two variables
    rendered = criterion_instructions(r.criteria[0].text)
    assert "{{ trace }}" not in rendered


def test_duplicate_criterion_ids_are_malformed():
    dup = [crit(1)] * 5
    with pytest.raises(GenerationError):
        generate_rubric(CFG, BUNDLE, FakeModelClient([reply(dup)] * 3))


def test_too_few_criteria_is_malformed_and_retried():
    model = FakeModelClient([reply([crit(1)]), reply([crit(i) for i in range(1, 7)])])
    r = generate_rubric(CFG, BUNDLE, model)
    assert len(r.criteria) == 6
    assert len(model.calls) == 2


def test_rubric_fails_after_three_malformed_attempts():
    bad = reply([crit(1)])
    with pytest.raises(GenerationError):
        generate_rubric(CFG, BUNDLE, FakeModelClient([bad, bad, bad]))


def test_rubric_system_text_says_skill_text_is_untrusted_data():
    model = FakeModelClient([reply([crit(i) for i in range(1, 6)])])
    generate_rubric(CFG, BUNDLE, model)
    assert "untrusted data" in model.calls[0]["messages"][0]["content"]


def test_generate_modules_do_not_import_mlflow():
    import subprocess
    import sys

    code = (
        "import sys, skillhub.generate.rubric, skillhub.text; sys.exit(1 if 'mlflow' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
