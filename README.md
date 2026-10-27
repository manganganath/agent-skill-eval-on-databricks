# Agent Skill Eval on Databricks

Register agent skills (`SKILL.md` bundles) in Unity Catalog, serve them to agents through Unity Gateway under Unity Catalog permissions, and evaluate every skill version against any set of models, with the evidence kept in MLflow and Delta.

**Status:** under active development.

## What it does

- **Skills registry:** common and department-specific skills as Unity Catalog securables. Versions can be published, switched and rolled back.
- **Skill evaluation:** whether a skill is invoked when it should be (and not when it shouldn't), and how much it improves results compared with the same model without the skill. Results form a matrix across skill versions and models, scored with MLflow judges.
- **CI/CD:** GitHub Actions evaluate every skill change on its pull request. A reviewer approves the new version before it goes live.
- **Skill Hub:** a Databricks App for browsing skills, running evals, reviewing results and switching versions, governed by Unity Catalog permissions.

## Repository layout (planned)

| Path | Contents |
|---|---|
| `skills/` | Skill bundles by department, with their evaluation cases and rubrics |
| `config/hub.yaml` | Deployment settings: catalog, departments, model profiles, thresholds |
| `src/skillhub/` | Python package and `skillhub` CLI |
| `app/` | Skill Hub app |
| `resources/`, `databricks.yml` | Databricks bundle: jobs, app, schemas |
| `.github/workflows/` | CI/CD pipelines |
| `docs/` | Architecture, operations, API and onboarding guides |

Example skills are imported from [anthropics/financial-services](https://github.com/anthropics/financial-services) under the Apache-2.0 license; see `THIRD_PARTY_NOTICES`.

## License

Apache-2.0; see `LICENSE`.
