from typing import Annotated

import pytest
from backend import deps
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

BODY = {"detail": {"reason": "no_user_token"}}


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/probe")
    def probe(w: Annotated[object, Depends(deps.get_user_client)]):
        return {"ok": True}

    return TestClient(app)


def test_missing_token_header_is_403(client):
    r = client.get("/probe")
    assert r.status_code == 403
    assert r.json() == BODY


@pytest.mark.parametrize("value", ["", " ", "   ", "\t"])
def test_blank_token_header_is_403(client, value):
    r = client.get("/probe", headers={deps.TOKEN_HEADER: value})
    assert r.status_code == 403
    assert r.json() == BODY


def test_unset_host_is_403_not_500(client, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    r = client.get("/probe", headers={deps.TOKEN_HEADER: "tok"})
    assert r.status_code == 403
    assert r.json() == {"detail": {"reason": "authz_check_failed"}}
