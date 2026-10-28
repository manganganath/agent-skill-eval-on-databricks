import os

import mlflow
import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.naming import experiment_path
from skillhub.results import experiments as exp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


@pytest.fixture
def probe():
    cfg = load_config()
    w = workspace_client()
    dept = next(iter(cfg.departments))
    exp_id = exp.ensure_experiment(cfg, dept, "it-results-probe")
    yield cfg, w, dept, exp_id
    mlflow.delete_experiment(exp_id)


def test_experiment_binding_and_acl_round_trip(probe):
    cfg, w, dept, exp_id = probe
    assert exp.ensure_experiment(cfg, dept, "it-results-probe") == exp_id  # idempotent
    exp.apply_experiment_acl(w, cfg, dept, exp_id)
    owner = w.current_user.me().user_name
    path = experiment_path(cfg, dept, "it-results-probe")
    # Effective ACL: only grant-derived principals, the owner and admins may appear. Grants inherited from
    # the experiment root and above are checked once, by check_root_acl (C-C1b).
    exempt = exp._parent_ids(w, cfg)
    assert exp.check_experiment_acl(w, cfg, dept, path, exp_id, {owner}, exempt) == []
