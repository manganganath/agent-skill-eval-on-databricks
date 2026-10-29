# Skills

Each skill lives in its own folder under its department:

```
skills/<department>/<skill>/
  SKILL.md          the skill, with `name` and `description` in its frontmatter
  references/       optional files the skill loads
  evals/
    evals.json      trigger and effectiveness cases
    rubric.json     the grading rubric
```

- `<department>` is a key under `departments` in `config/hub.yaml`.
- The folder name must equal the `name` in `SKILL.md`.
- Run `uv run skillhub lint` before you open a pull request.
- See `docs/onboarding.md` to add a skill, and `docs/operations.md` for how pull requests evaluate and publish skills.
