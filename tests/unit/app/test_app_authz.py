import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from app_fakes import FakeApi, FakeWorkspace
from backend import authz
from backend.authz import Forbidden, Reason
from conftest import make_cfg
from databricks.sdk.errors import NotFound, PermissionDenied, Unauthenticated

from skillhub.registry.uc_skills import SkillInfo

ME = "/api/2.0/preview/scim/v2/Me"
EFF = "/api/2.1/unity-catalog/effective-permissions/schema/skill_hub.common"
EFF_CAT = "/api/2.1/unity-catalog/effective-permissions/catalog/skill_hub"


def me_api(groups=("skillhub-users",)):
    return FakeApi({ME: {"userName": "ana@example.com", "groups": [{"display": g} for g in groups]}})


def info(fqn="skill_hub.common.a"):
    return SkillInfo(
        fqn=fqn,
        name=fqn.rsplit(".", 1)[1],
        bundle_name=None,
        description="d",
        etag="e1",
        finalize_time=None,
        comment=None,
    )


def uc_factory(get=None, lst=None):
    class _UC:
        def __init__(self, w):
            self.w = w

        def get(self, fqn):
            if get is None:
                raise AssertionError("UCSkills.get not expected")
            return get(self.w, fqn)

        def list(self, schema):
            if lst is None:
                raise AssertionError("UCSkills.list not expected")
            return lst(self.w, schema)

    return _UC


def effective(privs):
    return {
        "privilege_assignments": [
            {"principal": "ana@example.com", "privileges": [{"privilege": p} for p in privs]}
        ]
    }


def app_api(schema_privs=("READ_VOLUME", "USE_SCHEMA"), catalog_privs=("USE_CATALOG",), **extra):
    responses = {EFF: effective(list(schema_privs)), EFF_CAT: effective(list(catalog_privs))}
    return FakeApi({**responses, **extra})


USER = FakeWorkspace(api=me_api())
APP_ALL = FakeWorkspace(api=app_api(["ALL_PRIVILEGES"], ["ALL_PRIVILEGES"]))
APP = FakeWorkspace(api=app_api())


def test_user_check_yes_returns_info(monkeypatch):
    got = info()
    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=lambda w, f: got))
    assert authz.authorize_read(USER, APP, got.fqn) is got


def test_user_check_absent_is_forbidden(monkeypatch):
    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=lambda w, f: None))
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, APP, "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE


def test_not_found_under_the_user_token_is_a_definite_no(monkeypatch):
    def hidden(w, f):
        if w is not USER:
            raise AssertionError("no fallback after a definite not-found")
        raise NotFound("no such skill")

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=hidden))
    no_fallback = FakeWorkspace(api=FakeApi())  # any effective-permissions call would raise KeyError
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, no_fallback, "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE
    assert no_fallback.api_client.calls == []


SCOPE_DENIED = PermissionDenied("Provided OAuth token does not have required scopes: unity-catalog")


def test_permission_denied_is_scope_not_granted_and_the_fallback_allows(monkeypatch):
    got = info()

    def scoped(w, f):
        if w is USER:
            raise SCOPE_DENIED
        return got

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=scoped))
    assert authz.authorize_read(USER, APP, got.fqn) is got
    assert authz.authorize_read_as(USER, APP, got.fqn) == (got, APP)  # files are read as the app after this
    assert {c[1] for c in APP.api_client.calls} == {EFF, EFF_CAT}


def test_permission_denied_without_read_volume_is_refused_by_the_fallback(monkeypatch):
    def scoped(w, f):
        if w is USER:
            raise SCOPE_DENIED
        return info()

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=scoped))
    no_read = FakeWorkspace(api=app_api(["USE_SCHEMA"]))
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, no_read, "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE
    assert [c[1] for c in no_read.api_client.calls] == [EFF]


@pytest.mark.parametrize(
    "app_api_",
    [
        app_api(["READ_VOLUME"]),  # USE SCHEMA revoked
        app_api(catalog_privs=["SELECT"]),  # USE CATALOG revoked
    ],
)
def test_fallback_also_needs_use_catalog_and_use_schema(monkeypatch, app_api_):
    def scoped(w, f):
        if w is USER:
            raise SCOPE_DENIED
        return info()

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=scoped))
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, FakeWorkspace(api=app_api_), "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE


def test_plain_permission_denied_is_a_definite_no_without_the_fallback(monkeypatch):
    def denied(w, f):
        raise PermissionDenied("User does not have USE SCHEMA on Schema 'skill_hub.common'")

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=denied, lst=denied))
    no_fallback = FakeWorkspace(api=FakeApi())
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, no_fallback, "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE
    assert authz.list_visible_skills(USER, no_fallback, "skill_hub.common") == []
    assert no_fallback.api_client.calls == []


def test_user_token_read_keeps_the_user_client(monkeypatch):
    got = info()
    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=lambda w, f: got))
    assert authz.authorize_read_as(USER, APP, got.fqn) == (got, USER)


def test_unknown_error_falls_back_to_effective_permissions(monkeypatch):
    got = info()

    def flaky(w, f):
        if w is USER:
            raise RuntimeError("scope missing")  # user-token UC call failed: use effective permissions
        return got

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=flaky))
    assert authz.authorize_read(USER, APP, got.fqn) is got


def test_fallback_denied_without_read_volume(monkeypatch):
    def flaky(w, f):
        if w is USER:
            raise RuntimeError("scope missing")
        return info()

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=flaky))
    denied_app = FakeWorkspace(api=app_api(["USE_SCHEMA"]))
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, denied_app, "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE


def test_fallback_error_fails_closed(monkeypatch):
    def flaky(w, f):
        raise RuntimeError("scope missing")

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=flaky))
    broken_app = FakeWorkspace(api=FakeApi())  # effective-permissions path missing -> KeyError
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, broken_app, "skill_hub.common.a")
    assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED


def test_reviewer_yes_no_unknown():
    cfg = make_cfg()
    assert authz.reviewer_status(FakeWorkspace(api=me_api(("skillhub-reviewers",))), cfg) == "yes"
    assert authz.reviewer_status(FakeWorkspace(api=me_api(("skillhub-users",))), cfg) == "no"
    assert authz.reviewer_status(FakeWorkspace(api=FakeApi()), cfg) == "unknown"  # SCIM call failed


def test_require_reviewer_refuses_non_reviewer():
    cfg = make_cfg()
    with pytest.raises(Forbidden) as exc:
        authz.require_reviewer(FakeWorkspace(api=me_api(("skillhub-users",))), cfg)
    assert exc.value.reason is Reason.NOT_REVIEWER


def test_list_uses_user_view_when_available(monkeypatch):
    rows = [info("skill_hub.common.a")]
    monkeypatch.setattr(authz, "UCSkills", uc_factory(lst=lambda w, s: rows))
    assert authz.list_visible_skills(USER, APP, "skill_hub.common") == rows


def test_list_user_denied_schema_is_empty(monkeypatch):
    def deny(w, s):
        raise NotFound("hidden")

    monkeypatch.setattr(authz, "UCSkills", uc_factory(lst=deny))
    assert authz.list_visible_skills(USER, APP, "skill_hub.external") == []


def test_list_permission_denied_falls_back_to_effective_permissions(monkeypatch):
    rows = [info("skill_hub.common.a")]

    def scoped(w, s):
        if w is USER:
            raise SCOPE_DENIED
        return rows

    monkeypatch.setattr(authz, "UCSkills", uc_factory(lst=scoped))
    assert authz.list_visible_skills(USER, APP, "skill_hub.common") == rows
    no_read = FakeWorkspace(api=app_api(["USE_SCHEMA"]))
    assert authz.list_visible_skills(USER, no_read, "skill_hub.common") == []


def test_list_fallback_filters_out_schema_without_read(monkeypatch):
    def flaky(w, s):
        if w is USER:
            raise RuntimeError("scope missing")
        return [info("skill_hub.common.a")]

    monkeypatch.setattr(authz, "UCSkills", uc_factory(lst=flaky))
    no_read = FakeWorkspace(api=app_api(["USE_SCHEMA"]))
    assert authz.list_visible_skills(USER, no_read, "skill_hub.common") == []


def test_list_fallback_error_refuses(monkeypatch):
    def flaky(w, s):
        raise RuntimeError("scope missing")

    monkeypatch.setattr(authz, "UCSkills", uc_factory(lst=flaky))
    with pytest.raises(Forbidden) as exc:
        authz.list_visible_skills(USER, FakeWorkspace(api=FakeApi()), "skill_hub.common")
    assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED


def test_sdk_errors_on_identity_become_authz_check_failed():
    api = FakeApi(raises={ME: Unauthenticated("expired")})
    with pytest.raises(Forbidden) as exc:
        authz.fetch_me(FakeWorkspace(api=api))
    assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED
    with pytest.raises(Forbidden) as exc:
        authz.require_reviewer(FakeWorkspace(api=api), make_cfg())
    assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED


def test_unauthenticated_skill_check_with_dead_identity_fails_closed(monkeypatch):
    def expired(w, f):
        raise Unauthenticated("expired")

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=expired))
    dead_user = FakeWorkspace(api=FakeApi(raises={ME: Unauthenticated("expired")}))
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(dead_user, APP, "skill_hub.common.a")
    assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED


def test_fallback_not_found_on_app_read_is_refused(monkeypatch):
    def flaky(w, f):
        if w is USER:
            raise RuntimeError("scope missing")
        return None

    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=flaky))
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(USER, APP, "skill_hub.common.a")
    assert exc.value.reason is Reason.SKILL_NOT_READABLE


def test_all_privileges_counts_as_read():
    assert authz.effective_read_schema(APP_ALL, "ana@example.com", "skill_hub.common") is True


def test_effective_read_ignores_other_principals():
    other = {
        "privilege_assignments": [
            {"principal": "bob@example.com", "privileges": [{"privilege": "READ_VOLUME"}]}
        ]
    }
    app = FakeWorkspace(api=FakeApi({EFF: other, EFF_CAT: other}))
    assert authz.effective_read_schema(app, "ana@example.com", "skill_hub.common") is False


def test_effective_read_matches_user_case_insensitively_and_groups():
    def holding(holder):
        privs = [{"privilege": p} for p in ("READ_VOLUME", "USE_SCHEMA", "USE_CATALOG")]
        return {"privilege_assignments": [{"principal": holder, "privileges": privs}]}

    upper = holding("ANA@Example.com")
    assert authz.effective_read_schema(
        FakeWorkspace(api=FakeApi({EFF: upper, EFF_CAT: upper})), "ana@example.com", "skill_hub.common"
    )
    grp = holding("skillhub-users")
    app = FakeWorkspace(api=FakeApi({EFF: grp, EFF_CAT: grp}))
    assert authz.effective_read_schema(app, "ana@example.com", "skill_hub.common", ("skillhub-users",))
    assert not authz.effective_read_schema(app, "ana@example.com", "skill_hub.common")


def test_effective_read_query_is_exactly_the_user():
    api = app_api()
    authz.effective_read_schema(FakeWorkspace(api=api), "ana@example.com", "skill_hub.common")
    # One schema answer holds both READ VOLUME and USE SCHEMA, so the schema is asked once.
    assert api.calls == [
        ("GET", EFF, {"principal": "ana@example.com"}),
        ("GET", EFF_CAT, {"principal": "ana@example.com"}),
    ]


def test_effective_read_with_a_request_memo_asks_for_a_shared_catalog_once_per_principal():
    other = "/api/2.1/unity-catalog/effective-permissions/schema/skill_hub.dept_equity_research"
    api = app_api(**{other: effective(["READ_VOLUME", "USE_SCHEMA"])})
    app, memo = FakeWorkspace(api=api), authz.RequestMemo()
    assert authz.effective_read_schema(app, "ana@example.com", "skill_hub.common", memo=memo)
    assert authz.effective_read_schema(app, "ana@example.com", "skill_hub.dept_equity_research", memo=memo)
    assert [c[1] for c in api.calls] == [EFF, EFF_CAT, other]
    # Another principal never gets an answer fetched for the first one.
    assert not authz.effective_read_schema(app, "bob@example.com", "skill_hub.common", memo=memo)
    assert api.calls[3:] == [("GET", EFF, {"principal": "bob@example.com"})]


def test_request_memo_computes_a_key_once_when_threads_ask_together():
    memo, computed, together = authz.RequestMemo(), [], threading.Barrier(4, timeout=5)

    def compute():
        computed.append(1)
        time.sleep(0.05)  # still running while the other threads ask
        return "answer"

    def ask(_):
        together.wait()
        return memo.get("key", compute)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(ask, range(4))) == ["answer"] * 4
    assert len(computed) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"userName": ""},
        {"userName": "  "},
        {"userName": None},
        {"userName": 5},
        {"userName": "a@b", "groups": None},
    ],
)
def test_fetch_me_rejects_malformed_profile(payload):
    with pytest.raises(Forbidden) as exc:
        authz.fetch_me(FakeWorkspace(api=FakeApi({ME: payload})))
    assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED


def test_fetch_me_without_groups_key_has_no_groups():
    me = authz.fetch_me(FakeWorkspace(api=FakeApi({ME: {"userName": "a@b"}})))
    assert me.groups == ()


HOSTILE = [
    "a.b",
    "a.b.c.d",
    "a/../b.c.d",
    "a.b.c/../x",
    "a.b.c?x=1",
    "a.b.c d",
    "a.b.c\n",
    "a..c",
    "",
    "a.b.c%2f",
]


@pytest.mark.parametrize("fqn", HOSTILE)
def test_authorize_read_rejects_hostile_fqn_before_any_call(monkeypatch, fqn):
    monkeypatch.setattr(authz, "UCSkills", uc_factory())  # any use raises AssertionError
    user = FakeWorkspace(api=FakeApi())
    app = FakeWorkspace(api=FakeApi())
    with pytest.raises(Forbidden) as exc:
        authz.authorize_read(user, app, fqn)
    assert exc.value.reason is Reason.SKILL_NOT_READABLE
    assert user.api_client.calls == [] and app.api_client.calls == []


@pytest.mark.parametrize("schema", ["a", "a.b.c", "a/b", "a.b?x", "a. b", "a.b\n", ".b"])
def test_list_and_effective_reject_hostile_schema(monkeypatch, schema):
    monkeypatch.setattr(authz, "UCSkills", uc_factory())
    app = FakeWorkspace(api=FakeApi())
    with pytest.raises(Forbidden) as exc:
        authz.list_visible_skills(USER, app, schema)
    assert exc.value.reason is Reason.SKILL_NOT_READABLE
    with pytest.raises(Forbidden):
        authz.effective_read_schema(app, "ana@example.com", schema)
    assert app.api_client.calls == []


def test_hyphenated_skill_names_are_accepted(monkeypatch):
    got = info("skill_hub.common.my-skill")
    monkeypatch.setattr(authz, "UCSkills", uc_factory(get=lambda w, f: got))
    assert authz.authorize_read(USER, APP, got.fqn) is got


def test_unavailable_client_never_allows(monkeypatch):
    monkeypatch.setattr(authz, "UCSkills", authz._UnavailableUCSkills)
    for call in (
        lambda: authz.authorize_read(USER, APP, "skill_hub.common.a"),
        lambda: authz.list_visible_skills(USER, APP, "skill_hub.common"),
    ):
        with pytest.raises(Forbidden) as exc:
            call()
        assert exc.value.reason is Reason.AUTHZ_CHECK_FAILED


def test_cached_me_resolves_the_identity_once_and_remembers_a_failure():
    api = me_api()
    me = authz.CachedMe(FakeWorkspace(api=api))
    assert me() == me() and [c[1] for c in api.calls] == [ME]
    dead = FakeApi(raises={ME: Unauthenticated("expired")})
    cached = authz.CachedMe(FakeWorkspace(api=dead))
    for _ in range(2):
        with pytest.raises(Forbidden):
            cached()
    assert len(dead.calls) == 1
