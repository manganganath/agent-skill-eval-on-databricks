from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import EntityTagAssignment

from skillhub import SkillHubError

ENTITY_TYPE = "skills"
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


class UnsafePath(SkillHubError, ValueError):
    """A bundle-relative path that is empty, absolute or escapes the bundle."""


def _check_rel(rel_path: str) -> str:
    parts = rel_path.split("/")
    if (
        not rel_path
        or rel_path.startswith("/")
        or "\\" in rel_path
        or any(ord(c) < 32 or ord(c) == 127 for c in rel_path)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise UnsafePath(f"unsafe bundle path: {rel_path!r}")
    return rel_path


# Names go into REST paths, so they are checked here as well as by their builders.
# A versioned name is `<skill>-v<N>`, so the last part may be longer than a skill name.
_SCHEMA_FQN = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
_SKILL_FQN = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9][a-z0-9_-]{0,79}")


def _check_fqn(fqn: str, pattern: re.Pattern[str] = _SKILL_FQN) -> str:
    if not isinstance(fqn, str) or not pattern.fullmatch(fqn):
        raise UnsafePath(f"unsafe Unity Catalog name: {fqn!r}")
    return fqn


@dataclass
class SkillInfo:
    fqn: str
    name: str
    bundle_name: str | None
    description: str | None
    etag: str
    finalize_time: str | None
    comment: str | None


def _info(d: dict[str, Any]) -> SkillInfo:
    fqn = d["name"].removeprefix("skills/")
    return SkillInfo(
        fqn=fqn,
        name=fqn.rsplit(".", 1)[1],
        bundle_name=d.get("bundle_name"),
        description=d.get("description"),
        etag=d.get("etag", ""),
        finalize_time=d.get("finalize_time"),
        comment=d.get("comment"),
    )


def _volume_path(fqn: str) -> str:
    """Files API path of a skill's bundle, /Skills/<cat>/<schema>/<skill>."""
    return "/Skills/" + _check_fqn(fqn).replace(".", "/")


class UCSkills:
    """Thin wrapper over the UC skills REST API and the Files API.

    Every JSON write sends Content-Type application/json. Etags go in the query string.
    """

    def __init__(self, w: WorkspaceClient) -> None:
        self.w = w

    def ensure_schema(self, schema_fqn: str) -> None:
        cat, sch = _check_fqn(schema_fqn, _SCHEMA_FQN).split(".", 1)
        try:
            self.w.schemas.get(schema_fqn)
        except NotFound:
            self.w.schemas.create(name=sch, catalog_name=cat)

    def create(self, schema_fqn: str, skill_id: str, comment: str = "") -> SkillInfo:
        _check_fqn(f"{schema_fqn}.{skill_id}")
        res = self.w.api_client.do(
            "POST",
            "/api/2.1/unity-catalog/skills",
            query={"parent": f"schemas/{schema_fqn}", "skill_id": skill_id},
            body={"comment": comment} if comment else {},  # an empty skill object; no body is refused
            headers=JSON_HEADERS,
        )
        return _info(res)

    def get(self, fqn: str) -> SkillInfo | None:
        _check_fqn(fqn)
        try:
            res = self.w.api_client.do("GET", f"/api/2.1/unity-catalog/skills/{fqn}", headers=JSON_HEADERS)
        except NotFound:
            return None
        return _info(res)

    def list(self, schema_fqn: str) -> list[SkillInfo]:
        out: list[SkillInfo] = []
        token: str | None = None
        while True:
            query: dict[str, Any] = {
                "parent": f"schemas/{_check_fqn(schema_fqn, _SCHEMA_FQN)}",
                "page_size": 100,
            }
            if token:
                query["page_token"] = token
            res = self.w.api_client.do(
                "GET", "/api/2.1/unity-catalog/skills", query=query, headers=JSON_HEADERS
            )
            out += [_info(s) for s in res.get("skills", [])]
            token = res.get("next_page_token")
            if not token:
                return out

    def finalize(self, fqn: str) -> SkillInfo:
        res = self.w.api_client.do(
            "POST", f"/api/2.1/unity-catalog/skills/{_check_fqn(fqn)}/finalize", headers=JSON_HEADERS
        )
        return _info(res)

    def update_comment(self, fqn: str, comment: str, etag: str) -> SkillInfo:
        res = self.w.api_client.do(
            "PATCH",
            f"/api/2.1/unity-catalog/skills/{_check_fqn(fqn)}",
            query={"etag": etag, "update_mask": "comment"},
            body={"name": f"skills/{fqn}", "comment": comment},
            headers=JSON_HEADERS,
        )
        return _info(res)

    def delete(self, fqn: str, etag: str | None = None) -> None:
        query = {"etag": etag} if etag is not None else None
        self.w.api_client.do(
            "DELETE", f"/api/2.1/unity-catalog/skills/{_check_fqn(fqn)}", query=query, headers=JSON_HEADERS
        )

    def upload_file(self, fqn: str, rel_path: str, data: bytes) -> None:
        self.w.files.upload(f"{_volume_path(fqn)}/{_check_rel(rel_path)}", io.BytesIO(data), overwrite=True)

    def delete_file(self, fqn: str, rel_path: str) -> None:
        self.w.files.delete(f"{_volume_path(fqn)}/{_check_rel(rel_path)}")

    def read_files(self, fqn: str) -> dict[str, bytes]:
        root = _volume_path(fqn)
        out: dict[str, bytes] = {}

        def walk_entry(entry: Any) -> None:
            if entry.is_directory:
                for child in self.w.files.list_directory_contents(entry.path):
                    walk_entry(child)
            else:
                out[entry.path[len(root) + 1 :]] = self.w.files.download(entry.path).contents.read()

        try:
            top = list(self.w.files.list_directory_contents(root))
        except NotFound:
            return {}
        for entry in top:
            walk_entry(entry)
        return out

    def get_tags(self, fqn: str) -> dict[str, str]:
        tags = self.w.entity_tag_assignments.list(ENTITY_TYPE, fqn)
        return {t.tag_key: t.tag_value or "" for t in tags}

    def set_tag(self, fqn: str, key: str, value: str) -> None:
        assignment = EntityTagAssignment(
            entity_type=ENTITY_TYPE, entity_name=fqn, tag_key=key, tag_value=value
        )
        if key in self.get_tags(fqn):
            self.w.entity_tag_assignments.update(ENTITY_TYPE, fqn, key, assignment, update_mask="tag_value")
        else:
            self.w.entity_tag_assignments.create(assignment)
