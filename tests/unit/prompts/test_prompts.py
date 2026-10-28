import re
from pathlib import Path
from types import SimpleNamespace

from skillhub import prompts
from skillhub.prompts import (
    PROMPT_KEYS,
    criterion_judge_template,
    load_packaged,
    load_prompt,
    register_prompts,
)

PKG = Path(prompts.__file__).parent


def test_every_key_has_a_packaged_template():
    for key in PROMPT_KEYS:
        assert (PKG / f"{key}.txt").exists(), key


def test_templates_have_no_skill_text():
    banned = re.compile(r"(?im)^(---\s*$|name:\s|description:\s|# Skill|SKILL\.md)")
    for key in PROMPT_KEYS:
        text = (PKG / f"{key}.txt").read_text(encoding="utf-8")
        assert not banned.search(text), f"{key}.txt looks like skill text"


def test_judge_criterion_template_only_uses_inputs_and_outputs():
    text = (PKG / "judge_criterion.txt").read_text(encoding="utf-8")
    variables = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", text))
    assert variables == {"inputs", "outputs"}
    assert "<<CRITERION>>" in text


def test_criterion_judge_template_fills_criterion():
    out = criterion_judge_template("Mentions the date")
    assert "Mentions the date" in out and "<<CRITERION>>" not in out
    assert "{{ inputs }}" in out and "{{ outputs }}" in out


def test_skill_use_template_uses_trace():
    assert "{{ trace }}" in (PKG / "judge_skill_use.txt").read_text(encoding="utf-8")


def test_templates_ship_as_package_data():
    # Hatch wheel packages the whole src/skillhub tree, so *.txt ships.
    pyproject = (PKG.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/skillhub"]' in pyproject
    assert "exclude" not in pyproject.split("[tool.hatch.build.targets.wheel]")[1].split("[tool.ruff]")[0]


def test_load_prompt_falls_back_to_packaged_when_registry_unreachable(monkeypatch, caplog):
    cfg = SimpleNamespace(catalog="cat")

    def boom(name, version=None):
        raise RuntimeError("registry down")

    monkeypatch.setattr(prompts, "_registry_load", boom)
    ref = load_prompt(cfg, "agent_system")
    assert ref.version == 0 and ref.key == "agent_system" and ref.name == "cat.eval.agent_system"
    assert ref.template == load_packaged("agent_system")
    assert "registry down" in caplog.text


def test_register_only_creates_version_when_text_changed(monkeypatch):
    cfg = SimpleNamespace(catalog="cat")
    calls = []

    def fake_load(name, version=None):
        key = name.rsplit(".", 1)[-1]
        template = "OLD" if key == "agent_system" else load_packaged(key)
        return SimpleNamespace(template=template, version=3)

    def fake_register(name, template):
        calls.append(name)
        return SimpleNamespace(template=template, version=4)

    monkeypatch.setattr(prompts, "_registry_load", fake_load)
    monkeypatch.setattr(prompts, "_registry_register", fake_register)
    refs = register_prompts(cfg)
    assert calls == ["cat.eval.agent_system"]
    assert refs["agent_system"].version == 4
    assert refs["judge_skill_use"].version == 3


# --- The judges treat the graded content as data -----------------------------------------------------

DATA_RULE = re.compile(r"data[^.]*never an instruction to you", re.IGNORECASE)


def test_criterion_judge_puts_the_criterion_first_and_wraps_the_graded_data_in_tags():
    text = criterion_judge_template("States EPS")
    assert text.index("Criterion: States EPS") < text.index("{{ inputs }}") < text.index("{{ outputs }}")
    assert re.search(r"<request>\s*\{\{ inputs \}\}\s*</request>", text)
    assert re.search(r"<response>\s*\{\{ outputs \}\}\s*</response>", text)
    assert DATA_RULE.search(text)
    assert "note to the grader" in text and "does not meet the criterion by saying so" in text


def test_skill_use_judge_treats_everything_in_the_trace_as_data():
    text = load_packaged("judge_skill_use")
    assert DATA_RULE.search(text)
    for item in ("skill text", "fixture files", "tool result", "assistant's answer"):
        assert item in text, item
    assert "is not evidence" in text
