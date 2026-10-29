# Onboarding

Authors write skills, reviewers mark cases as reviewed and switch versions, and readers use skills. Who approves changes on GitHub is a separate setting. See Approval gates in `docs/operations.md`.

| Role | Group | Can do |
|---|---|---|
| Author | `skillhub-authors` | Open pull requests with skills and cases. Upload drafts, and see and evaluate candidates (skills under review) in the Skill Hub, in the departments they can read |
| Reviewer | `skillhub-reviewers` | Mark generated cases and rubrics as reviewed, switch versions in the Skill Hub, upload drafts, see and evaluate the candidates of every department, and read the cross-department results and the dashboard |
| Reader | The department group named in `config/hub.yaml` (for example `dept-equity-research`), or `skillhub-users` | Load skills through agents and browse them in the Skill Hub |

The customer's administrator grants group membership.

## Roles in the Skill Hub

The Skill Hub shows the same table on its Access page, with your own roles marked. Each role adds to the one before it.

| Action | Everyone signed in | Authors | Reviewers |
|---|---|---|---|
| Browse live skills | Own departments | Own departments | All departments |
| Try a skill | Yes | Yes | Yes |
| Run an eval on live skills | Yes | Yes | Yes |
| See skills under review | No | Own departments | All departments |
| Upload a draft | No | Yes | Yes |
| Run an eval on a candidate or draft | No | Yes | Yes |
| Switch the live version | No | No | Yes |
| Add or edit a skill | No | Through a pull request | Through a pull request |
| Approve a release | No | No | In GitHub, as a required reviewer |
| Publish a version | No one: CI publishes after approval | | |

Nobody publishes a version from the app, and skills are not edited in it. To change a skill, open a pull request that changes `skills/<department>/<skill>/`. The pull request's evaluation runs by itself, and a failed trigger eval blocks the merge. After the merge, the required reviewers of the GitHub `production` environment approve the release, and CI publishes the new version and makes it live.

## Add a skill

1. Create a branch.
2. Create `skills/<department>/<name>/SKILL.md`. The `name` in the frontmatter must equal the folder name, and it must match `^[a-z0-9]+(?:-[a-z0-9]+)*$`. Add `references/` files as needed.
3. Run `uv run skillhub lint`. Lint checks the frontmatter, the name pattern, that names are unique across departments, that the department folder is a key in `config/hub.yaml`, and that case IDs are unique and in range. It also reports a per-skill threshold that is looser than the default in `config/hub.yaml`. A skill can only tighten the gate.
4. Run `uv run skillhub generate --skills <name>`. It writes `evals/evals.json` with trigger cases and `evals/rubric.json` with 5 to 10 criteria. Trigger case IDs start at 1, and effectiveness case IDs start at 101. `generate` never overwrites an existing `rubric.json` without `--force`. Test cases and rubrics stay in `evals/` in the repository. They are never uploaded to the skill bundle, so the agent under test cannot read them.
5. Review them. Check that each positive case is a realistic request the skill should handle, each near-miss is a request that a sibling skill should take, and each unrelated case is unrelated. Check that each rubric criterion can be checked from the output, and that its `source` points at a real part of `SKILL.md`. Mark what you checked with `uv run python scripts/mark_reviewed.py --skill <name> --ids 1,2,3 --rubric`. A member of the reviewers group still has to review them. On GitHub, the `production` environment reviewer approves publish and switch. Code owners are asked to review `evals/**` changes, but GitHub enforces that only if you turn on code-owner review.
6. Add effectiveness cases (`kind: effectiveness`) only where the skill's output can be checked against fixtures. Each has a realistic `prompt`, two to four `assertions`, and `fixtures` under `fixtures/`.
7. Add a unit test for the skill's files if it needs one. For example, `tests/unit/test_skill_<name>.py` asserts that the skill's cases load and that the rubric has 5 to 10 criteria.
8. Run `uv run pytest tests/unit -q`.
9. Open a pull request. Its description must say what the skill is for. The PR pipeline runs lint, the trigger tests, and the effectiveness tests for skills that have them. It then posts a matrix comment. A `fail` blocks merge. An `inconclusive` check needs a re-run. An `advisory` note means the skill lacks enough reviewed cases. While the pull request is open, the skill is also a candidate in the Skill Hub; see [Evaluate a new skill](#evaluate-a-new-skill).
10. After merge to `main`, the publish workflow re-evaluates every changed skill, and stops on `fail` or `inconclusive`. Otherwise it waits for approval in the `production` environment. After approval it starts the `skill-switch` Job, which registers a new version and switches it live. The Job publishes only content that the merge eval evaluated.

## Evaluate a new skill

A new or changed skill can be evaluated before it is released: in its pull request, in the Skill Hub while the pull request is open, or in the Skill Hub as a draft before there is any pull request.

**In the pull request.** Open a pull request as in [Add a skill](#add-a-skill). CI uploads the pull request's skills to a candidate folder, `<workspace_root>/<catalog>/candidates/<run key>`, with a run key such as `pr12_ab12cd3_a1`. It then runs the eval and posts the matrix comment. This eval is the merge gate.

**In the Skill Hub.** Authors and reviewers see the same upload as a candidate:

1. In the catalog, each skill that a pull request adds or changes is listed in its department, after the live skills, with a **Candidate · PR #N** badge. Set the **Status** filter to **Under review** to see only these, or to **Live** to hide them. Only the newest upload of each pull request is listed.
2. Open a candidate. The banner says **Not released. Under review in PR #N.** The page shows `SKILL.md` and the other files, the test cases and rubric from the pull request's `evals/` folder, and the eval results of this upload.
3. Select **Run eval**. The run page opens with the candidate selected. Choose models and modes, then start the evaluation. One run evaluates live skills, or candidates from one upload, not both, and a candidate run cannot pin versions.

A Skill Hub run gets its own run key (`<candidate run key>_h<8 hex digits>`) and `trigger_source` `manual`, so it never changes the pull request's gate. Its results appear on the candidate's page next to the pull request's own eval. To change the skill or its cases, push to the pull request; the next upload replaces the candidate.

Who sees a candidate: reviewers see every department. An author sees a candidate only in a department whose live skills they can read, and always sees their own drafts. Everyone else sees no candidates. The nightly cleanup removes a candidate or draft folder 7 days after its upload.

**As a draft.** To try a skill before you open a pull request:

1. In the catalog, select **Upload draft**. Choose the department, then a zip of the skill folder or the folder itself. The upload needs `SKILL.md` with `name` and `description` in its frontmatter and `evals/evals.json`. `evals/rubric.json` is optional; without it the draft runs in trigger mode only. A `fixtures/` folder in the upload replaces the default fixtures for this draft.
2. The Skill Hub checks the skill as `uv run skillhub lint` does. If something is wrong, the form lists every problem; fix them and upload again.
3. The draft's page opens, with a **Draft** badge and the banner **Not released. Draft uploaded by ...**. Select **Run eval** to evaluate it.

A draft is never released. To release the skill, add it to `skills/` in a pull request, as in [Add a skill](#add-a-skill).

## Retire a skill

Delete the folder `skills/<department>/<name>/` in a pull request. After the merge and the approval in the `production` environment, the `skill-switch` Job retires the skill. Its versions are kept.

## Import another skill repository

1. Find a source repository with a commit to pin. Use `github.com/<owner>/<repo>@<full-sha>`. Branch names are not allowed.
2. Edit `config/hub.yaml`, in the `import:` block. Set `source`, add the ordered `map` (the first matching path wins, so put specific paths first), and add to `exclude` any skill names to drop. A new department must also be added under `departments`, with its `readers` group, before it can be a `map` target.
3. Run `uv run skillhub import --dest skills` (or `--source-dir <checkout>` for a local checkout at the pinned commit). Import fails on purpose in two cases: a skill copy that is not byte-identical to another copy of the same name, and a skill found only under paths that the `map` does not cover. Fix the `map` or add an `exclude` entry. Do not edit the upstream text.
4. Check `THIRD_PARTY_NOTICES` and add the source's license notice. Only Apache-2.0 and compatible licenses are accepted in this release. Check the source's `LICENSE` file for each plugin before you map it.
5. Run `uv run skillhub lint` and `uv run pytest tests/unit -q`. Update the count and department assertions in `tests/unit/test_imported_skills.py` in the same pull request, and say why in its description.
6. Open a pull request. Partner-built skills are imported into the quarantined `external` department. Nothing in `external` can be released in this release. CI refuses a change that moves a skill out of a quarantined department. An operator must do that; see `docs/operations.md`.
