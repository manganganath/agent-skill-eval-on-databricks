import json
from types import SimpleNamespace as NS

from skillhub.results import genie
from tests.fakes import make_cfg


class FakeGenie:
    def __init__(self, spaces=(), pages=None):
        self.spaces = list(spaces)
        self.pages = pages  # optional {token: (spaces, next_token)}
        self.created = []

    def list_spaces(self, page_size=None, page_token=None):
        if self.pages is not None:
            spaces, nxt = self.pages[page_token]
            return NS(spaces=spaces, next_page_token=nxt)
        return NS(spaces=self.spaces, next_page_token=None)

    def create_space(self, warehouse_id, serialized_space, *, description=None, parent_path=None, title=None):
        self.created.append(
            {"warehouse_id": warehouse_id, "serialized_space": serialized_space, "title": title}
        )
        return NS(space_id="space-1", title=title)


def test_payload_covers_the_three_eval_views_only():
    cfg = make_cfg()
    payload = json.loads(genie.serialized_space(cfg))
    tables = [t["identifier"] for t in payload["data_sources"]["tables"]]
    assert tables == sorted(f"{cfg.catalog}.eval.{v}" for v in ("matrix_all", "runs_all", "cases_all"))


def test_payload_has_instructions_and_sample_questions():
    cfg = make_cfg()
    raw = genie.serialized_space(cfg)
    payload = json.loads(raw)
    instr = payload["instructions"]["text_instructions"]
    assert len(instr) == 1 and instr[0]["content"] == genie.INSTRUCTIONS
    text = " ".join(instr[0]["content"])
    assert "unreviewed_cases" in text and "advisory" in text
    qs = payload["instructions"]["example_question_sqls"]
    assert len(qs) >= 4
    assert all(q["question"] and q["sql"] for q in qs)
    ids = [q["id"] for q in qs]
    assert ids == sorted(ids)
    assert "{ev}" not in raw and f"{cfg.catalog}.eval." in raw
    assert genie.serialized_space(cfg) == raw  # deterministic


def test_ensure_creates_once_then_reuses_by_title():
    cfg = make_cfg()
    w = NS(genie=FakeGenie())
    assert genie.ensure_genie_space(cfg, w) == "space-1"
    assert w.genie.created[0]["warehouse_id"] == cfg.workspace.sql_warehouse_id
    title = w.genie.created[0]["title"]
    w2 = NS(genie=FakeGenie(spaces=[NS(space_id="existing", title=title)]))
    assert genie.ensure_genie_space(cfg, w2) == "existing"
    assert w2.genie.created == []


def test_existing_space_is_found_on_a_later_page():
    cfg = make_cfg()
    title = genie.space_title(cfg)
    pages = {None: ([NS(space_id="a", title="other")], "t2"), "t2": ([NS(space_id="b", title=title)], None)}
    w = NS(genie=FakeGenie(pages=pages))
    assert genie.ensure_genie_space(cfg, w) == "b"
    assert w.genie.created == []


def test_two_catalogs_in_one_workspace_get_separate_spaces():
    cfg_a = make_cfg()
    cfg_b = cfg_a.model_copy(update={"catalog": "other_catalog"})
    assert genie.space_title(cfg_a) != genie.space_title(cfg_b)
    w = NS(genie=FakeGenie())
    genie.ensure_genie_space(cfg_a, w)
    w.genie.spaces = [NS(space_id="space-a", title=w.genie.created[0]["title"])]
    assert genie.ensure_genie_space(cfg_b, w) == "space-1"
    assert len(w.genie.created) == 2
    assert "other_catalog.eval." in w.genie.created[1]["serialized_space"]


def test_no_hardcoded_workspace_values():
    raw = genie.serialized_space(make_cfg())
    assert "http" not in raw and "test-warehouse" not in raw


# --- The Genie space goes to ci at the hand-over ---


def _perms(refuse=()):
    from databricks.sdk.errors import PermissionDenied

    calls = []

    def update(request_object_type, request_object_id, access_control_list):
        (acl,) = access_control_list
        calls.append(
            (request_object_type, request_object_id, acl.service_principal_name, acl.permission_level.value)
        )
        if acl.permission_level.value in refuse:
            raise PermissionDenied("no")

    return NS(permissions=NS(update=update)), calls


def test_hand_over_space_makes_ci_the_owner():
    from skillhub.results.genie import hand_over_space

    w, calls = _perms()
    assert hand_over_space(w, "s1", "ci-app") == "Genie space s1: IS_OWNER for ci-app"
    assert calls == [("genie", "s1", "ci-app", "IS_OWNER")]


def test_hand_over_space_falls_back_to_can_manage_then_warns(caplog):
    from skillhub.results.genie import hand_over_space

    w, calls = _perms(refuse={"IS_OWNER"})
    assert hand_over_space(w, "s1", "ci-app") == "Genie space s1: CAN_MANAGE for ci-app"
    w, calls = _perms(refuse={"IS_OWNER", "CAN_MANAGE"})
    with caplog.at_level("WARNING"):
        line = hand_over_space(w, "s1", "ci-app")
    assert line.startswith("warning: could not give Genie space s1 to ci-app") and "CAN MANAGE" in line
    assert [c[3] for c in calls] == ["IS_OWNER", "CAN_MANAGE"]
