import io
from types import SimpleNamespace

from databricks.sdk.errors import NotFound, ResourceAlreadyExists
from databricks.sdk.service.workspace import ObjectType


class FakeApi:
    """Stands in for WorkspaceClient.api_client.do(method, path, query=...). Keyed by path."""

    def __init__(self, responses: dict | None = None, raises: dict | None = None):
        self.responses = dict(responses or {})
        self.raises = dict(raises or {})
        self.calls: list[tuple[str, str, dict]] = []

    def do(self, method, path, query=None, body=None, headers=None, **_):
        self.calls.append((method, path, dict(query or {})))
        if path in self.raises:
            raise self.raises[path]
        if path not in self.responses:
            raise KeyError(f"unexpected call {method} {path}")
        return self.responses[path]


class FakeTree:
    """Stands in for WorkspaceClient.workspace over an in-memory tree of workspace files.

    `files` maps a path, with or without the /Workspace prefix, to bytes; folders are implied by the paths.
    Entries come back without the prefix, as the Workspace API returns them, and `list(recursive=True)` lists
    one folder per call, as the SDK does. `calls` records (method, path). `fail` maps a path to the exception a
    call on it raises. `mkdirs`, `upload` and `delete` write the tree; `formats` records each upload's format."""

    def __init__(self, files: dict[str, bytes] | None = None, fail: dict | None = None):
        self.files = {self._norm(p): bytes(b) for p, b in (files or {}).items()}
        self.fail = {self._norm(p): e for p, e in (fail or {}).items()}
        self.calls: list[tuple[str, str]] = []
        self.made: set[str] = set()
        self.formats: dict[str, object] = {}

    @staticmethod
    def _norm(path: str) -> str:
        return path.removeprefix("/Workspace") if path.startswith("/Workspace/") else path

    def _dirs(self) -> set[str]:
        out: set[str] = set()
        for d in self.made:
            parts = d.split("/")
            out.update("/".join(parts[:i]) for i in range(2, len(parts) + 1))
        for f in self.files:
            parts = f.split("/")
            out.update("/".join(parts[:i]) for i in range(2, len(parts)))
        return out

    def _touch(self, method: str, path: str) -> str:
        p = self._norm(path)
        self.calls.append((method, p))
        if p in self.fail:
            raise self.fail[p]
        return p

    def _children(self, p: str):
        dirs = self._dirs()
        names = {
            c.split("/")[0] for c in (x[len(p) + 1 :] for x in (*self.files, *dirs) if x.startswith(p + "/"))
        }
        for name in sorted(names):
            child = f"{p}/{name}"
            kind = ObjectType.DIRECTORY if child in dirs else ObjectType.FILE
            yield SimpleNamespace(path=child, object_type=kind, modified_at=None)

    def list(self, path, recursive=False, **_):
        queue = [self._touch("list", path)]
        while queue:
            p = queue.pop(0)
            if p not in self._dirs():
                raise NotFound(p)
            for entry in self._children(p):
                if recursive and entry.object_type == ObjectType.DIRECTORY:
                    queue.append(entry.path)
                    self.calls.append(("list", entry.path))
                    continue
                yield entry

    def download(self, path, **_):
        p = self._touch("download", path)
        if p not in self.files:
            raise NotFound(p)
        return io.BytesIO(self.files[p])

    def mkdirs(self, path):
        self.made.add(self._touch("mkdirs", path))

    def upload(self, path, content, *, format=None, overwrite=False, **_):
        p = self._touch("upload", path)
        if p.rsplit("/", 1)[0] not in self._dirs():
            raise NotFound(f"parent of {p}")
        if p in self.files and not overwrite:
            raise ResourceAlreadyExists(p)
        self.files[p] = bytes(content if isinstance(content, bytes) else content.read())
        self.formats[p] = format

    def delete(self, path, recursive=False):
        p = self._touch("delete", path)
        self.files = {f: b for f, b in self.files.items() if f != p and not f.startswith(p + "/")}
        self.made = {d for d in self.made if d != p and not d.startswith(p + "/")}

    def get_status(self, path):
        p = self._touch("get_status", path)
        if p in self.files:
            return SimpleNamespace(path=p, object_type=ObjectType.FILE)
        if p in self._dirs():
            return SimpleNamespace(path=p, object_type=ObjectType.DIRECTORY)
        raise NotFound(p)


class FakeWorkspace:
    def __init__(self, api: FakeApi | None = None, jobs=None, statements=None, workspace=None):
        self.api_client = api or FakeApi()
        self.jobs = jobs
        self.statement_execution = statements
        self.workspace = workspace
        self.current_user = SimpleNamespace(me=lambda: SimpleNamespace(user_name="ana@example.com"))


class FakeSqlClient:
    """Stands in for skillhub.sql.SqlClient. `script` holds rows (a list of dicts) or exceptions, in call order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        self.calls.append((statement, dict(params or {})))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeJobs:
    """Stands in for WorkspaceClient.jobs. Records run_now calls; `runs` maps run_id -> run object."""

    def __init__(self, job_ids=None, run_now_raises=None, run_id=99, runs=None, active=None):
        self.job_ids = {"skill-eval": 11, "skill-switch": 12} if job_ids is None else job_ids
        self.run_now_raises = run_now_raises
        self.run_id = run_id
        self.runs = runs or {}
        self.active = active or []  # parameter dicts of the active runs
        self.list_runs_calls: list[dict] = []
        self.started: list[dict] = []
        self.list_calls = 0

    def list(self, name=None, **_):
        self.list_calls += 1
        return [
            SimpleNamespace(job_id=jid, settings=SimpleNamespace(name=n))
            for n, jid in self.job_ids.items()
            if name is None or n == name
        ]

    def list_runs(self, job_id=None, active_only=False, **kw):
        self.list_runs_calls.append({"job_id": job_id, "active_only": active_only, **kw})
        return [
            SimpleNamespace(
                run_id=i, job_parameters=[SimpleNamespace(name=k, value=v) for k, v in params.items()]
            )
            for i, params in enumerate(self.active)
        ]

    def run_now(self, job_id, job_parameters=None, **_):
        if self.run_now_raises:
            raise self.run_now_raises
        self.started.append({"job_id": job_id, "params": dict(job_parameters or {})})
        return SimpleNamespace(response=SimpleNamespace(run_id=self.run_id))

    def get_run(self, run_id, **_):
        if run_id not in self.runs:
            raise NotFound("no run")
        return self.runs[run_id]


def run_obj(params: dict, life="TERMINATED", result="SUCCESS", job_id=11):
    return SimpleNamespace(
        job_id=job_id,
        job_parameters=[SimpleNamespace(name=k, value=v) for k, v in params.items()],
        state=SimpleNamespace(
            life_cycle_state=SimpleNamespace(value=life),
            result_state=SimpleNamespace(value=result) if result else None,
        ),
    )
