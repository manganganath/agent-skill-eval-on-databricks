from types import SimpleNamespace

from databricks.sdk.errors import NotFound


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


class FakeWorkspace:
    def __init__(self, api: FakeApi | None = None, jobs=None, statements=None):
        self.api_client = api or FakeApi()
        self.jobs = jobs
        self.statement_execution = statements
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

    def list_runs(self, job_id=None, active_only=False, **_):
        self.list_runs_calls.append({"job_id": job_id, "active_only": active_only})
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
