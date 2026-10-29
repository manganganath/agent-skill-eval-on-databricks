# Onboarding

Authors write skills, reviewers mark cases as reviewed and switch versions, and readers use skills. Who approves changes on GitHub is a separate setting. See Approval gates in `docs/operations.md`.

| Role | Group | Can do |
|---|---|---|
| Author | `skillhub-authors` | Open pull requests with skills and cases |
| Reviewer | `skillhub-reviewers` | Mark generated cases and rubrics as reviewed, switch versions in the Skill Hub, and read the cross-department results and the dashboard |
| Reader | The department group named in `config/hub.yaml` (for example `dept-equity-research`), or `skillhub-users` | Load skills through agents and browse them in the Skill Hub |

The customer's administrator grants group membership.

## Add a skill

1. Create a branch.
2. Create `skills/<department>/<name>/SKILL.md`. The `name` in the frontmatter must equal the folder name, and it must match `^[a-z0-9]+(?:-[a-z0-9]+)*$`. Add `references/` files as needed.
3. Run `uv run skillhub lint`. Lint checks the frontmatter, the name pattern, that names are unique across departments, that the department folder is a key in `config/hub.yaml`, and that case IDs are unique and in range. It also reports a per-skill threshold that is looser than the default in `config/hub.yaml`. A skill can only tighten the gate.
4. Run `uv run skillhub generate --skills <name>`. It writes `evals/evals.json` with trigger cases and `evals/rubric.json` with 5 to 10 criteria. Trigger case IDs start at 1, and effectiveness case IDs start at 101. `generate` never overwrites an existing `rubric.json` without `--force`. Test cases and rubrics stay in `evals/` in the repository. They are never uploaded to the skill bundle, so the agent under test cannot read them.
5. Review them. Check that each positive case is a realistic request the skill should handle, each near-miss is a request that a sibling skill should take, and each unrelated case is unrelated. Check that each rubric criterion can be checked from the output, and that its `source` points at a real part of `SKILL.md`. Mark what you checked with `uv run python scripts/mark_reviewed.py --skill <name> --ids 1,2,3 --rubric`. A member of the reviewers group still has to review them. On GitHub, the `production` environment reviewer approves publish and switch. Code owners are asked to review `evals/**` changes, but GitHub enforces that only if you turn on code-owner review.
6. Add effectiveness cases (`kind: effectiveness`) only where the skill's output can be checked against fixtures. Each has a realistic `prompt`, two to four `assertions`, and `fixtures` under `fixtures/`.
7. Add a unit test for the skill's files if it needs one. For example, `tests/unit/test_skill_<name>.py` asserts that the skill's cases load and that the rubric has 5 to 10 criteria.
8. Run `uv run pytest tests/unit -q`.
9. Open a pull request. Its description must say what the skill is for. The PR pipeline runs lint, the trigger tests, and the effectiveness tests for skills that have them. It then posts a matrix comment. A `fail` blocks merge. An `inconclusive` check needs a re-run. An `advisory` note means the skill lacks enough reviewed cases.
10. After merge to `main`, the publish workflow re-evaluates every changed skill, and stops on `fail` or `inconclusive`. Otherwise it waits for approval in the `production` environment. After approval it starts the `skill-switch` Job, which registers a new version and switches it live. The Job publishes only content that the merge eval evaluated.

## Retire a skill

Delete the folder `skills/<department>/<name>/` in a pull request. After the merge and the approval in the `production` environment, the `skill-switch` Job retires the skill. Its versions are kept.

## Import another skill repository

1. Find a source repository with a commit to pin. Use `github.com/<owner>/<repo>@<full-sha>`. Branch names are not allowed.
2. Edit `config/hub.yaml`, in the `import:` block. Set `source`, add the ordered `map` (the first matching path wins, so put specific paths first), and add to `exclude` any skill names to drop. A new department must also be added under `departments`, with its `readers` group, before it can be a `map` target.
3. Run `uv run skillhub import --dest skills` (or `--source-dir <checkout>` for a local checkout at the pinned commit). Import fails on purpose in two cases: a skill copy that is not byte-identical to another copy of the same name, and a skill found only under paths that the `map` does not cover. Fix the `map` or add an `exclude` entry. Do not edit the upstream text.
4. Check `THIRD_PARTY_NOTICES` and add the source's license notice. Only Apache-2.0 and compatible licenses are accepted in this release. Check the source's `LICENSE` file for each plugin before you map it.
5. Run `uv run skillhub lint` and `uv run pytest tests/unit -q`. Update the count and department assertions in `tests/unit/test_imported_skills.py` in the same pull request, and say why in its description.
6. Open a pull request. Partner-built skills are imported into the quarantined `external` department. Nothing in `external` can be released in this release. CI refuses a change that moves a skill out of a quarantined department. An operator must do that; see `docs/operations.md`.
