"""The Skill Hub's pull request flow as rules over the GitHub client (backend.github). A draft becomes a pull request
against main. A pull request's gate and release are read from GitHub, and a reviewer may merge it from the Hub when
the rules below hold. The app never approves the production release: that stays with the reviewers of the production
environment in GitHub.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from backend import candidates, github
from backend.github import GitHubClient, Pull, Run
from skillhub.bundle import is_ignored
from skillhub.config import HubConfig

BASE = "main"
SKILLS = "skills"
BRANCH_PREFIX = "skillhub/"
GATE_CHECK = "skills-pr"  # the check run of skills-pr.yml, named by its job
REQUIRED_CHECKS = (GATE_CHECK, "platform")
PUBLISH_WORKFLOW = "skills-publish.yml"
HUB_MARKER = "This pull request was opened from the Skill Hub."
FILE_MODE = "100644"
BLOB_WORKERS = 8


class AlreadySubmitted(Exception):
    """The draft's branch already has a pull request: the draft was submitted before."""

    def __init__(self, pull: Pull):
        self.pull = pull
        super().__init__(f"pull request {pull.number} exists for this draft")


class NoChanges(Exception):
    """main already holds the skill folder exactly as the draft has it, so a pull request would change nothing."""


@dataclass(frozen=True)
class Submission:
    pull: Pull
    branch: str


def skill_folder(dept: str, skill: str) -> str:
    return f"{SKILLS}/{dept}/{skill}"


def branch_name(skill: str, draft_id: str) -> str:
    """skillhub/<skill>-<the last 8 hex digits of the draft id>: unique per draft, and readable in the branch list."""
    return f"{BRANCH_PREFIX}{skill}-{draft_id[-8:]}"


def title(dept: str, skill: str, *, update: bool) -> str:
    return f"{'Update' if update else 'Add'} {skill} ({dept}) from the Skill Hub"


def commit_message(skill: str, submitter: str) -> str:
    return f"feat(skills): {skill} from the Skill Hub\n\nSubmitted-by: {submitter}"


def pull_body(dept: str, skill: str, run_key: str, submitter: str, *, has_rubric: bool) -> str:
    lines = [
        f"Submitted by {submitter} from the draft `{run_key}` in the Skill Hub.",
        "",
        f"Skill folder: `{skill_folder(dept, skill)}/`",
        "",
        "What happens next:",
        "",
        "1. The eval gate (`skills-pr`) runs on this pull request by itself.",
        "2. A reviewer merges it once the gate passes.",
        "3. The release is approved in GitHub, in the production environment. CI then publishes the new version.",
    ]
    if not has_rubric:
        lines += [
            "",
            "The draft has no `evals/rubric.json`. CI generates the missing rubric in the eval gate.",
        ]
    lines += ["", HUB_MARKER]
    return "\n".join(lines)


def blob_sha(data: bytes) -> str:
    """The Git blob id of a file, as GitHub lists it in a tree."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - Git's object id, not a secret


def _folder_files(gh: GitHubClient, root_tree: str, folder: str) -> dict[str, str]:
    """{path relative to the folder: blob sha} for the skill folder on main. {} when the folder is not there."""
    tree = root_tree
    for part in folder.split("/"):
        entry = next((e for e in gh.tree(tree) if e["path"] == part and e["type"] == "tree"), None)
        if entry is None or not entry["sha"]:
            return {}
        tree = entry["sha"]
    return {
        e["path"]: str(e["sha"])
        for e in gh.tree(tree, recursive=True)
        if e["type"] == "blob" and not is_ignored(e["path"])
    }


def open_pull(
    gh: GitHubClient,
    dept: str,
    skill: str,
    draft_id: str,
    run_key: str,
    files: dict[str, bytes],
    submitter: str,
    *,
    has_rubric: bool,
) -> Submission:
    """Open the pull request for a draft: a commit on a new branch from main's head, in which the skill folder holds the
    draft's files and nothing else. A file of the skill on main that the draft lacks is deleted. Raises NoChanges, before
    anything is written, when main already has the folder as the draft has it, and AlreadySubmitted when the branch
    already has a pull request."""
    folder = skill_folder(dept, skill)
    head = gh.ref_sha(BASE)
    base_tree = gh.commit_tree(head)
    on_main = _folder_files(gh, base_tree, folder)
    if on_main and on_main == {rel: blob_sha(data) for rel, data in files.items()}:
        raise NoChanges()
    with ThreadPoolExecutor(max_workers=BLOB_WORKERS) as pool:
        blobs = dict(zip(files, pool.map(gh.create_blob, files.values()), strict=True))
    entries: list[dict] = [
        {"path": f"{folder}/{rel}", "mode": FILE_MODE, "type": "blob", "sha": sha}
        for rel, sha in blobs.items()
    ]
    # A null sha removes the path from the tree, so the folder ends up as the draft is.
    entries += [
        {"path": f"{folder}/{rel}", "mode": FILE_MODE, "type": "blob", "sha": None}
        for rel in on_main
        if rel not in files
    ]
    tree = gh.create_tree(base_tree, entries)
    commit = gh.create_commit(commit_message(skill, submitter), tree, head)
    branch = branch_name(skill, draft_id)
    try:
        gh.create_ref(branch, commit)
    except github.GitHubRejected:
        existing = gh.pulls_for_branch(branch)
        if existing:
            raise AlreadySubmitted(existing[0]) from None
        gh.update_ref(branch, commit)  # the branch is left from an attempt that never opened a pull request
    pull = gh.create_pull(
        title(dept, skill, update="SKILL.md" in on_main),
        branch,
        BASE,
        pull_body(dept, skill, run_key, submitter, has_rubric=has_rubric),
    )
    return Submission(pull, branch)


# --- the gate and the release, as GitHub reports them -------------------------------------------------------------


def gate_state(checks: dict[str, str]) -> str:
    """pending, passed, failed, or none when the gate has not run on the commit."""
    return checks.get(GATE_CHECK, "none")


def release_state(run: Run | None) -> str:
    """The release of a merge commit: not_started, running, waiting_approval, succeeded or failed."""
    if run is None:
        return "not_started"
    if run.waiting:
        return "waiting_approval"
    if run.status != "completed":
        return "running"
    return "succeeded" if run.conclusion == "success" else "failed"


# --- merging from the Hub ---------------------------------------------------------------------------------------------


def skill_of(path: str, depts: Collection[str]) -> tuple[str, str] | None:
    """(department, skill) for a path under skills/<department>/<skill>/, or None for any other path."""
    parts = path.split("/")
    if (
        len(parts) < 4
        or parts[0] != SKILLS
        or parts[1] not in depts
        or not candidates.valid_skill_name(parts[2])
    ):
        return None
    return parts[1], parts[2]


def unmet_merge_rules(
    pull: Pull, paths: list[str], checks: dict[str, str], cfg: HubConfig, visible: set[str]
) -> list[str]:
    """The rules a merge from the Hub does not meet, by name. Empty when the pull request may be merged now.

    The pull request is open. It touches only skills/<department>/<skill>/ paths. It was opened from the Skill Hub, or
    it names only skills the reviewer may see. Every required check passed. Its mergeable state is clean: GitHub
    reports that only when branch protection is satisfied, and the token owner may be an administrator who could
    bypass it, so the rule is checked here as well."""
    unmet: list[str] = []
    if pull.state != "open" or pull.merged:
        unmet.append("open")
    named = [skill_of(path, cfg.departments) for path in paths]
    if not paths or any(n is None for n in named):
        unmet.append("only_skills")
    skills = {n for n in named if n is not None}
    if HUB_MARKER not in pull.body and not (skills and all(dept in visible for dept, _ in skills)):
        unmet.append("from_hub_or_visible_skill")
    unmet += [f"check:{name}" for name in REQUIRED_CHECKS if checks.get(name) != "passed"]
    if pull.mergeable_state != "clean":
        unmet.append("clean")
    return unmet
