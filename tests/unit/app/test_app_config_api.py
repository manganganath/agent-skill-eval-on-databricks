from conftest import make_cfg

TOKEN = {"x-forwarded-access-token": "tok"}


def test_config_lists_profiles_and_no_secrets(make_client, monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.example.test")
    body = make_client(cfg=make_cfg()).get("/api/config", headers=TOKEN).json()
    assert body == {
        "workspace_url": "https://ws.example.test",
        "models": {"claude-sonnet": "frontier", "gemini-flash": "fast"},
        "departments": ["common", "equity_research", "external"],
        "roles": {"authors": "skillhub-authors", "reviewers": "skillhub-reviewers"},
        "department_access": {
            "common": {"readers": ["skillhub-users"], "quarantine": False},
            "equity_research": {"readers": ["dept-equity-research"], "quarantine": False},
            "external": {"readers": ["skillhub-reviewers"], "quarantine": True},
        },
        "verdict_rules": {
            "fail_rate": 0.5,
            "warn_rate": 0.8,
            "max_error_rate": 0.1,
            "min_reviewed_cases": 5,
            "pass_score": 0.7,
            "gating_modes": body["verdict_rules"]["gating_modes"],
        },
    }
    assert set(body["verdict_rules"]["gating_modes"]) <= {"trigger", "effectiveness"}
    assert "token" not in str(body).lower()


def test_config_requires_user_token(make_client, monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.example.test")
    res = make_client(cfg=make_cfg()).get("/api/config")
    assert res.status_code == 403
    assert res.json()["detail"]["reason"] == "no_user_token"


def test_config_with_host_unset_is_structured_403(make_client, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    res = make_client(cfg=make_cfg()).get("/api/config", headers=TOKEN)
    assert res.status_code == 403
    assert res.json()["detail"]["reason"] == "authz_check_failed"
