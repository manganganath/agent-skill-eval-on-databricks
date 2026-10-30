# Onboarding

Authors write skills, reviewers mark cases as reviewed and switch versions, and readers use skills. Who approves changes on GitHub is a separate setting. See Approval gates in `docs/operations.md`.

| Role | Group | Can do |
|---|---|---|
| Author | `skillhub-authors` | Open pull requests with skills and cases. Upload drafts, and see and evaluate candidates (skills under review) in the Skill Hub, in the departments they can read |
| Reviewer | `skillhub-reviewers` | Mark generated cases and rubrics as reviewed, switch versions in the Skill Hub, upload drafts, see and evaluate the candidates of every department, and read the cross-department results and the Dashboard tab |
| Reader | The department group named in `config/hub.yaml` (for example `dept-equity-research`), or `skillhub-users` | Load skills through agents and browse them in the Skill Hub |

The customer's administrator grants group membership. Reviewers see the live skills of a department only through its reader group. So add `skillhub-reviewers` to every department's reader group, or reviewers cannot browse or switch that department's live skills.

## Roles in the Skill Hub

The Skill Hub shows the same table on its Access page, with your own roles marked. Each role adds to the one before it.

| Action | Reader | Author | Reviewer |
|---|---|---|---|
| Browse live skills | Own departments | Own departments | All departments |
| Try a live skill | Yes | Yes | Yes |
| Run an eval on live skills | Yes | Yes | Yes |
| See skills under review | No | Own departments | All departments |
| Upload a draft | No | Yes | Yes |
| Discard a draft | No | Their own | Any draft |
| Submit a draft for review | No | Their own | Any draft |
| Try a skill under review | No | Own departments | All departments |
| Run an eval on a candidate or draft | No | Yes | Yes |
| Merge a pull request from the Hub | No | No | Yes, when its gate passed |
| Switch the live version | No | No | Yes |
| Add or edit a skill | No | Through a pull request | Through a pull request |
| Approve a release | In GitHub: the required reviewers of the `production` environment, not a Skill Hub role | | |
| Publish a version | No one: CI publishes after approval | | |

Nobody publishes a version from the app, and skills are not edited in it. To change a skill, open a pull request that changes `skills/<department>/<skill>/`. The pull request's evaluation runs by itself, and a failed trigger eval blocks the merge. After the merge, the required reviewers of the GitHub `production` environment approve the release, and CI publishes the new version and makes it live.

## Add a skill

1. Create a branch.
2. Create `skills/<department>/<name>/SKILL.md`. The `name` in the frontmatter must equal the folder name, and it must match `^[a-z0-9]+(?:-[a-z0-9]+)*$`. Add `references/` files as needed.
3. Run `uv run skillhub lint`. Lint checks the frontmatter, the name pattern, that names are unique across departments, that the department folder is a key in `config/hub.yaml`, and that case IDs are unique and in range. It also reports a per-skill threshold that is looser than the default in `config/hub.yaml`. A skill can only tighten the gate.
4. Run `uv run skillhub generate --skills <name>`. It calls the generator model, so set `SKILLHUB_CATALOG` and `SQL_WAREHOUSE_ID` and sign in to Databricks first. It writes `evals/evals.json` with trigger cases and `evals/rubric.json` with 5 to 10 criteria. Run it once per skill: each run without `--force` adds another batch of trigger cases, and `--force` replaces the generated ones. Trigger case IDs start at 1, and effectiveness case IDs start at 101. `generate` never overwrites an existing `rubric.json` without `--force`. Test cases and rubrics stay in `evals/` in the repository. They are never uploaded to the skill bundle, so the agent under test cannot read them.
5. Review them. Check that each positive case is a realistic request the skill should handle, each near-miss is a request that a sibling skill should take, and each unrelated case is unrelated. Check that each rubric criterion can be checked from the output, and that its `source` points at a real part of `SKILL.md`. Mark what you checked with `uv run python scripts/mark_reviewed.py --skill <name> --ids 1,2,3 --rubric`. A member of the reviewers group still has to review them. On GitHub, the `production` environment reviewer approves publish and switch. Code owners are asked to review `evals/**` changes, but GitHub enforces that only if you turn on code-owner review.
6. Add effectiveness cases (`kind: effectiveness`) only where the skill's output can be checked against fixtures. Each has a realistic `prompt`, two to four `assertions`, and `fixtures` under `fixtures/`.
7. Add a unit test for the skill's files if it needs one. For example, `tests/unit/test_skill_<name>.py` asserts that the skill's cases load and that the rubric has 5 to 10 criteria.
8. Run `uv run pytest tests/unit -q`. `tests/unit/test_generated_cases.py` checks every skill: only generated trigger cases (6 to 10 positive, 3 to 5 near-misses, 2 to 4 unrelated), and a rubric generated from the current skill content. After you change the skill's files, run `uv run skillhub generate --skills <name> --rubric-only --force`. The `platform` check runs these tests and must pass before a merge.
9. Open a pull request. Its description must say what the skill is for. The PR pipeline runs lint, the trigger tests, and the effectiveness tests for skills that have them. It then posts a matrix comment. A `fail` blocks merge. An `inconclusive` check needs a re-run. An `advisory` note means the skill lacks enough reviewed cases. While the pull request is open, the skill is also a candidate in the Skill Hub; see [Evaluate a new skill](#evaluate-a-new-skill).
10. After merge to `main`, the publish workflow re-evaluates every changed skill, and stops on `fail` or `inconclusive`. Otherwise it waits for approval in the `production` environment. After approval it starts the `skill-switch` Job, which registers a new version and switches it live. The Job publishes only content that the cited eval evaluated. When the pull request changed only `skills/`, was up to date with `main`, only trigger verdicts gate (`defaults.gating_modes: [trigger]`), and its gate passed on its last commit, the workflow reuses that gate's results instead of evaluating again, so the approval request comes a few minutes after the merge (see [Architecture](architecture.md#data-flow)).

## Evaluate a new skill

A new or changed skill can be evaluated before it is released: in its pull request, in the Skill Hub while the pull request is open, or in the Skill Hub as a draft before there is any pull request.

**In the pull request.** Open a pull request as in [Add a skill](#add-a-skill). CI uploads the pull request's skills to a candidate folder, `<workspace_root>/<catalog>/candidates/<run key>`, with a run key such as `pr12_ab12cd3_a1`. It then runs the eval and posts the matrix comment. This eval is the merge gate.

**In the Skill Hub.** Authors and reviewers see the same upload as a candidate:

1. In the catalog, each skill that a pull request adds or changes is listed in its department, after the live skills, with a **Candidate · PR #N** badge. Set the **Status** filter to **Under review** to see only these, or to **Live** to hide them. Only the newest upload of each pull request is listed.
2. Open a candidate. The banner says **Not released. Under review in PR #N.** The page shows `SKILL.md` and the other files, the test cases and rubric from the pull request's `evals/` folder, and the eval results of this upload.
3. Select **Run eval**. The run page opens with the candidate selected. Choose models and modes, then start the evaluation. One run evaluates live skills, or candidates from one upload, not both, and a candidate run cannot pin versions.

A Skill Hub run gets its own run key (`<candidate run key>_h<8 hex digits>`) and `trigger_source` `manual`, so it never changes the pull request's gate. Its results appear on the candidate's page next to the pull request's own eval. To change the skill or its cases, push to the pull request; the next upload replaces the candidate.

**Try it.** On a candidate's or draft's page, **Try this skill** runs the upload as you, in place of the live version, and nobody else can load it.

Who sees a candidate: reviewers see every department. An author sees a candidate only in a department whose live skills they can read, and always sees their own drafts. Everyone else sees no candidates. The nightly cleanup removes a candidate or draft folder 7 days after its upload.

**As a draft.** To try a skill before you open a pull request:

1. In the catalog, select **Upload draft**. Choose the department, then a zip of the skill folder or the folder itself. The upload needs `SKILL.md` with `name` and `description` in its frontmatter and `evals/evals.json`. `evals/rubric.json` is optional; without it the draft runs in trigger mode only. A `fixtures/` folder in the upload replaces the default fixtures for this draft.
2. The Skill Hub checks the skill as `uv run skillhub lint` does. If something is wrong, the form lists every problem; fix them and upload again.
3. The draft's page opens, with a **Draft** badge and the banner **Not released. Draft uploaded by ...**. Select **Run eval** to evaluate it.

A draft is never released. To release the skill, submit the draft for review from its page, or add it to `skills/` in a pull request, as in [Add a skill](#add-a-skill).

**Submit a draft for review.** On the draft's page, select **Submit for review**, then confirm. The Skill Hub opens a pull request that puts the draft's files under `skills/<department>/<name>/`, replacing what is there. The draft's `fixtures/` stay in the Skill Hub; the pull request carries the skill only. The page then shows **Submitted as PR #N** and the release path, with five steps:

1. **Pull request opened.** The link opens it on GitHub.
2. **Eval gate.** The `skills-pr` check runs on the pull request by itself. A failed trigger eval blocks the merge.
3. **Merged.** Once the gate has passed, a reviewer selects **Merge pull request** on the release path and confirms. The merge is a merge commit.
4. **Release approved in GitHub.** The `production` environment's required reviewers approve the release in GitHub. The step links to the waiting run.
5. **Live.** When the switch has run, the step shows the live version and links to the skill.

If the draft has no `evals/rubric.json`, CI generates it in the eval gate, and the pull request says so. The release path refreshes by itself while a step is pending. If GitHub is not set up for the Skill Hub yet, the page says so, and an administrator sets it up as described in [GitHub token](operations.md#github-token-for-the-skill-hub).

## Retire a skill

Delete the folder `skills/<department>/<name>/` in a pull request. After the merge and the approval in the `production` environment, the `skill-switch` Job retires the skill. Its versions are kept.

## Import another skill repository

1. Find a source repository with a commit to pin. Use `github.com/<owner>/<repo>@<full-sha>`. Branch names are not allowed.
2. Edit `config/hub.yaml`, in the `import:` block. Set `source`, add the ordered `map` (the first matching path wins, so put specific paths first), and add to `exclude` any skill names to drop. A new department must also be added under `departments`, with its `readers` group, before it can be a `map` target.
3. Run `uv run skillhub import --dest skills` (or `--source-dir <checkout>` for a local checkout at the pinned commit). It reads `config/hub.yaml`, so set `SKILLHUB_CATALOG` and `SQL_WAREHOUSE_ID` first. Import fails on purpose, and writes nothing, in these cases: a skill copy that is not byte-identical to another copy of the same name, a skill found only under paths that the `map` does not cover, a `map` entry that names an unknown department, a source without a `LICENSE` file, and a `source` that is not pinned to a full commit SHA. Fix the `map` or add an `exclude` entry. Do not edit the upstream text.
4. Check `THIRD_PARTY_NOTICES`. Import writes the source's `LICENSE` and `NOTICE` into a generated block there. Import does not check the license itself: only Apache-2.0 and compatible licenses are accepted in this release, so check the source's `LICENSE` file for each plugin before you map it.
5. Run `uv run skillhub generate --missing`, then `uv run skillhub lint` and `uv run pytest tests/unit -q`. The tests check the shipped example skills: `tests/unit/test_review_status.py` and `tests/unit/test_effectiveness_cases.py` name six of them, and `tests/unit/test_generated_cases.py` checks every skill. If the import removes or changes those skills, update these tests in the same pull request, and say why in its description.
6. Open a pull request. Partner-built skills are imported into the quarantined `external` department. Nothing in `external` can be released in this release. CI refuses a change that moves a skill out of a quarantined department. An operator must do that; see `docs/operations.md`.
